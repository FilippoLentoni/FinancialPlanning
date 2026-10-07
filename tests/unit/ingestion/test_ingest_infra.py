"""Ingestion stack (task 6.8): ING-02 template assertions, published references, tags, ownership.

Synthesized offline. The schedule is asserted per configured time (09:00 default and 09:30)
and always in ``America/New_York``, so daylight saving never shifts the local run time.
"""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from finplan_contracts.ownership import check_template
from finplan_platform.core.config import EnvConfig, load_config, load_shared_config
from finplan_platform.handlers.scheduler import scheduler_input

from infra.stacks.common import StageContext
from infra.stacks.ingestion import IngestionStack, add_to_stage
from infra.stacks.metadata import MetadataStack
from infra.stacks.storage import StorageStack

from .conftest import config_with


def _synth(cfg: EnvConfig, *, api_url: str | None = None) -> dict[str, Any]:
    app = cdk.App()
    stage = cdk.Stage(app, cfg.env.capitalize())
    storage = StorageStack(stage, "Storage", cfg=cfg, stack_name=f"finplan-{cfg.env}-financialplanning-storage")
    metadata = MetadataStack(stage, "Metadata", cfg=cfg, storage=storage, stack_name=f"finplan-{cfg.env}-financialplanning-metadata")
    ctx = StageContext(cfg=cfg, shared=load_shared_config(), storage=storage, metadata=metadata)
    if api_url:
        ctx.extras["api"] = {"url": api_url}
    add_to_stage(stage, ctx)
    stack: IngestionStack = ctx.extras["ingestion"]["stack"]
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def templates() -> dict[str, dict[str, Any]]:
    return {env: _synth(load_config(env), api_url="https://example.invalid/live/") for env in ("beta", "gamma", "prod")}


def _of(t: dict[str, Any], typ: str) -> list[dict[str, Any]]:
    return [r for r in t["Resources"].values() if r["Type"] == typ]


# ------------------------------------------------------------------ ING-02
@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_schedule_weekdays_new_york_from_configuration(templates: dict[str, Any], env: str) -> None:
    (sched,) = _of(templates[env], "AWS::Scheduler::Schedule")
    p = sched["Properties"]
    assert p["ScheduleExpression"] == "cron(0 9 ? * MON-FRI *)"
    assert p["ScheduleExpressionTimezone"] == "America/New_York"
    assert p["FlexibleTimeWindow"] == {"Mode": "OFF"} and p["State"] == "ENABLED"
    assert p["Target"]["RetryPolicy"]["MaximumRetryAttempts"] >= 1 and "DeadLetterConfig" in p["Target"]
    assert json.loads(p["Target"]["Input"]) == scheduler_input("finance/etf-daily/SPY")


def test_configured_0930_fires_at_0930_new_york() -> None:
    t = _synth(config_with(**{"ingest.schedule_time": "09:30"}))
    (sched,) = _of(t, "AWS::Scheduler::Schedule")
    assert sched["Properties"]["ScheduleExpression"] == "cron(30 9 ? * MON-FRI *)"
    assert sched["Properties"]["ScheduleExpressionTimezone"] == "America/New_York"
    (param,) = [p for p in _of(t, "AWS::SSM::Parameter") if p["Properties"]["Name"].endswith("/config/ingest-schedule")]
    assert param["Properties"]["Value"] == "09:30"


def test_dlq_alarm_and_async_failures_routed_to_the_dlq(templates: dict[str, Any]) -> None:
    t = templates["beta"]
    (q,) = _of(t, "AWS::SQS::Queue")
    assert q["Properties"]["SqsManagedSseEnabled"] is True
    (alarm,) = _of(t, "AWS::CloudWatch::Alarm")
    a = alarm["Properties"]
    assert a["MetricName"] == "ApproximateNumberOfMessagesVisible" and a["Threshold"] == 0 and a["ComparisonOperator"] == "GreaterThanThreshold"
    (cfg,) = _of(t, "AWS::Lambda::EventInvokeConfig")
    assert cfg["Properties"]["MaximumRetryAttempts"] == 2 and "OnFailure" in cfg["Properties"]["DestinationConfig"]


