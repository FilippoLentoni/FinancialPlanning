"""Project budget, alerts, enforcement action and boundaries in the tooling stack (tasks 9.1, 9.2; COST-01, COST-02).

Template assertions on the synthesized tooling stack plus offline IAM policy simulations
(:func:`infra.policy_sim.simulate`, built on the contract evaluator).
"""

from __future__ import annotations

import json
from typing import Any

from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import budget as contract_budget

from infra.policy_sim import Principal, simulate
from infra.stacks.tooling import BUDGET_ACTION_LOGICAL_ID, BUDGET_NAME, WRITER_SOURCE
from tests.unit.ops.conftest import resources_of, tags_of

ALLOW_ALL = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}


def _one(template: dict[str, Any], rtype: str) -> tuple[str, dict[str, Any]]:
    found = resources_of(template, rtype)
    assert len(found) == 1, (rtype, list(found))
    return next(iter(found.items()))


# ------------------------------------------------------------------ COST-01
def test_budget_limit_comes_from_the_ceiling_parameter_COST_01(tooling_template: dict[str, Any]) -> None:
    _, budget = _one(tooling_template, "AWS::Budgets::Budget")
    data = budget["Properties"]["Budget"]
    assert data["BudgetName"] == BUDGET_NAME and data["BudgetType"] == "COST"
    ref = data["BudgetLimit"]["Amount"]["Ref"]
    param = tooling_template["Parameters"][ref]
    assert param["Type"] == "AWS::SSM::Parameter::Value<String>"
    assert param["Default"] == "/finplan/shared/financialplanning/config/cost-ceiling-usd"
    assert data["BudgetLimit"]["Unit"] == "USD"
    # scoped to the project tag only once a human activated it; whole account until then
    assert data["CostFilters"]["Fn::If"][0] == "ScopeToProjectTag"
    assert tooling_template["Parameters"]["ScopeBudgetToProjectTag"]["Default"] == "false"


def test_alerts_at_50_80_100_actual_and_100_forecast_to_the_topic_COST_01(tooling_template: dict[str, Any]) -> None:
    _, budget = _one(tooling_template, "AWS::Budgets::Budget")
    topic_id, _ = _one(tooling_template, "AWS::SNS::Topic")
    seen = set()
    for n in budget["Properties"]["NotificationsWithSubscribers"]:
        note = n["Notification"]
        assert note["ThresholdType"] == "PERCENTAGE" and note["ComparisonOperator"] == "GREATER_THAN"
        assert n["Subscribers"] == [{"SubscriptionType": "SNS", "Address": {"Ref": topic_id}}]
        seen.add((note["NotificationType"], note["Threshold"]))
    assert seen == {("ACTUAL", 50), ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100)}


def test_eighty_percent_alerts_without_deny_COST_01(tooling_template: dict[str, Any]) -> None:
    """The 80% alert is a notification only: the only deny is the action, at 100% of ACTUAL spend."""
    _, action = _one(tooling_template, "AWS::Budgets::BudgetsAction")
    p = action["Properties"]
    assert p["NotificationType"] == "ACTUAL" and p["ActionThreshold"] == {"Type": "PERCENTAGE", "Value": 100}
    assert len(resources_of(tooling_template, "AWS::Budgets::BudgetsAction")) == 1


def test_notification_target_is_set_by_a_human_never_committed_COST_01(tooling_template: dict[str, Any]) -> None:
    param = tooling_template["Parameters"]["NotificationEmail"]
    assert param["Default"] == ""
    subs = [r for r in resources_of(tooling_template, "AWS::SNS::Subscription").values() if r["Properties"]["Protocol"] == "email"]
    assert len(subs) == 1 and subs[0]["Properties"]["Endpoint"] == {"Ref": "NotificationEmail"}
    assert subs[0]["Condition"] == "HasNotificationEmail"
    # budgets may publish to the topic (same account only)
    _, policy = _one(tooling_template, "AWS::SNS::TopicPolicy")
    st = policy["Properties"]["PolicyDocument"]["Statement"][0]
    assert st["Principal"] == {"Service": "budgets.amazonaws.com"} and st["Action"] == "sns:Publish"
    assert "aws:SourceAccount" in json.dumps(st["Condition"])


def test_no_ceiling_literal_in_the_template_COST_01(tooling_template: dict[str, Any]) -> None:
    """Ceiling raised -> the limit follows the parameter; no file holds the value except config/shared.json."""
    _, budget = _one(tooling_template, "AWS::Budgets::Budget")
    assert isinstance(budget["Properties"]["Budget"]["BudgetLimit"]["Amount"], dict)


