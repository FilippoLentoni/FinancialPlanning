"""Daily recommendation trigger stack, one per environment (daily-recommendation-trigger; tasks 3.1-3.3; design T1).

* step Lambda ``daily-trigger-step`` (Python 3.12, arm64; ``finplan_platform.handlers.daily_trigger.handler``)
  with role ``finplan-<env>-financialplanning-daily-trigger-step-role``: SSM reads of the FinanceModel
  production-strategy key, the research-plan reference, the two endpoints and the budget state;
  ``execute-api:Invoke`` on FinanceModel ``POST /v1/jobs`` and ``GET /v1/jobs/*`` and on the platform
  routes of the ``automation`` class; an **explicit deny** on the publish route (DLY-06); write-once
  outcome records in the ``reports`` bucket;
* Step Functions Standard state machine ``daily-trigger`` (role ``daily-trigger-role``: invoke the step
  Lambda only): ``CheckSnapshot -> ReadStrategy -> CheckBudget -> SubmitJob -> (Wait -> Poll) x <= 9 ->
  Accept -> WriteOutcome``;
* EventBridge rule ``daily-trigger-start``: the ingestion function's asynchronous **success** destination
  event for a scheduled research-universe ingestion starts the state machine.

No DynamoDB stream, no CDK bootstrap references; the function code is the build-stage arm64 bundle.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct
from finplan_contracts import ssm as contract_ssm
from finplan_contracts.budget import STATE_PARAMETER
from finplan_platform.core.config import EnvConfig
from finplan_platform.core.daily_trigger import research_plan_ref_parameter
from finplan_platform.handlers.api import AUTOMATION_CLASS, ROUTES, route_allowed

from .common import (
    PlatformStack,
    StageContext,
    lambda_code,
    platform_role,
    resource_name,
    tag_role,
)

__all__ = ["DailyTriggerStack", "add_to_stage", "publish_deny_resources", "trigger_api_resources"]

STEP_LOGICAL = "daily-trigger-step"


def _api_arn(stack: cdk.Stack, path: str) -> str:
    return stack.format_arn(service="execute-api", resource="*", resource_name=path)


def trigger_api_resources() -> list[str]:
    """Platform routes of the automation class as ``<stage>/<METHOD>/<path>`` patterns."""
    return sorted({f"*/{r.resource_pattern.split('/', 1)[0]}/{r.resource_pattern.split('/', 1)[1]}" for r in ROUTES if route_allowed(r, AUTOMATION_CLASS)})


def publish_deny_resources() -> list[str]:
    return sorted({f"*/{r.resource_pattern.split('/', 1)[0]}/{r.resource_pattern.split('/', 1)[1]}" for r in ROUTES if r.operation == "publish_plan_version"})


class DailyTriggerStack(PlatformStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, storage: Any, ingestion_function: lambda_.IFunction, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, cfg=cfg, description=f"FinancialPlanning daily recommendation trigger ({cfg.env}): state machine, step function, start rule", **kwargs)
        env = cfg.env
        prod = env == "prod"
        u = cfg.universe
        dt = cfg.daily_trigger or {"poll_interval_seconds": 300, "max_polls": 9}

        # ------------------------------------------------------------ step function
        self.step_role = platform_role(self, "StepRole", cfg=cfg, logical=STEP_LOGICAL, assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"), description=f"finplan {env} daily recommendation trigger step function (never publishes)")
        self.step_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        storage.grant_artifacts(self.step_role, write=("reports",))
        params = [
            contract_ssm.production_strategy_parameter(env),
            research_plan_ref_parameter(env),
            contract_ssm.build(env, "financemodel", "api", "job-endpoint"),
            contract_ssm.build(env, "financialplanning", "api", "plan-endpoint"),
            STATE_PARAMETER,
        ]
        self.step_role.add_to_principal_policy(iam.PolicyStatement(sid="ReadTriggerParameters", actions=["ssm:GetParameter"], resources=[self.format_arn(service="ssm", resource="parameter", resource_name=p.lstrip("/")) for p in params]))
        self.step_role.add_to_principal_policy(
            iam.PolicyStatement(sid="FinanceModelJobApi", actions=["execute-api:Invoke"], resources=[_api_arn(self, "*/POST/v1/jobs"), _api_arn(self, "*/GET/v1/jobs/*")])
        )
        self.step_role.add_to_principal_policy(iam.PolicyStatement(sid="PlatformAutomationRoutes", actions=["execute-api:Invoke"], resources=[_api_arn(self, p) for p in trigger_api_resources()]))
        self.step_role.add_to_principal_policy(iam.PolicyStatement(sid="DenyPublish", effect=iam.Effect.DENY, actions=["execute-api:Invoke"], resources=[_api_arn(self, p) for p in publish_deny_resources()]))

        log_group = logs.LogGroup(self, "StepLogs", log_group_name=f"/aws/lambda/{resource_name(env, STEP_LOGICAL)}", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY)
        tag_role(log_group, STEP_LOGICAL)
        self.function = lambda_.Function(
            self,
            "StepFunction",
            function_name=resource_name(env, STEP_LOGICAL),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="finplan_platform.handlers.daily_trigger.handler",
            code=lambda_code("daily-trigger"),
            role=self.step_role,
            timeout=Duration.seconds(60),
            memory_size=256,
            architecture=lambda_.Architecture.ARM_64,
            log_group=log_group,
            environment={
                "FINPLAN_ENV": env,
                **{k: v for k, v in storage.bucket_env().items() if k in ("FINPLAN_BUCKET_REPORTS", "FINPLAN_KMS_KEY_ARN")},
                "FINPLAN_CONFIG_DIR": "/var/task/config",
            },
            description="Daily recommendation trigger steps (strategy-gated, budget-checked, never publishes)",
        )
        tag_role(self.function, STEP_LOGICAL)

        # ------------------------------------------------------------ state machine (T1)
        def task(name: str, step: str, *, start: bool = False) -> tasks.LambdaInvoke:
            payload = {"step": step, "event": sfn.JsonPath.entire_payload} if start else {"step": step, "state": sfn.JsonPath.entire_payload}
            return tasks.LambdaInvoke(
                self,
                name,
                lambda_function=self.function,
                payload=sfn.TaskInput.from_object(payload),
                payload_response_only=True,
                retry_on_service_exceptions=True,
            )

        start = task("Start", "start", start=True)
        check_snapshot = task("CheckSnapshot", "check_snapshot")
        read_strategy = task("ReadStrategy", "read_strategy")
        check_budget = task("CheckBudget", "check_budget")
        submit = task("SubmitJob", "submit_job")
        poll = task("Poll", "poll_job")
        accept = task("Accept", "accept")
        write = task("WriteOutcome", "write_outcome")
        wait = sfn.Wait(self, "WaitForJob", time=sfn.WaitTime.duration(Duration.seconds(int(dt["poll_interval_seconds"]))))
        done = sfn.Condition.is_present("$.outcome")

        def gate(name: str, nxt: sfn.IChainable) -> sfn.Choice:
            return sfn.Choice(self, name).when(done, write).otherwise(nxt)

        write.next(sfn.Succeed(self, "Recorded"))
        accept.next(write)
        poll_choice = sfn.Choice(self, "AfterPoll").when(done, write).when(sfn.Condition.boolean_equals("$.terminal", True), accept).otherwise(wait)
        wait.next(poll)
        poll.next(poll_choice)
        definition = (
            start.next(check_snapshot)
        )
        check_snapshot.next(gate("AfterSnapshot", read_strategy))
        read_strategy.next(gate("AfterStrategy", check_budget))
        check_budget.next(gate("AfterBudget", submit))
        submit.next(gate("AfterSubmit", wait))

        self.machine_role = platform_role(self, "MachineRole", cfg=cfg, logical="daily-trigger", assumed_by=iam.ServicePrincipal("states.amazonaws.com"), description=f"finplan {env} daily recommendation trigger state machine: invoke the step function only")
        # Standard workflows keep 90 days of execution history (auditable); no vended log delivery, which
        # would need account-wide logs:*LogDelivery grants on the deploy and machine roles
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=resource_name(env, "daily-trigger"),
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            role=self.machine_role,
            timeout=Duration.minutes(60),
        )
        tag_role(self.state_machine, "daily-trigger")

        # ------------------------------------------------------------ start rule
        self.rule = events.Rule(
            self,
            "StartRule",
            rule_name=resource_name(env, "daily-trigger-start"),
            description="Scheduled research-universe ingestion succeeded -> daily recommendation trigger",
            event_pattern=events.EventPattern(
                source=["lambda"],
                detail_type=["Lambda Function Invocation Result - Success"],
                detail={
                    "requestContext": {"functionArn": [{"prefix": ingestion_function.function_arn}]},
                    "requestPayload": {"source": ["finplan.scheduler"], "dataset_id": [u.dataset_id if u else "none"]},
                },
            ),
        )
        rule_role = platform_role(self, "RuleRole", cfg=cfg, logical="daily-trigger-rule", assumed_by=iam.ServicePrincipal("events.amazonaws.com"), description=f"finplan {env} daily trigger start rule: start the state machine only")
        self.rule.add_target(targets.SfnStateMachine(self.state_machine, role=rule_role))
        tag_role(self.rule, "daily-trigger-rule")


def add_to_stage(stage: cdk.Stage, ctx: StageContext) -> None:
    env = ctx.cfg.env
    ingestion = ctx.extras.get("ingestion") or {}
    if not ingestion or ctx.cfg.universe is None:
        return
    stack = DailyTriggerStack(stage, "DailyTrigger", cfg=ctx.cfg, storage=ctx.storage, ingestion_function=ingestion["function"], stack_name=f"finplan-{env}-financialplanning-daily-trigger")
    ctx.extras["daily_trigger"] = {"stack": stack, "state_machine": stack.state_machine, "step_role": stack.step_role, "function": stack.function}
