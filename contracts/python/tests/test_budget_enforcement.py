"""ENV-17 (allocation, pre-flight, Jev excluded) and ENV-19 (budget alerts and enforcement action:
synth assertions plus policy simulation) - tasks 13.3 (semantic part) and 13.4."""

from __future__ import annotations

import pytest

from finplan_contracts import budget
from finplan_contracts.boundaries import check_role_boundaries, concrete_boundary
from finplan_contracts.boundaries import env_permission_boundary as _env_permission_boundary
from finplan_contracts.budget import CostRecord, enforcement_deny_policy, preflight
from finplan_contracts.iam import EXPLICIT_DENY, Request, evaluate
from finplan_contracts.ssm import ENVIRONMENTS, REPOS, build
from finplan_contracts.validate import validate

from conftest import fixture


def env_permission_boundary(env):
    """The generated (CloudFormation-ready) boundary with placeholder pseudo parameters, for the offline evaluator."""
    return concrete_boundary(_env_permission_boundary(env))


ACCT = "<account-id>"
ALLOW_ALL = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}


# ----------------------------------------------------------------- ENV-17
def test_registered_defaults_are_valid_and_sum_to_the_ceiling():
    assert budget.DEFAULT_ALLOCATION == {"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5}
    assert budget.DEFAULT_CEILING_USD == 50
    assert budget.allocation_problems(budget.DEFAULT_ALLOCATION) == []
    assert set(budget.CATEGORIES) == {"platform_infra", "cpu_research", "bedrock_explanations", "gpu", "reserve"}


def test_allocation_above_ceiling_is_invalid_and_no_paid_work_starts():
    over = fixture("budget-allocation/invalid/sum-above-ceiling.json")
    assert budget.allocation_problems(over)
    for cat in budget.CATEGORIES:
        res = preflight(cat, 0.01, over)
        assert not res.allowed and res.error["code"] == "VALIDATION_FAILED"
        assert validate(res.error, "error").valid
    assert not preflight("gpu", 1, budget.DEFAULT_ALLOCATION, ceiling_usd=40).allowed


def test_exhausted_category_refuses_with_budget_exceeded():
    spend = [CostRecord(usd=6.0, category="cpu_research")]
    res = preflight("cpu_research", 2.5, budget.DEFAULT_ALLOCATION, spend, correlation_id="corr-synthetic-0002")
    assert not res.allowed
    err = res.error
    assert err["code"] == "BUDGET_EXCEEDED" and err["retryable"] is False
    assert err["details"]["budget_category"] == "cpu_research" and err["details"]["remaining_allocation_usd"] == pytest.approx(1.0)
    assert validate(err, "error").valid
    # the same job fits a fresh category
    assert preflight("cpu_research", 2.5, budget.DEFAULT_ALLOCATION).allowed
    # other categories are unaffected by cpu spend
    assert preflight("gpu", 20, budget.DEFAULT_ALLOCATION, spend).allowed


def test_jev_cost_is_not_counted():
    spend = [
        {"usd": 40.0, "category": None, "billed_by": "typesafe", "description": "synthetic Jev prepaid-credit usage"},
        {"usd": 1.0, "category": "gpu", "billed_by": "aws"},
    ]
    assert budget.aws_spend_by_category(spend)["gpu"] == 1.0
    assert sum(budget.aws_spend_by_category(spend).values()) == 1.0
    assert budget.third_party_spend(spend) == {"typesafe": 40.0}
    res = preflight("gpu", 24.0, budget.DEFAULT_ALLOCATION, spend)
    assert res.allowed and res.third_party_usd == {"typesafe": 40.0}
    assert "typesafe_jev" not in budget.CATEGORIES


def test_unregistered_category_and_enforced_state_refuse():
    assert preflight("typesafe_jev", 1, budget.DEFAULT_ALLOCATION).error["code"] == "VALIDATION_FAILED"
    res = preflight("platform_infra", 0.5, budget.DEFAULT_ALLOCATION, budget_state={"state": "enforced"})
    assert not res.allowed and res.error["code"] == "BUDGET_EXCEEDED"
    assert preflight("platform_infra", 0.5, budget.DEFAULT_ALLOCATION, budget_state='{"state": "ok"}').allowed


def test_preflight_cost_estimate_block_matches_contract():
    res = preflight("gpu", 4.5, budget.DEFAULT_ALLOCATION)
    block = {"estimated_usd_upper_bound": res.estimated_usd_upper_bound, "price_retrieved_at": "2026-01-10T09:00:00Z", "remaining_allocation_usd": res.remaining_allocation_usd, "budget_category": res.budget_category}
    assert validate(block, "cost-estimate").valid


def test_cli(tmp_path, capsys):
    assert budget.main(["check-allocation"]) == 0
    p = tmp_path / "over.json"
    p.write_text('{"platform_infra": 30, "gpu": 25}')
    assert budget.main(["check-allocation", str(p)]) == 1
    s = tmp_path / "spend.json"
    s.write_text('[{"usd": 7, "category": "cpu_research"}]')
    assert budget.main(["preflight", "--category", "cpu_research", "--estimate", "0.5", "--spend", str(s)]) == 1
    assert "BUDGET_EXCEEDED" in capsys.readouterr().out


# ----------------------------------------------------------------- ENV-19 synth assertions
@pytest.fixture(scope="module")
def tpl():
    return budget.budget_template()


def _of_type(tpl, t):
    return {k: v for k, v in tpl["Resources"].items() if v["Type"] == t}


def test_exactly_one_budget_tagged_shared_with_limit_from_parameter(tpl):
    budgets = _of_type(tpl, "AWS::Budgets::Budget")
    assert len(budgets) == 1
    (b,) = budgets.values()
    assert b["Properties"]["Budget"]["BudgetLimit"] == {"Amount": {"Ref": "CostCeilingUsd"}, "Unit": "USD"}
    assert tpl["Parameters"]["CostCeilingUsd"]["Default"] == "/finplan/shared/financialplanning/config/cost-ceiling-usd"
    tags = {t["Key"]: t["Value"] for t in b["Properties"]["ResourceTags"]}
    assert tags["environment"] == "shared" and tags["owner-repo"] == "financialplanning" and tags["project"] == "finplan"


def test_alerts_at_50_80_100_percent_of_actual_spend(tpl):
    (b,) = _of_type(tpl, "AWS::Budgets::Budget").values()
    notes = b["Properties"]["NotificationsWithSubscribers"]
    assert sorted(n["Notification"]["Threshold"] for n in notes) == [50, 80, 100]
    for n in notes:
        assert n["Notification"]["NotificationType"] == "ACTUAL" and n["Notification"]["ThresholdType"] == "PERCENTAGE"
        assert {"SubscriptionType": "EMAIL", "Address": {"Ref": "NotificationEmail"}} in n["Subscribers"]
    assert "Default" not in tpl["Parameters"]["NotificationEmail"]  # human-configured target


def test_deny_action_at_100_percent_on_published_enforced_role_names(tpl):
    actions = _of_type(tpl, "AWS::Budgets::BudgetsAction")
    assert len(actions) == 1
    (a,) = actions.values()
    p = a["Properties"]
    assert p["ActionThreshold"] == {"Type": "PERCENTAGE", "Value": 100}
    assert p["ActionType"] == "APPLY_IAM_POLICY" and p["NotificationType"] == "ACTUAL"
    assert p["Definition"]["IamActionDefinition"]["PolicyArn"] == {"Ref": "BudgetEnforcementDenyPolicy"}
    joined = p["Definition"]["IamActionDefinition"]["Roles"]["Fn::Split"][1]["Fn::Join"][1]
    refs = {j["Fn::Join"][1]["Ref"] for j in joined}
    defaults = {tpl["Parameters"][r]["Default"] for r in refs}
    assert defaults == {build(e, r, "config", "budget-enforced-role-names") for e in ENVIRONMENTS for r in REPOS}
    assert "/finplan/prod/financeagent/config/budget-enforced-role-names" in defaults  # FinanceAgent runtime role (Bedrock)
    for r in refs:
        assert tpl["Parameters"][r]["Type"] == "AWS::SSM::Parameter::Value<List<String>>"


def test_action_execution_role_has_shared_boundary(tpl):
    assert check_role_boundaries(tpl) == []


# ----------------------------------------------------------------- ENV-19 policy simulation
def _attached(identity=ALLOW_ALL):
    return {"identity": identity, "budget-deny": enforcement_deny_policy()}


@pytest.mark.parametrize(
    "action,resource",
    [
        ("sagemaker:CreateTrainingJob", f"arn:aws:sagemaker:us-east-2:{ACCT}:training-job/finplan-gamma-qwen-run"),
        ("sagemaker:CreateProcessingJob", f"arn:aws:sagemaker:us-east-2:{ACCT}:processing-job/finplan-gamma-backtest"),
        ("codepipeline:StartPipelineExecution", f"arn:aws:codepipeline:us-east-2:{ACCT}:finplan-shared-financemodel-pipeline"),
        ("bedrock:InvokeModel", "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-opus-5"),
        ("bedrock:InvokeModelWithResponseStream", f"arn:aws:bedrock:us-east-2:{ACCT}:inference-profile/us.anthropic.claude-opus-5"),
        ("codebuild:StartBuild", f"arn:aws:codebuild:us-east-2:{ACCT}:project/finplan-shared-financialplanning-build"),
    ],
)
def test_after_the_action_new_billable_work_is_denied(action, resource):
    before = evaluate(Request(action, resource), {"identity": ALLOW_ALL}, env_permission_boundary("gamma"))
    after = evaluate(Request(action, resource), _attached(), env_permission_boundary("gamma"))
    assert before.allowed
    assert after.decision == EXPLICIT_DENY


@pytest.mark.parametrize(
    "action,resource",
    [
        ("sagemaker:DescribeTrainingJob", f"arn:aws:sagemaker:us-east-2:{ACCT}:training-job/finplan-gamma-qwen-run"),
        ("codepipeline:GetPipelineState", f"arn:aws:codepipeline:us-east-2:{ACCT}:finplan-shared-financemodel-pipeline"),
        ("bedrock:ListFoundationModels", "*"),
        ("s3:GetObject", "arn:aws:s3:::finplan-gamma-financialplanning-snapshot-artifact-bucket-<sfx>/k"),
        ("ssm:GetParameter", f"arn:aws:ssm:us-east-2:{ACCT}:parameter/finplan/shared/financialplanning/config/budget-state"),
        ("dynamodb:GetItem", f"arn:aws:dynamodb:us-east-2:{ACCT}:table/finplan-gamma-financialplanning-plan-table"),
    ],
)
def test_after_the_action_reads_stay_allowed(action, resource):
    assert evaluate(Request(action, resource), _attached(), env_permission_boundary("gamma")).allowed


def test_only_a_human_removes_the_deny():
    r = evaluate(
        Request("iam:DetachRolePolicy", f"arn:aws:iam::{ACCT}:role/finplan-gamma-financeagent-runtime-role", {"iam:PolicyARN": f"arn:aws:iam::{ACCT}:policy/finplan-budget-enforcement-deny"}),
        {"identity": ALLOW_ALL},
        env_permission_boundary("gamma"),
    )
    assert r.decision == EXPLICIT_DENY
