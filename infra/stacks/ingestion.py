"""Market-data ingestion stack, one per environment (tasks 6.8, 6.17; ING-01, ING-02; design P1, P5).

Both triggers run the same operation (:func:`finplan_platform.core.ingestion.run_ingestion`):

* **on demand**: ``POST /v1/ingestions`` on the plan REST API (API stack), served in-process by
  the plan-api function's router;
* **scheduled**: an EventBridge Scheduler schedule (``America/New_York``, weekdays, at the
  configured ``09:00`` or ``09:30``) that invokes the **ingestion function** directly with the
  static scheduler input (``finplan_platform.handlers.scheduler.scheduler_input``).

Resources (all named ``finplan-<env>-financialplanning-*``, tagged, under the environment
permission boundary applied by :class:`PlatformStack`):

* ingestion function ``ingestion-handler`` (Python 3.12, arm64, no VPC, no reserved or
  provisioned concurrency) with the role ``ingestion-handler-role``: read/write ``raw``,
  ``curated`` and ``snapshots`` (+ object tagging for the approval mirror), the snapshot
  catalog, idempotency and audit tables, and ``ssm:GetParameter`` on the budget-state flag;
  asynchronous invocations retry twice and failures go to the dead-letter queue;
* schedule ``daily-ingest`` (``cron(<m> <h> ? * MON-FRI *)`` from ``config/<env>.json``,
  ``ScheduleExpressionTimezone`` ``America/New_York`` so daylight saving never shifts the local
  time), flexible window off, retry policy and the dead-letter queue; its role
  ``daily-ingest-schedule-role`` may only invoke the function and send to the queue;
* SQS dead-letter queue ``ingest-schedule-dlq`` (SQS-managed encryption, TLS only) and a
  CloudWatch alarm when its depth is above 0;
* SSM outputs ``/finplan/<env>/financialplanning/config/ingest-schedule`` (the configured
  time) and ``/finplan/<env>/financialplanning/api/ingestion-endpoint`` (the plan API's
  ``v1/ingestions`` URL, published when the API stack is present in the stage).

Packaging (task 6.17): the function code is :func:`infra.stacks.common.lambda_code` (the
build-stage bundle with the pinned ``providers`` extra when ``FINPLAN_LAMBDA_BUNDLE`` is set).
When the pinned dependencies exceed the zip limit (``scripts/ingestion_package_size.py``),
the build sets ``FINPLAN_INGESTION_IMAGE_DIR`` to a directory with the ``Dockerfile`` from
``infra/docker/ingestion/`` and the function becomes a container-image Lambda.
"""

from __future__ import annotations

import json
import os
from typing import Any

import aws_cdk as cdk
from aws_cdk import Aws, Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct
from finplan_contracts.budget import STATE_PARAMETER
from finplan_platform.core.config import EnvConfig, schedule_expression
from finplan_platform.handlers.scheduler import scheduler_input

from .common import PlatformStack, StageContext, lambda_code, platform_role, resource_name, ssm_name, tag_role

__all__ = ["INGESTION_FUNCTION_LOGICAL", "SCHEDULE_TIMEZONE", "IngestionStack", "add_to_stage", "ingestion_function_name"]

SCHEDULE_TIMEZONE = "America/New_York"
INGESTION_FUNCTION_LOGICAL = "ingestion-handler"
IMAGE_DIR_ENV = "FINPLAN_INGESTION_IMAGE_DIR"


def ingestion_function_name(env: str) -> str:
    return resource_name(env, INGESTION_FUNCTION_LOGICAL)