# ------------------------------------------------------------------ COST-02
def _action_roles(template: dict[str, Any], resolver: Any) -> tuple[list[str], dict[str, Any]]:
    _, action = _one(template, "AWS::Budgets::BudgetsAction")
    roles = action["Properties"]["Definition"]["IamActionDefinition"]["Roles"]
    cond, with_extra, own = roles["Fn::If"]
    assert cond == "HasAdditionalEnforcedRoles"
    return [resolver.resolve(r, "tooling") for r in own], with_extra


def test_budget_action_targets_the_tooling_roles_and_published_names_COST_02(tooling_template: dict[str, Any], tooling_resolver: Any) -> None:
    own, with_extra = _action_roles(tooling_template, tooling_resolver)
    created = {tooling_resolver.resolve({"Ref": lid}, "tooling") for lid in resources_of(tooling_template, "AWS::IAM::Role")}
    assert set(own) <= created
    expected = {
        "finplan-shared-financialplanning-pipeline-role",
        "finplan-shared-financialplanning-pipeline-build-project-role",
        *(f"finplan-shared-financialplanning-deploy-role-{e}" for e in ("beta", "gamma", "prod")),
        *(f"finplan-shared-financialplanning-deploy-role-{e}-exec" for e in ("beta", "gamma", "prod")),
        *(f"finplan-{e}-financialplanning-operator-pipeline-stage" for e in ("beta", "gamma", "prod")),
    }
    assert set(own) == expected
    # the budget-action and budget-state-writer roles are never denied (the cap must keep working)
    assert not any("budget" in r for r in own)
    # published role names (other repositories, platform runtime roles) come from the bootstrap parameter
    assert "AdditionalEnforcedRoleNames" in json.dumps(with_extra)
    assert tooling_template["Parameters"]["AdditionalEnforcedRoleNames"]["Type"] == "CommaDelimitedList"


def _deny_doc(template: dict[str, Any]) -> dict[str, Any]:
    pols = {r["Properties"]["ManagedPolicyName"]: r for r in resources_of(template, "AWS::IAM::ManagedPolicy").values()}
    return pols[contract_boundaries.BUDGET_DENY_POLICY_NAME]["Properties"]["PolicyDocument"]


def test_deny_policy_is_the_contract_policy_COST_02(tooling_template: dict[str, Any]) -> None:
    assert _deny_doc(tooling_template) == contract_budget.enforcement_deny_policy()


def test_cap_reached_denies_paid_work_but_not_reads_COST_02(tooling_template: dict[str, Any]) -> None:
    deny = _deny_doc(tooling_template)
    capped = Principal.role("finplan-shared-financialplanning-deploy-role-beta", ALLOW_ALL, deny)
    for action, resource in [
        ("sagemaker:CreateTrainingJob", "*"),
        ("sagemaker:CreateEndpoint", "*"),
        ("bedrock:InvokeModel", "*"),
        ("bedrock:InvokeModelWithResponseStream", "*"),
        ("codepipeline:StartPipelineExecution", "*"),
        ("codebuild:StartBuild", "*"),
    ]:
        assert not simulate(action, resource, capped).allowed, action
    for action, resource in [
        ("execute-api:Invoke", "arn:aws:execute-api:us-east-2:<account-id>:abc/live/GET/v1/plan-versions/x"),
        ("dynamodb:GetItem", "arn:aws:dynamodb:us-east-2:<account-id>:table/finplan-beta-financialplanning-plan-version"),
        ("s3:GetObject", "arn:aws:s3:::finplan-beta-financialplanning-plans-<account-id>/k"),
        ("lambda:InvokeFunction", "arn:aws:lambda:us-east-2:<account-id>:function:finplan-beta-financialplanning-ingestion-handler"),
    ]:
        assert simulate(action, resource, capped).allowed, action


def test_bootstrap_identity_is_unaffected_COST_02(tooling_template: dict[str, Any], tooling_resolver: Any) -> None:
    own, _ = _action_roles(tooling_template, tooling_resolver)
    bootstrap = Principal("arn:aws:iam::<account-id>:root", (ALLOW_ALL,))
    assert simulate("codepipeline:StartPipelineExecution", "*", bootstrap).allowed
    assert all(not r.endswith(":root") and "bootstrap" not in r for r in own)


