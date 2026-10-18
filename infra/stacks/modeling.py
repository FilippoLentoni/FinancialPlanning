"""Beta modeling competition pipeline (beta only; ``config/beta.json`` ``modeling_competition``).

Deployed as the ``Modeling`` construct of the beta daily-trigger stack (see :func:`add_to_stage`).

Replaces the dev-box systemd timers of the beta modeling competition with deployed, scheduled
infrastructure. FinancialPlanning collects the evidence and renders the reports; FinanceModel's
runner Lambda ``finplan-<env>-financemodel-<runner_function_logical>`` (deployed by FinanceModel,
referenced here by its deterministic name only, so it may not exist yet) runs the models.

* Lambda ``modeling-sources`` (``finplan_platform.handlers.modeling.sources_handler``): quote and
  research collection (``scripts/collect_modeling_sources.py``) into the modeling evidence tables
  and the reports bucket. Public unpaid HTTPS sources only (Yahoo chart API, Wikipedia, State
  Street, Google News RSS); no API keys, no VPC.
* Lambda ``modeling-report`` (``...report_handler``): ``scripts/modeling_report.py`` and
  ``scripts/modeling_day_review.py``; immutable revisions plus the stable views under
  ``modeling-views/v1/`` (``finplan_platform.reporting.views``).
* Step Functions Standard ``modeling-decision``: ``CollectQuotes -> RunModels -> Report``
  (``RunModels`` failure still renders the report, then fails the execution) and
  ``modeling-research``: ``CollectResearch -> PrepareLLMPrompt``.
* EventBridge Scheduler (America/New_York, Monday to Friday, flexible window off, one retry,
  30 minute maximum event age), each passing ``<aws.scheduler.scheduled-time>``: decisions,
  research, end of day (``eod``) and Friday weekly (``weekly`` and ``closed_week``).

Logical-role tags reuse the ownership matrix rows ``daily-recommendation-trigger`` (functions, roles,
log groups, state machines) and ``daily-scheduler`` (schedules and their role): the pinned contract
matrix lists no modeling rows, and both rows already cover these resource types.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import aws_cdk as cdk
from aws_cdk import Arn, ArnFormat, Aws, Duration, RemovalPolicy, Size
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct
from finplan_platform.core.config import EnvConfig
from finplan_platform.core.repository import table_name

from .common import StageContext, lambda_code, platform_role_name, resource_name, role_arn_pattern, tag_role

__all__ = ["ModelingPipeline", "add_to_stage", "runner_function_name", "cron", "SCHEDULE_TIMEZONE", "FUNCTION_ROLE_TAG", "MACHINE_ROLE_TAG", "SCHEDULE_ROLE_TAG"]

SCHEDULE_TIMEZONE = "America/New_York"
#: ownership-matrix logical roles (see the module docstring)
FUNCTION_ROLE_TAG = "daily-trigger-step"
MACHINE_ROLE_TAG = "daily-trigger"
SCHEDULE_ROLE_TAG = "daily-ingest-schedule"
#: metadata tables the collector writes (inputs, their DynamoDB content copies, audit events)
SOURCE_WRITE_TABLES = ("research_input", "research_run", "portfolio_observation", "audit_event")


def runner_function_name(env: str, logical: str) -> str:
    return f"finplan-{env}-financemodel-{logical}"


def cron(times: list[str] | tuple[str, ...], days: str = "MON-FRI") -> str:
    """One EventBridge Scheduler cron for HH:MM times sharing a minute (validated by the config)."""
    minutes = {t.split(":")[1] for t in times}
    if len(minutes) != 1:
        raise ValueError(f"times {times} do not share a minute")
    hours = ",".join(str(int(t.split(":")[0])) for t in sorted(times))
    return f"cron({int(minutes.pop())} {hours} ? * {days} *)"


class ModelingPipeline(Construct):
    """Every modeling resource, added to the environment's existing daily-trigger stack."""

    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, storage: Any, metadata: Any, stage_role_name: str | None = None) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        mc = cfg.modeling_competition
        if not mc or cfg.env != "beta":
            raise ValueError("the modeling competition stack is beta only")
        env = cfg.env
        self.experiment = str(mc["experiment_id"])
        symbols = list(mc["symbols"])

        # ------------------------------------------------------------ functions
        env_vars = {
            "FINPLAN_ENV": env,
            **{k: v for k, v in storage.bucket_env().items() if k in ("FINPLAN_BUCKET_REPORTS", "FINPLAN_KMS_KEY_ARN")},
            "FINPLAN_CONFIG_DIR": "/var/task/config",
            "FINPLAN_MODELING_EXPERIMENT": self.experiment,
        }

        def function(cid: str, logical: str, handler: str, code: lambda_.Code, *, memory: int, write_tables: tuple[str, ...], async_retries: int, description: str) -> lambda_.Function:
            role = iam.Role(self, f"{cid}Role", role_name=platform_role_name(env, logical), assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"), description=f"finplan {env} {description}")
            tag_role(role, FUNCTION_ROLE_TAG)
            role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
            # compact grants with name patterns: per-table cross-stack grants overflow the inline policy
            reports = storage.buckets["reports"]
            role.add_to_principal_policy(iam.PolicyStatement(sid="ReportsBucketList", actions=["s3:ListBucket", "s3:GetBucketLocation"], resources=[reports.bucket_arn]))
            role.add_to_principal_policy(iam.PolicyStatement(sid="ReportsObjects", actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], resources=[reports.arn_for_objects("*")]))
            role.add_to_principal_policy(iam.PolicyStatement(sid="PlatformKey", actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:ReEncrypt*", "kms:DescribeKey"], resources=[storage.key.key_arn]))
            tables = stack.format_arn(service="dynamodb", resource="table", resource_name=f"finplan-{env}-financialplanning-*")
            role.add_to_principal_policy(iam.PolicyStatement(sid="MetadataReads", actions=["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"], resources=[tables, tables + "/index/*"]))
            if write_tables:
                # PutItem/UpdateItem/ConditionCheckItem: what TransactWriteItems needs; never DeleteItem
                role.add_to_principal_policy(iam.PolicyStatement(sid="EvidenceWrites", actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"],
                                                                 resources=[stack.format_arn(service="dynamodb", resource="table", resource_name=table_name(env, t)) for t in write_tables]))
            role.add_to_principal_policy(iam.PolicyStatement(sid="ReadReportsBucketReference", actions=["ssm:GetParameter"], resources=[stack.format_arn(service="ssm", resource="parameter", resource_name=f"finplan/{env}/financialplanning/config/bucket-reports")]))
            log_group = logs.LogGroup(self, f"{cid}Logs", log_group_name=f"/aws/lambda/{resource_name(env, logical)}", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
            tag_role(log_group, FUNCTION_ROLE_TAG)
            fn = lambda_.Function(
                self,
                cid,
                function_name=resource_name(env, logical),
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                role=role,
                timeout=Duration.seconds(900),
                memory_size=memory,
                ephemeral_storage_size=Size.mebibytes(2048),
                architecture=lambda_.Architecture.ARM_64,
                log_group=log_group,
                environment=env_vars,
                description=description,
                retry_attempts=async_retries,  # scheduler invocations are asynchronous; Step Functions calls are synchronous
            )
            tag_role(fn, FUNCTION_ROLE_TAG)
            if stage_role_name:
                # the Beta stage's deployed tests invoke the read-only preflight (tests/integration)
                fn.add_permission("InvokeFromStageRole", principal=iam.ArnPrincipal(role_arn_pattern(stage_role_name)), action="lambda:InvokeFunction")
            return fn

        self.sources = function("SourcesFunction", "modeling-sources", "finplan_platform.handlers.modeling.sources_handler", lambda_code("modeling-sources"), memory=1024, write_tables=SOURCE_WRITE_TABLES, async_retries=0,
                                description="Beta modeling quote/research collection (public sources; evidence tables and reports bucket only)")
        self.report = function("ReportFunction", "modeling-report", "finplan_platform.handlers.modeling.report_handler", lambda_code("modeling-report"), memory=3008, write_tables=(), async_retries=1,
                               description="Beta modeling reports and day review (S3 reports bucket only)")
        self.runner_name = runner_function_name(env, str(mc["runner_function_logical"]))
        self.runner_arn = Arn.format(cdk.ArnComponents(service="lambda", resource="function", resource_name=self.runner_name, arn_format=ArnFormat.COLON_RESOURCE_NAME), stack)
        runner = lambda_.Function.from_function_attributes(self, "ForwardRunner", function_arn=self.runner_arn, same_environment=True)

        # ------------------------------------------------------------ state machines
        def invoke(name: str, fn: lambda_.IFunction, payload: Mapping[str, Any], result: str, *, timeout: Duration) -> tasks.LambdaInvoke:
            return tasks.LambdaInvoke(self, name, lambda_function=fn, payload=sfn.TaskInput.from_object(dict(payload)), payload_response_only=True,
                                      retry_on_service_exceptions=True, result_path=result, task_timeout=sfn.Timeout.duration(timeout))

        slot = sfn.JsonPath.string_at("$.slot")
        collect_quotes = invoke("CollectQuotes", self.sources, {"kind": "quotes", "slot": slot, "experiment": self.experiment, "symbols": symbols}, "$.quotes", timeout=Duration.seconds(960))
        run_models = invoke("RunModels", runner, {"action": "run", "experiment": self.experiment, "slot": slot}, "$.models", timeout=Duration.seconds(960))
        report = invoke("Report", self.report, {"mode": "current", "experiment": self.experiment, "slot": slot}, "$.report", timeout=Duration.seconds(960))
        run_models.add_catch(report, errors=["States.ALL"], result_path="$.models_error")
        outcome = (sfn.Choice(self, "ModelsSucceeded")
                   .when(sfn.Condition.is_present("$.models_error"), sfn.Fail(self, "RunModelsFailed", error="RunModelsFailed", cause="The FinanceModel forward runner failed; the report was still rendered"))
                   .otherwise(sfn.Succeed(self, "Decided")))
        decision = collect_quotes.next(run_models).next(report).next(outcome)

        collect_research = invoke("CollectResearch", self.sources, {"kind": "research", "slot": slot, "experiment": self.experiment}, "$.research", timeout=Duration.seconds(960))
        prepare = invoke("PrepareLLMPrompt", runner, {"action": "prepare_llm_prompt", "experiment": self.experiment, "research_slot": slot}, "$.prompt", timeout=Duration.seconds(960))
        research = collect_research.next(prepare)

        self.machine_role = iam.Role(self, "MachineRole", role_name=platform_role_name(env, "modeling-machine"), assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
                                     description=f"finplan {env} modeling state machines: invoke the modeling functions and the FinanceModel forward runner only")
        tag_role(self.machine_role, MACHINE_ROLE_TAG)
        self.decision_machine = sfn.StateMachine(self, "DecisionMachine", state_machine_name=resource_name(env, "modeling-decision"), state_machine_type=sfn.StateMachineType.STANDARD,
                                                 definition_body=sfn.DefinitionBody.from_chainable(decision), role=self.machine_role, timeout=Duration.minutes(55))
        tag_role(self.decision_machine, MACHINE_ROLE_TAG)
        self.research_machine = sfn.StateMachine(self, "ResearchMachine", state_machine_name=resource_name(env, "modeling-research"), state_machine_type=sfn.StateMachineType.STANDARD,
                                                 definition_body=sfn.DefinitionBody.from_chainable(research), role=self.machine_role, timeout=Duration.minutes(40))
        tag_role(self.research_machine, MACHINE_ROLE_TAG)

        # ------------------------------------------------------------ schedules
        self.schedule_role = iam.Role(self, "ScheduleRole", role_name=platform_role_name(env, "modeling-schedule"),
                                      assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}}),
                                      description=f"finplan {env} modeling schedules: start the modeling state machines and invoke the report function only")
        tag_role(self.schedule_role, SCHEDULE_ROLE_TAG)
        self.decision_machine.grant_start_execution(self.schedule_role)
        self.research_machine.grant_start_execution(self.schedule_role)
        self.report.grant_invoke(self.schedule_role)

        retry = scheduler.CfnSchedule.RetryPolicyProperty(maximum_event_age_in_seconds=1800, maximum_retry_attempts=1)
        self.schedules: dict[str, scheduler.CfnSchedule] = {}

        def schedule(cid: str, logical: str, expression: str, target_arn: str, payload: Mapping[str, Any], description: str) -> None:
            s = scheduler.CfnSchedule(
                self, cid, name=resource_name(env, logical), description=description,
                schedule_expression=expression, schedule_expression_timezone=SCHEDULE_TIMEZONE,
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"), state="ENABLED",
                target=scheduler.CfnSchedule.TargetProperty(arn=target_arn, role_arn=self.schedule_role.role_arn, input=json.dumps(dict(payload), sort_keys=True), retry_policy=retry),
            )
            s.add_metadata("logical-role", SCHEDULE_ROLE_TAG)  # AWS::Scheduler::Schedule takes no Tags
            self.schedules[logical] = s

        at = "<aws.scheduler.scheduled-time>"
        schedule("DecisionSchedule", "modeling-decision", cron(mc["decision_times"]), self.decision_machine.state_machine_arn, {"slot": at},
                 f"Beta modeling decisions at {', '.join(mc['decision_times'])} America/New_York on weekdays")
        schedule("ResearchSchedule", "modeling-research", cron(mc["research_times"]), self.research_machine.state_machine_arn, {"slot": at},
                 f"Beta modeling research at {', '.join(mc['research_times'])} America/New_York on weekdays")
        schedule("EodSchedule", "modeling-eod", cron([mc["eod_time"]]), self.report.function_arn, {"mode": "eod", "experiment": self.experiment, "scheduled_time": at},
                 f"Beta modeling finalized day report and review at {mc['eod_time']} America/New_York on weekdays")
        schedule("WeeklySchedule", "modeling-weekly", cron([mc["weekly_time"]], "FRI"), self.report.function_arn, {"mode": "weekly", "experiment": self.experiment, "scheduled_time": at},
                 f"Beta modeling rolling weekly report on Fridays at {mc['weekly_time']} America/New_York")
        schedule("ClosedWeekSchedule", "modeling-closed-week", cron([mc["weekly_time"]], "FRI"), self.report.function_arn, {"mode": "closed_week", "experiment": self.experiment, "scheduled_time": at},
                 f"Beta modeling closed-week report on Fridays at {mc['weekly_time']} America/New_York")



def add_to_stage(stage: cdk.Stage, ctx: StageContext) -> None:
    """Adds the pipeline to the existing ``finplan-<env>-financialplanning-daily-trigger`` stack.

    A separate stack would need a new deploy action in the pipeline, which only the authenticated
    bootstrap can add (the pipeline lives in the tooling stack); the daily-trigger stack is already
    deployed by the Beta stage's ``DeployDailyTrigger`` action.
    """
    cfg = ctx.cfg
    if not cfg.modeling_competition or cfg.env != "beta":
        return
    trigger = (ctx.extras.get("daily_trigger") or {}).get("stack")
    if trigger is None:
        raise ValueError("the beta modeling pipeline needs the daily-trigger stack (infra.stacks.daily_trigger runs first)")
    from .pipeline import stage_role_name

    pipeline = ModelingPipeline(trigger, "Modeling", cfg=cfg, storage=ctx.storage, metadata=ctx.metadata, stage_role_name=stage_role_name(cfg.env))
    ctx.extras["modeling"] = {"stack": trigger, "construct": pipeline, "sources": pipeline.sources, "report": pipeline.report, "decision": pipeline.decision_machine, "research": pipeline.research_machine}