class IngestionStack(PlatformStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, storage: Any, metadata: Any, api_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, cfg=cfg, description=f"FinancialPlanning market-data ingestion ({cfg.env}): ingestion function, daily America/New_York schedule, DLQ and alarm", **kwargs)
        env = cfg.env
        prod = env == "prod"

        # ------------------------------------------------------------ function and role
        self.role = platform_role(
            self,
            "IngestionRole",
            cfg=cfg,
            logical="ingestion-handler",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"finplan {env} market-data ingestion function",
        )
        self.role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        storage.grant_artifacts(self.role, read=("raw", "curated", "snapshots"), write=("raw", "curated", "snapshots"), tag=("snapshots",))
        metadata.grant_metadata(self.role, read=("snapshot_catalog", "idempotency"), write=("snapshot_catalog", "idempotency", "audit_event"))
        self.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(service="ssm", resource="parameter", resource_name=STATE_PARAMETER.lstrip("/"))],
            )
        )

        self.dlq = sqs.Queue(
            self,
            "ScheduleDlq",
            queue_name=resource_name(env, "ingest-schedule-dlq"),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY,
        )
        tag_role(self.dlq, "daily-ingest-schedule")

        log_group = logs.LogGroup(
            self,
            "IngestionLogs",
            log_group_name=f"/aws/lambda/{ingestion_function_name(env)}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY,
        )
        tag_role(log_group, "ingestion-handler")

        environment = {
            "FINPLAN_ENV": env,
            **{k: v for k, v in storage.bucket_env().items() if k in ("FINPLAN_BUCKET_RAW", "FINPLAN_BUCKET_CURATED", "FINPLAN_BUCKET_SNAPSHOTS", "FINPLAN_KMS_KEY_ARN")},
            "FINPLAN_BUDGET_STATE_PARAMETER": STATE_PARAMETER,
            # the build-stage bundle (and the container image) carries config/ at the task root
            "FINPLAN_CONFIG_DIR": "/var/task/config",
        }
        timeout = Duration.seconds(int(cfg.ingest["function_timeout_seconds"]))
        image_dir = os.environ.get(IMAGE_DIR_ENV)
        common: dict[str, Any] = dict(
            function_name=ingestion_function_name(env),
            role=self.role,
            timeout=timeout,
            memory_size=1024,
            architecture=lambda_.Architecture.ARM_64,
            log_group=log_group,
            environment=environment,
            retry_attempts=2,
            max_event_age=Duration.hours(1),
            # function errors after the async retries land in the same DLQ as scheduler delivery failures
            on_failure=destinations.SqsDestination(self.dlq),
            description="Market-data ingestion (scheduled trigger); same operation as POST /v1/ingestions",
        )
        if image_dir:
            self.function: lambda_.Function = lambda_.DockerImageFunction(
                self,
                "IngestionFunction",
                code=lambda_.DockerImageCode.from_image_asset(image_dir, cmd=["finplan_platform.handlers.ingest.handler"]),
                **common,
            )
        else:
            self.function = lambda_.Function(
                self,
                "IngestionFunction",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="finplan_platform.handlers.ingest.handler",
                code=lambda_code(),
                **common,
            )
        tag_role(self.function, "ingestion-handler")
        # ------------------------------------------------------------ schedule
        self.schedule_role = platform_role(
            self,
            "ScheduleRole",
            cfg=cfg,
            logical="daily-ingest-schedule",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
            description=f"finplan {env} daily ingestion schedule: invoke the ingestion function only",
        )
        self.function.grant_invoke(self.schedule_role)
        self.dlq.grant_send_messages(self.schedule_role)

        self.schedule_time = cfg.schedule_time
        self.schedule = scheduler.CfnSchedule(
            self,
            "DailyIngestSchedule",
            name=resource_name(env, "daily-ingest"),
            description=f"Daily {cfg.dataset_id} ingestion at {cfg.schedule_time} America/New_York on weekdays",
            schedule_expression=schedule_expression(cfg.schedule_time),
            schedule_expression_timezone=SCHEDULE_TIMEZONE,
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            state="ENABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.function.function_arn,
                role_arn=self.schedule_role.role_arn,
                input=json.dumps(scheduler_input(cfg.dataset_id), sort_keys=True),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_event_age_in_seconds=3600, maximum_retry_attempts=3),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(arn=self.dlq.queue_arn),
            ),
        )
        # AWS::Scheduler::Schedule takes no Tags: declare the ownership logical role in Metadata
        self.schedule.add_metadata("logical-role", "daily-ingest-schedule")

        self.dlq_alarm = cloudwatch.Alarm(
            self,
            "ScheduleDlqAlarm",
            alarm_name=resource_name(env, "ingest-dlq-depth"),
            alarm_description="Daily ingestion events failed after retries (dead-letter queue depth above 0)",
            metric=self.dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(5), statistic="Maximum"),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        tag_role(self.dlq_alarm, "daily-ingest-schedule")

        # ------------------------------------------------------------ published references
        self.schedule_param = ssm.StringParameter(
            self,
            "IngestScheduleParam",
            parameter_name=ssm_name(env, "config", "ingest-schedule"),
            string_value=cfg.schedule_time,
            description="Daily ingestion local time (America/New_York, weekdays); 09:00 or 09:30 (OQ-6)",
        )
        tag_role(self.schedule_param, "daily-ingest-schedule")
        self.endpoint_param: ssm.StringParameter | None = None
        if api_url:
            self.endpoint_param = ssm.StringParameter(
                self,
                "IngestionEndpointParam",
                parameter_name=ssm_name(env, "api", "ingestion-endpoint"),
                string_value=cdk.Fn.join("", [api_url, "v1/ingestions"]),
                description="On-demand ingestion endpoint (POST, IAM auth); same operation as the daily schedule",
            )
            tag_role(self.endpoint_param, "ingestion-api")


def add_to_stage(stage: cdk.Stage, ctx: StageContext) -> None:
    env = ctx.cfg.env
    api = ctx.extras.get("api") or {}
    stack = IngestionStack(
        stage,
        "Ingestion",
        cfg=ctx.cfg,
        storage=ctx.storage,
        metadata=ctx.metadata,
        api_url=api.get("url"),
        stack_name=f"finplan-{env}-financialplanning-ingestion",
    )
    ctx.extras["ingestion"] = {"stack": stack, "function": stack.function, "role": stack.role, "schedule": stack.schedule, "dlq": stack.dlq}