def test_only_a_human_removes_the_deny_COST_02(tooling_template: dict[str, Any], tooling_resolver: Any) -> None:
    deny_arn = "arn:aws:iam::<account-id>:policy/" + contract_boundaries.BUDGET_DENY_POLICY_NAME
    roles = {tooling_resolver.resolve({"Ref": lid}, "tooling"): lid for lid in resources_of(tooling_template, "AWS::IAM::Role")}
    # the resolver renders a managed policy Ref as its logical ID; substitute the policy ARN
    deny_lid = next(lid for lid, r in resources_of(tooling_template, "AWS::IAM::ManagedPolicy").items() if r["Properties"]["ManagedPolicyName"] == contract_boundaries.BUDGET_DENY_POLICY_NAME)
    docs = [json.loads(json.dumps(d).replace(f'"{deny_lid}"', json.dumps(deny_arn))) for d in tooling_resolver.role_policies("finplan-shared-financialplanning-budget-action-role")]
    action_role = Principal.role("finplan-shared-financialplanning-budget-action-role", *docs, boundary=contract_boundaries.shared_permission_boundary())
    target = "arn:aws:iam::<account-id>:role/finplan-shared-financialplanning-pipeline-role"
    assert simulate("iam:AttachRolePolicy", target, action_role, context={"iam:PolicyARN": deny_arn}).allowed
    assert not simulate("iam:AttachRolePolicy", target, action_role, context={"iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess"}).allowed
    # contracts 0.2.2 (D15, incident 2026-10-07): the action role itself may detach the deny policy, so
    # AWS Budgets can reset or reverse its action; it still cannot detach any other policy
    assert simulate("iam:DetachRolePolicy", target, action_role, context={"iam:PolicyARN": deny_arn}).allowed
    assert not simulate("iam:DetachRolePolicy", target, action_role, context={"iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess"}).allowed
    # every other principal under a boundary is denied the detach, so automation never lifts the cap
    for name, lid in roles.items():
        if name == "finplan-shared-financialplanning-budget-action-role":
            continue
        env = tags_of(tooling_template["Resources"][lid])["environment"]
        boundary = contract_boundaries.shared_permission_boundary() if env == "shared" else contract_boundaries.env_permission_boundary(env)
        other = Principal.role(name, ALLOW_ALL, boundary=boundary)
        assert not simulate("iam:DetachRolePolicy", target, other, context={"iam:PolicyARN": deny_arn}).allowed, name
    research = Principal.role("finplan-beta-financemodel-research-role", ALLOW_ALL, boundary=contract_boundaries.research_permission_boundary("beta"))
    assert not simulate("iam:DetachRolePolicy", target, research, context={"iam:PolicyARN": deny_arn}).allowed
    # nor may a principal execute (reverse) the budget action: lifting the cap is a human decision
    assert not simulate("budgets:ExecuteBudgetAction", "*", action_role).allowed
    assert "finplan-shared-financialplanning-budget-action-role" in roles
    _, action = _one(tooling_template, "AWS::Budgets::BudgetsAction")
    assert action["Properties"]["ApprovalModel"] == "AUTOMATIC"


def test_budget_action_is_replaced_after_the_reset_failure_COST_02(tooling_template: dict[str, Any]) -> None:
    """Incident 2026-10-07: the first action is stuck in RESET_FAILURE. The V2 logical ID makes the next
    bootstrap replace it with a fresh action in STANDBY (CloudFormation deletes the old one)."""
    actions = resources_of(tooling_template, "AWS::Budgets::BudgetsAction")
    assert list(actions) == [BUDGET_ACTION_LOGICAL_ID] == ["BudgetEnforcementActionV2"]
    assert "BudgetEnforcementAction" not in tooling_template["Resources"]


