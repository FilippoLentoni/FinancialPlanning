"""Beta modeling competition infrastructure (infra/stacks/modeling.py): beta only, names, schedules,
state machine definitions, grants and the reports-bucket view carve-out. Offline synth only."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from finplan_platform.core.config import load_config, validate_config
from infra.policy_sim import Principal
from infra.stacks.modeling import cron
from tests.unit.test_policy_simulation import WORST, policies, s3sim  # noqa: F401 - fixture

ENVS = ("beta", "gamma", "prod")
EXPERIMENT = "beta-competition-v1"
SCHED = "<aws.scheduler.scheduled-time>"
RETRY_SERVICE = ["Lambda.ClientExecutionTimeoutException", "Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"]


@pytest.fixture(scope="module")
def modeling_synth() -> dict[str, Any]:
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from infra.app import build_app

    app = build_app(cdk.App(), list(ENVS), env_modules=("infra.stacks.modeling",), app_modules=())
    out: dict[str, Any] = {"children": {}, "template": None}
    for env in ENVS:
        stage = app.node.find_child(env.capitalize())
        out["children"][env] = [c.node.id for c in stage.node.children]
        if env == "beta":
            stack = stage.node.find_child("Modeling")
            out["stack_name"] = stack.stack_name
            out["template"] = Template.from_stack(stack).to_json()
    return out


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        return "".join(_render(v) for v in value["Fn::Join"][1])
    if "Ref" in value:
        return "${" + value["Ref"] + "}"
    if "Fn::GetAtt" in value:
        return "${" + ".".join(value["Fn::GetAtt"]) + "}"
    if "Fn::ImportValue" in value:
        return "${import}"
    raise AssertionError(value)


def _of(t: dict[str, Any], rtype: str) -> dict[str, dict[str, Any]]:
    return {k: r for k, r in t["Resources"].items() if r["Type"] == rtype}


def _named(t: dict[str, Any], rtype: str, key: str) -> dict[str, dict[str, Any]]:
    return {r["Properties"][key]: r for r in _of(t, rtype).values()}


def _definition(t: dict[str, Any], name: str) -> dict[str, Any]:
    machine = _named(t, "AWS::StepFunctions::StateMachine", "StateMachineName")[name]
    return json.loads(_render(machine["Properties"]["DefinitionString"]))


# ------------------------------------------------------------------ beta only
def test_stack_exists_only_in_beta(modeling_synth: dict[str, Any]) -> None:
    assert "Modeling" in modeling_synth["children"]["beta"]
    assert modeling_synth["stack_name"] == "finplan-beta-financialplanning-modeling"
    for env in ("gamma", "prod"):
        assert "Modeling" not in modeling_synth["children"][env]
        assert load_config(env).modeling_competition is None


@pytest.mark.parametrize("env", ["gamma", "prod"])
def test_config_rejects_the_pipeline_outside_beta(env: str) -> None:
    doc = copy.deepcopy(load_config(env).data)
    doc["modeling_competition"] = copy.deepcopy(load_config("beta").data["modeling_competition"])
    assert any(p.pointer == "/modeling_competition" for p in validate_config(doc, env=env))


def test_config_requires_one_minute_per_schedule() -> None:
    doc = copy.deepcopy(load_config("beta").data)
    doc["modeling_competition"]["decision_times"] = ["09:45", "15:30"]
    assert any(p.pointer == "/modeling_competition/decision_times" for p in validate_config(doc, env="beta"))
    assert cron(["15:45", "09:45"]) == "cron(45 9,15 ? * MON-FRI *)"
    with pytest.raises(ValueError):
        cron(["09:45", "15:30"])


# ------------------------------------------------------------------ names
def test_resource_names_follow_the_platform_convention(modeling_synth: dict[str, Any]) -> None:
    t = modeling_synth["template"]
    functions = _named(t, "AWS::Lambda::Function", "FunctionName")
    assert set(functions) == {"finplan-beta-financialplanning-modeling-sources", "finplan-beta-financialplanning-modeling-report"}
    roles = {r["Properties"]["RoleName"] for r in _of(t, "AWS::IAM::Role").values()}
    assert roles == {f"finplan-beta-financialplanning-{n}-role" for n in ("modeling-sources", "modeling-report", "modeling-machine", "modeling-schedule")}
    assert set(_named(t, "AWS::StepFunctions::StateMachine", "StateMachineName")) == {"finplan-beta-financialplanning-modeling-decision", "finplan-beta-financialplanning-modeling-research"}
    for name, fn in functions.items():
        p = fn["Properties"]
        assert p["Runtime"] == "python3.12" and p["Architectures"] == ["arm64"] and p["Timeout"] == 900
        assert p["Environment"]["Variables"]["FINPLAN_ENV"] == "beta" and p["Environment"]["Variables"]["FINPLAN_MODELING_EXPERIMENT"] == EXPERIMENT
        assert "FINPLAN_BUCKET_REPORTS" in p["Environment"]["Variables"] and "VpcConfig" not in p
        assert p["Handler"] == "finplan_platform.handlers.modeling." + ("sources_handler" if name.endswith("sources") else "report_handler")
    assert functions["finplan-beta-financialplanning-modeling-report"]["Properties"]["MemorySize"] >= 3008
    logs = {r["Properties"]["LogGroupName"]: r["Properties"]["RetentionInDays"] for r in _of(t, "AWS::Logs::LogGroup").values()}
    assert logs == {f"/aws/lambda/{n}": 30 for n in functions}


def test_every_taggable_resource_has_an_ownership_matrix_role(modeling_synth: dict[str, Any]) -> None:
    from finplan_contracts.ownership import check_template

    report = check_template(modeling_synth["template"], "financialplanning", name="modeling").to_dict()
    assert report["problems"] == []
    for s in _of(modeling_synth["template"], "AWS::Scheduler::Schedule").values():
        assert s["Metadata"]["logical-role"] == "daily-ingest-schedule"


# ------------------------------------------------------------------ schedules
def test_schedules_new_york_weekdays_no_flexible_window(modeling_synth: dict[str, Any]) -> None:
    t = modeling_synth["template"]
    schedules = _named(t, "AWS::Scheduler::Schedule", "Name")
    expected = {
        "modeling-decision": ("cron(45 9,15 ? * MON-FRI *)", {"slot": SCHED}, "DecisionMachine"),
        "modeling-research": ("cron(0 9,15 ? * MON-FRI *)", {"slot": SCHED}, "ResearchMachine"),
        "modeling-eod": ("cron(0 17 ? * MON-FRI *)", {"mode": "eod", "experiment": EXPERIMENT, "scheduled_time": SCHED}, "ReportFunction"),
        "modeling-weekly": ("cron(25 17 ? * FRI *)", {"mode": "weekly", "experiment": EXPERIMENT, "scheduled_time": SCHED}, "ReportFunction"),
        "modeling-closed-week": ("cron(25 17 ? * FRI *)", {"mode": "closed_week", "experiment": EXPERIMENT, "scheduled_time": SCHED}, "ReportFunction"),
    }
    assert set(schedules) == {f"finplan-beta-financialplanning-{k}" for k in expected}
    for logical, (expression, payload, target) in expected.items():
        p = schedules[f"finplan-beta-financialplanning-{logical}"]["Properties"]
        assert p["ScheduleExpression"] == expression and p["ScheduleExpressionTimezone"] == "America/New_York"
        assert p["FlexibleTimeWindow"] == {"Mode": "OFF"} and p["State"] == "ENABLED"
        assert json.loads(p["Target"]["Input"]) == payload
        assert p["Target"]["RetryPolicy"] == {"MaximumEventAgeInSeconds": 1800, "MaximumRetryAttempts": 1}
        assert _render(p["Target"]["Arn"]).startswith("${" + target)


# ------------------------------------------------------------------ state machines
def test_decision_machine_definition(modeling_synth: dict[str, Any]) -> None:
    d = _definition(modeling_synth["template"], "finplan-beta-financialplanning-modeling-decision")
    s = d["States"]
    assert d["StartAt"] == "CollectQuotes" and s["CollectQuotes"]["Next"] == "RunModels" and s["RunModels"]["Next"] == "Report"
    assert s["CollectQuotes"]["Parameters"] == {"kind": "quotes", "slot.$": "$.slot", "experiment": EXPERIMENT, "symbols": ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA", "VOO"]}
    run = s["RunModels"]
    assert run["Resource"] == "arn:${AWS::Partition}:lambda:us-east-2:${AWS::AccountId}:function:finplan-beta-financemodel-forward-runner"
    assert run["Parameters"] == {"action": "run", "experiment": EXPERIMENT, "slot.$": "$.slot"}
    assert 900 <= run["TimeoutSeconds"] <= 1000
    for state in ("CollectQuotes", "RunModels", "Report"):
        assert [r["ErrorEquals"] for r in s[state]["Retry"]] == [RETRY_SERVICE]  # Lambda service errors only
    assert run["Catch"] == [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.models_error", "Next": "Report"}]
    assert s["Report"]["Parameters"] == {"mode": "current", "experiment": EXPERIMENT, "slot.$": "$.slot"}
    assert s["ModelsSucceeded"]["Choices"][0] == {"Variable": "$.models_error", "IsPresent": True, "Next": "RunModelsFailed"}
    assert s["RunModelsFailed"]["Type"] == "Fail" and s[s["ModelsSucceeded"]["Default"]]["Type"] == "Succeed"


def test_research_machine_definition(modeling_synth: dict[str, Any]) -> None:
    d = _definition(modeling_synth["template"], "finplan-beta-financialplanning-modeling-research")
    s = d["States"]
    assert d["StartAt"] == "CollectResearch" and s["CollectResearch"]["Next"] == "PrepareLLMPrompt" and s["PrepareLLMPrompt"]["End"] is True
    assert s["CollectResearch"]["Parameters"] == {"kind": "research", "slot.$": "$.slot", "experiment": EXPERIMENT}
    assert s["PrepareLLMPrompt"]["Parameters"] == {"action": "prepare_llm_prompt", "experiment": EXPERIMENT, "research_slot.$": "$.slot"}
    assert s["PrepareLLMPrompt"]["Resource"].endswith(":function:finplan-beta-financemodel-forward-runner")


def test_grants(modeling_synth: dict[str, Any]) -> None:
    t = modeling_synth["template"]
    roles = {lid: r["Properties"]["RoleName"] for lid, r in _of(t, "AWS::IAM::Role").items()}
    by_role: dict[str, list[dict[str, Any]]] = {}
    for pol in _of(t, "AWS::IAM::Policy").values():
        for ref in pol["Properties"]["Roles"]:
            by_role.setdefault(roles[ref["Ref"]], []).extend(pol["Properties"]["PolicyDocument"]["Statement"])

    def actions(role: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for st in by_role[f"finplan-beta-financialplanning-{role}-role"]:
            for a in st["Action"] if isinstance(st["Action"], list) else [st["Action"]]:
                out.setdefault(a, []).extend(_render(r) for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]))
        return out

    machine = actions("modeling-machine")
    assert set(machine) == {"lambda:InvokeFunction"}
    assert "arn:${AWS::Partition}:lambda:us-east-2:${AWS::AccountId}:function:finplan-beta-financemodel-forward-runner" in machine["lambda:InvokeFunction"]
    assert set(actions("modeling-schedule")) == {"states:StartExecution", "lambda:InvokeFunction"}
    sources, report = actions("modeling-sources"), actions("modeling-report")
    assert "dynamodb:PutItem" in sources and "dynamodb:PutItem" not in report
    for granted in (sources, report):
        assert "s3:PutObject" in granted and not any(a.startswith("dynamodb:DeleteItem") or "Stream" in a or "GetRecords" in a for a in granted)
    # the Beta stage's deployed test invokes the preflight through the function resource policy
    permissions = [r["Properties"] for r in _of(t, "AWS::Lambda::Permission").values()]
    assert len(permissions) == 2 and all(_render(p["Principal"]).endswith(":role/finplan-beta-financialplanning-operator-pipeline-stage") for p in permissions)


# ------------------------------------------------------------------ reports bucket views
def test_only_beta_report_views_are_overwritable(policies: dict[str, Any]) -> None:  # noqa: F811
    writer = Principal.role("finplan-beta-financialplanning-modeling-report-role", WORST)
    assert s3sim(policies, "s3:PutObject", "beta", "reports", "modeling-views/v1/beta-competition-v1/latest/report.html", writer)
    for key in ("modeling/v1/beta-competition-v1/forward/2026-10-19/abc/report.html", "modeling-evidence/v1/inputs/x/y.json", "modeling-views-other/x"):
        assert not s3sim(policies, "s3:PutObject", "beta", "reports", key, writer)
        assert s3sim(policies, "s3:PutObject", "beta", "reports", key, writer, **{"s3:if-none-match": "*"})
    assert not s3sim(policies, "s3:DeleteObject", "beta", "reports", "modeling-views/v1/x/latest/report.html", writer)
    for env in ("gamma", "prod"):
        other = Principal.role(f"finplan-{env}-financialplanning-modeling-report-role", WORST)
        assert not s3sim(policies, "s3:PutObject", env, "reports", "modeling-views/v1/x/latest/report.html", other)