def test_published_references(templates: dict[str, Any]) -> None:
    names = {p["Properties"]["Name"]: p["Properties"]["Value"] for p in _of(templates["gamma"], "AWS::SSM::Parameter")}
    assert names["/finplan/gamma/financialplanning/config/ingest-schedule"] == "09:00"
    assert "/finplan/gamma/financialplanning/api/ingestion-endpoint" in names


def test_function_is_serverless_arm64_without_vpc_or_reserved_concurrency(templates: dict[str, Any]) -> None:
    (fn,) = _of(templates["beta"], "AWS::Lambda::Function")
    p = fn["Properties"]
    assert p["FunctionName"] == "finplan-beta-financialplanning-ingestion-handler"
    assert p["Runtime"] == "python3.12" and p["Architectures"] == ["arm64"] and p["Handler"] == "finplan_platform.handlers.ingest.handler"
    assert "VpcConfig" not in p and "ReservedConcurrentExecutions" not in p
    assert p["Timeout"] == load_config("beta").ingest["function_timeout_seconds"]
    env = p["Environment"]["Variables"]
    assert env["FINPLAN_ENV"] == "beta" and env["FINPLAN_BUDGET_STATE_PARAMETER"] == "/finplan/shared/financialplanning/config/budget-state"


def test_roles_are_platform_named_with_the_environment_boundary(templates: dict[str, Any]) -> None:
    roles = _of(templates["prod"], "AWS::IAM::Role")
    names = sorted(r["Properties"]["RoleName"] for r in roles)
    assert names == ["finplan-prod-financialplanning-daily-ingest-schedule-role", "finplan-prod-financialplanning-ingestion-handler-role"]
    for r in roles:
        assert "PermissionsBoundary" in r["Properties"]


def test_schedule_role_may_only_invoke_and_send_to_dlq(templates: dict[str, Any]) -> None:
    t = templates["beta"]
    role_id = next(k for k, r in t["Resources"].items() if r["Type"] == "AWS::IAM::Role" and r["Properties"]["RoleName"].endswith("daily-ingest-schedule-role"))
    actions: set[str] = set()
    for r in _of(t, "AWS::IAM::Policy"):
        if {"Ref": role_id} in r["Properties"]["Roles"]:
            for st in r["Properties"]["PolicyDocument"]["Statement"]:
                actions |= set(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
    assert actions <= {"lambda:InvokeFunction", "sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"}, actions


def test_every_taggable_resource_carries_cost_tags(templates: dict[str, Any]) -> None:
    for env, t in templates.items():
        for lid, r in t["Resources"].items():
            if r["Type"] in ("AWS::IAM::Policy", "AWS::SQS::QueuePolicy", "AWS::Scheduler::Schedule", "AWS::Lambda::EventInvokeConfig", "AWS::CDK::Metadata"):
                continue
            tags = r["Properties"].get("Tags")
            if tags is None:
                continue
            kv = {x["Key"]: x["Value"] for x in tags} if isinstance(tags, list) else dict(tags)
            for k in ("project", "owner-repo", "environment", "logical-role"):
                assert k in kv, (env, lid, k)
            assert kv["environment"] == env


def test_no_always_on_compute(templates: dict[str, Any]) -> None:
    forbidden = {"AWS::EC2::Instance", "AWS::EC2::NatGateway", "AWS::SageMaker::Endpoint", "AWS::ECS::Service", "AWS::RDS::DBInstance"}
    for t in templates.values():
        assert not forbidden & {r["Type"] for r in t["Resources"].values()}


# ------------------------------------------------------------------ ownership (OWN-01)
def test_ownership_check_passes(templates: dict[str, Any]) -> None:
    """Contracts 0.2.0: the ``ingestion-service`` and ``daily-scheduler`` rows list the role, log group,
    dead-letter queue and its policy, alarm and SSM references, so no problem is accepted."""
    problems = []
    for env, t in templates.items():
        report = check_template(t, "financialplanning", name=f"finplan-{env}-financialplanning-ingestion").to_dict()
        problems += [(env, p["logical_id"], p["resource_type"], p["message"]) for p in report["problems"]]
    assert problems == [], problems