def test_boundary_detach_exemption_names_only_the_budget_action_role_COST_02(tooling_template: dict[str, Any], tooling_resolver: Any) -> None:
    """The only aws:PrincipalArn exemption in any synthesized boundary is the shared boundary's
    detach of the deny policy, and its pattern matches the budget action role and no other role."""
    pols = {r["Properties"]["ManagedPolicyName"]: r["Properties"]["PolicyDocument"] for r in resources_of(tooling_template, "AWS::IAM::ManagedPolicy").values()}
    exempting = {name: doc for name, doc in pols.items() if "aws:PrincipalArn" in json.dumps(doc)}
    assert list(exempting) == [contract_boundaries.boundary_name("shared")]
    stmts = [s for s in exempting[contract_boundaries.boundary_name("shared")]["Statement"] if "aws:PrincipalArn" in json.dumps(s)]
    assert len(stmts) == 1
    (stmt,) = stmts
    assert stmt["Effect"] == "Deny" and stmt["Action"] == ["iam:DetachRolePolicy"]
    assert stmt["Condition"]["ArnLike"]["iam:PolicyARN"].endswith(":policy/" + contract_boundaries.BUDGET_DENY_POLICY_NAME)
    pattern = stmt["Condition"]["ArnNotLike"]["aws:PrincipalArn"]
    assert pattern == {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/finplan-shared-*-budget-action-role"}
    from finplan_contracts.iam import policy_glob_matches

    rendered = pattern["Fn::Sub"].replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", "<account-id>")
    names = [tooling_resolver.resolve({"Ref": lid}, "tooling") for lid in resources_of(tooling_template, "AWS::IAM::Role")]
    matched = [n for n in names if policy_glob_matches(rendered, f"arn:aws:iam::<account-id>:role/{n}")]
    assert matched == ["finplan-shared-financialplanning-budget-action-role"]
    # the action uses that role
    _, action = _one(tooling_template, "AWS::Budgets::BudgetsAction")
    role_lid = action["Properties"]["ExecutionRoleArn"]["Fn::GetAtt"][0]
    assert tooling_resolver.resolve({"Ref": role_lid}, "tooling") == "finplan-shared-financialplanning-budget-action-role"


def test_budget_state_writer_writes_only_the_flag_COST_02(tooling_template: dict[str, Any], tooling_resolver: Any) -> None:
    name = "finplan-shared-financialplanning-budget-state-writer-role"
    writer = Principal.role(name, *tooling_resolver.role_policies(name), boundary=contract_boundaries.shared_permission_boundary())
    param = "arn:aws:ssm:us-east-2:<account-id>:parameter/finplan/shared/financialplanning/config/"
    assert simulate("ssm:PutParameter", param + "budget-state", writer).allowed
    for other in ("budget-allocation", "cost-ceiling-usd"):
        assert not simulate("ssm:PutParameter", param + other, writer).allowed
    fns = resources_of(tooling_template, "AWS::Lambda::Function")
    (fn,) = fns.values()
    assert fn["Properties"]["Code"]["ZipFile"] == WRITER_SOURCE.read_text(encoding="utf-8")
    assert fn["Properties"]["Environment"]["Variables"]["FINPLAN_BUDGET_STATE_PARAMETER"] == contract_budget.STATE_PARAMETER
    subs = [r for r in resources_of(tooling_template, "AWS::SNS::Subscription").values() if r["Properties"]["Protocol"] == "lambda"]
    assert len(subs) == 1


# ------------------------------------------------------------------ boundaries and shared tagging
def test_permission_boundaries_are_the_contract_documents(tooling_template: dict[str, Any]) -> None:
    pols = {r["Properties"]["ManagedPolicyName"]: r["Properties"]["PolicyDocument"] for r in resources_of(tooling_template, "AWS::IAM::ManagedPolicy").values()}
    for env in ("beta", "gamma", "prod"):
        assert pols[contract_boundaries.boundary_name(env)] == contract_boundaries.env_permission_boundary(env)
        assert pols[contract_boundaries.research_boundary_name(env)] == contract_boundaries.research_permission_boundary(env)
    assert pols[contract_boundaries.boundary_name("shared")] == contract_boundaries.shared_permission_boundary()


def test_every_tooling_role_carries_its_environment_boundary_and_tags(tooling_template: dict[str, Any]) -> None:
    """Account-level roles: shared tag and boundary. Per-environment pipeline roles (deploy, CloudFormation
    execution, stage): their environment's tag and boundary (contracts D13, ENV-21; PIPE-08)."""
    lid_of = {r["Properties"]["ManagedPolicyName"]: lid for lid, r in resources_of(tooling_template, "AWS::IAM::ManagedPolicy").items()}
    per_env = {"beta": 0, "gamma": 0, "prod": 0}
    for lid, role in resources_of(tooling_template, "AWS::IAM::Role").items():
        tags = tags_of(role)
        env = tags["environment"]
        assert role["Properties"]["PermissionsBoundary"] == {"Ref": lid_of[contract_boundaries.boundary_name(env)]}, lid
        assert tags["owner-repo"] == "financialplanning" and tags["project"] == "finplan" and tags["logical-role"], lid
        if env != "shared":
            per_env[env] += 1
            name = role["Properties"]["RoleName"]
            assert tags["logical-role"] in {"deploy-role", "pipeline-role"} and env in name, (lid, name)
    assert per_env == {"beta": 3, "gamma": 3, "prod": 3}  # deploy, exec and stage role per environment
