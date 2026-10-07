"""ENV-03 / ENV-04 / ENV-05 (policy simulation), ENV-18 (boundary check and cross-env deny), ENV-16
(shared resources hold no environment data) and the generated guardrail templates (tasks 9.1, 9.3, 12.3, 13.1)."""

from __future__ import annotations

import json
import re

import pytest

from finplan_contracts import boundaries
from finplan_contracts.boundaries import (
    check_role_boundaries,
    check_shared_resources,
)
from finplan_contracts.iam import EXPLICIT_DENY, Request, evaluate

from conftest import CONTRACTS
from infra_helpers import concrete, infra, infra_files


# The generated boundaries are CloudFormation-ready (``Fn::Sub`` with pseudo parameters); the offline
# evaluator simulates them with placeholder values (partition aws, us-east-2, <account-id>).
def env_permission_boundary(env):
    return concrete(boundaries.env_permission_boundary(env))


def research_permission_boundary(env):
    return concrete(boundaries.research_permission_boundary(env))


def shared_permission_boundary():
    return concrete(boundaries.shared_permission_boundary())

ALLOW_ALL = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
ACCT = "<account-id>"


def sim(action, resource, boundary, identity=ALLOW_ALL, **ctx):
    return evaluate(Request(action, resource, ctx), [identity], boundary)


# ----------------------------------------------------------------- ENV-03 / ENV-18 cross-environment
@pytest.mark.parametrize("env,other", [("beta", "prod"), ("gamma", "prod"), ("beta", "gamma"), ("prod", "gamma")])
def test_role_acting_on_other_environment_tagged_resource_is_denied(env, other):
    b = env_permission_boundary(env)
    r = sim("s3:GetObject", "arn:aws:s3:::some-data-bucket-<sfx>/k", b, **{"aws:ResourceTag/environment": other})
    assert r.decision == EXPLICIT_DENY and any("DenyOtherEnvironmentTagged" in s for s in r.matched_deny)


def test_gamma_role_reading_prod_platform_bucket_by_name_is_denied():
    b = env_permission_boundary("gamma")
    r = sim("s3:GetObject", "arn:aws:s3:::finplan-prod-financialplanning-plan-artifact-bucket-x-<sfx>/plans/p.json", b)
    assert r.decision == EXPLICIT_DENY
    assert not sim("lambda:InvokeFunction", f"arn:aws:lambda:us-east-2:{ACCT}:function:finplan-prod-financelambdastool-get-plan", b).allowed
    assert not sim("ssm:GetParameter", f"arn:aws:ssm:us-east-2:{ACCT}:parameter/finplan/prod/financialplanning/api/plan-endpoint", b).allowed
    assert not sim("dynamodb:PutItem", f"arn:aws:dynamodb:us-east-2:{ACCT}:table/finplan-prod-financialplanning-plan-table", b).allowed


def test_same_environment_and_shared_resources_allowed():
    b = env_permission_boundary("gamma")
    assert sim("s3:GetObject", "arn:aws:s3:::finplan-gamma-financialplanning-snapshot-artifact-bucket-<sfx>/k", b, **{"aws:ResourceTag/environment": "gamma"}).allowed
    assert sim("ssm:GetParameter", f"arn:aws:ssm:us-east-2:{ACCT}:parameter/finplan/shared/financialplanning/contract/registry-ref", b).allowed
    assert sim("ecr:BatchGetImage", f"arn:aws:ecr:us-east-2:{ACCT}:repository/finplan-shared-financemodel-image", b, **{"aws:ResourceTag/environment": "shared"}).allowed
    assert sim("sts:GetCallerIdentity", "*", b).allowed  # untagged, unnamed: allowed


def test_gamma_gateway_cannot_invoke_prod_tool_lambda():
    b = env_permission_boundary("gamma")
    r = sim("lambda:InvokeFunction", f"arn:aws:lambda:us-east-2:{ACCT}:function:finplan-prod-financelambdastool-refresh-market-data", b, **{"aws:ResourceTag/environment": "prod"})
    assert r.decision == EXPLICIT_DENY


# ----------------------------------------------------------------- ENV-04 research roles
@pytest.fixture
def research():
    return research_permission_boundary("gamma")


def test_research_role_cannot_write_publication_table(research):
    for action in ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems", "dynamodb:GetItem"):
        r = sim(action, f"arn:aws:dynamodb:us-east-2:{ACCT}:table/finplan-gamma-financialplanning-publication-table", research)
        assert r.decision == EXPLICIT_DENY, action
    tagged = sim("dynamodb:PutItem", f"arn:aws:dynamodb:us-east-2:{ACCT}:table/anything", research, **{"aws:ResourceTag/owner-repo": "financialplanning", "aws:ResourceTag/environment": "gamma"})
    assert tagged.decision == EXPLICIT_DENY


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "v1/plans/pl_X/versions"),
        ("POST", "v1/plans/pl_X/publications"),
        ("POST", "v1/publications/pub_X/executions"),
        ("PUT", "v1/plans/pl_X/head"),
        ("POST", "v1/plans/pl_X/staged-outputs/run_X/accept"),
        ("DELETE", "v1/portfolios/pf_X"),
    ],
)
def test_research_role_cannot_write_plan_publication_or_execution_api(research, method, path):
    r = sim("execute-api:Invoke", f"arn:aws:execute-api:us-east-2:{ACCT}:abc123/gamma/{method}/{path}", research)
    assert r.decision == EXPLICIT_DENY


def test_research_role_may_read_staged_output_outcome_and_snapshots(research):
    assert sim("execute-api:Invoke", f"arn:aws:execute-api:us-east-2:{ACCT}:abc123/gamma/GET/v1/staged-outputs/run_X", research).allowed
    assert sim("s3:GetObject", "arn:aws:s3:::finplan-gamma-financialplanning-snapshot-artifact-bucket-<sfx>/snap_X/manifest.json", research, **{"aws:ResourceTag/environment": "gamma"}).allowed


def test_research_role_writes_only_research_storage_and_staging(research):
    assert sim("s3:PutObject", "arn:aws:s3:::finplan-gamma-financialplanning-run-staging-area-<sfx>/run_X/manifest.json", research).allowed
    assert sim("s3:PutObject", "arn:aws:s3:::finplan-gamma-financemodel-research-workspace-bucket-<sfx>/x", research).allowed
    assert sim("s3:PutObject", "arn:aws:s3:::finplan-gamma-financialplanning-plan-artifact-bucket-<sfx>/x", research).decision == EXPLICIT_DENY
    assert sim("s3:PutObject", "arn:aws:s3:::finplan-gamma-financialplanning-snapshot-artifact-bucket-<sfx>/x", research).decision == EXPLICIT_DENY
    assert sim("s3:PutObject", "arn:aws:s3:::finplan-prod-financialplanning-run-staging-area-<sfx>/x", research).decision == EXPLICIT_DENY


# ----------------------------------------------------------------- ENV-05 live-financial deny
@pytest.mark.parametrize("boundary", [env_permission_boundary("beta"), research_permission_boundary("prod"), shared_permission_boundary()], ids=["env", "research", "shared"])
def test_live_financial_actions_and_secrets_denied(boundary):
    for action in ("payments:CreatePayment", "managedblockchain:CreateAccessor", "bedrock-agentcore:CreatePaymentSession", "bedrock-agentcore:SpendFromWallet", "coinbase:PlaceOrder"):
        assert sim(action, "*", boundary).decision == EXPLICIT_DENY, action
    sm = f"arn:aws:secretsmanager:us-east-2:{ACCT}:secret:"
    for secret_id in ("finplan/beta/financeagent/coinbase-api-key", sm + "finplan/prod/x/brokerage-credentials-AbCdEf", "finplan/gamma/x/wallet-seed"):
        assert sim("secretsmanager:GetSecretValue", sm + "x", boundary, **{"secretsmanager:SecretId": secret_id}).decision == EXPLICIT_DENY, secret_id
    assert sim("secretsmanager:CreateSecret", sm + "x", boundary, **{"secretsmanager:Name": "finplan/beta/x/trading-key"}).decision == EXPLICIT_DENY
    # the Jev API key is not a live-financial credential
    jev = "finplan/shared/financemodel/jev-api-key"
    assert sim("secretsmanager:GetSecretValue", sm + jev + "-AbCdEf", boundary, **{"secretsmanager:SecretId": jev}).allowed


def test_boundaries_cover_the_live_permission_scan_catalogue():
    live_perms = pytest.importorskip("finplan_contracts.live_perms")
    for b in (env_permission_boundary("gamma"), research_permission_boundary("beta"), shared_permission_boundary()):
        for action in live_perms.LIVE_ACTION_CATALOGUE:
            assert sim(action, "*", b).decision == EXPLICIT_DENY, action
    count, findings = live_perms.scan_paths([CONTRACTS / "templates"])
    assert count >= 12 and findings == [], [str(f) for f in findings]


def test_boundaries_protect_themselves_and_the_budget_deny():
    b = env_permission_boundary("beta")
    assert not sim("iam:DeleteRolePermissionsBoundary", f"arn:aws:iam::{ACCT}:role/r", b).allowed
    assert not sim("iam:CreateRole", f"arn:aws:iam::{ACCT}:role/r", b).allowed  # no boundary in the request
    assert sim("iam:CreateRole", f"arn:aws:iam::{ACCT}:role/r", b, **{"iam:PermissionsBoundary": f"arn:aws:iam::{ACCT}:policy/finplan-beta-permission-boundary"}).allowed
    assert not sim("iam:CreateRole", f"arn:aws:iam::{ACCT}:role/r", b, **{"iam:PermissionsBoundary": f"arn:aws:iam::{ACCT}:policy/finplan-prod-permission-boundary"}).allowed
    assert not sim("iam:DetachRolePolicy", f"arn:aws:iam::{ACCT}:role/r", b, **{"iam:PolicyARN": f"arn:aws:iam::{ACCT}:policy/finplan-budget-enforcement-deny"}).allowed
    assert not sim("budgets:ExecuteBudgetAction", "*", b).allowed


# ----------------------------------------------------------------- ENV-18 static check
def test_role_without_environment_boundary_fails_and_is_named():
    findings = check_role_boundaries(infra("roles/invalid/role-without-boundary.json"))
    assert len(findings) == 1 and findings[0].resource == "PlanApiHandlerRole" and "no environment permission boundary" in findings[0].message


def test_role_with_another_environment_boundary_fails():
    findings = check_role_boundaries(infra("roles/invalid/role-with-other-environment-boundary.json"))
    assert findings and findings[0].resource == "PlanApiHandlerRole"


@pytest.mark.parametrize("path", infra_files("roles/valid"), ids=lambda p: p.name)
def test_valid_role_templates_pass(path):
    assert check_role_boundaries(json.loads(path.read_text())) == []


def test_pipeline_and_tooling_templates_pass_the_boundary_check():
    assert check_role_boundaries(infra("pipelines/valid/standard.json")) == []
    assert check_role_boundaries(infra("assembly/FinplanToolingStack.template.json")) == []


def test_untagged_role_with_unknown_environment_fails():
    tpl = infra("roles/valid/role-with-environment-boundary.json")
    tpl["Resources"]["PlanApiHandlerRole"]["Properties"]["Tags"] = []
    assert check_role_boundaries(tpl)
    assert check_role_boundaries(tpl, environment="beta") == []


# ----------------------------------------------------------------- ENV-16 shared resources
@pytest.mark.parametrize("path", infra_files("shared/invalid"), ids=lambda p: p.name)
def test_environment_data_in_shared_resource_fails(path):
    assert check_shared_resources(json.loads(path.read_text())), path.name


@pytest.mark.parametrize("path", infra_files("shared/valid"), ids=lambda p: p.name)
def test_valid_shared_and_environment_resources_pass(path):
    assert check_shared_resources(json.loads(path.read_text())) == []


def test_jev_secret_accepted_as_shared_credential_and_not_declared():
    matrix = boundaries.load_matrix()
    row = next(r for r in matrix["resources"] if r["id"] == "jev-api-key-secret")
    assert row["scope"] == "shared" and row["owner"] == "financemodel" and row["iac_declared"] is False and row["environment_data"] is False
    assert matrix["shared_rows"]["jev-api-key-secret"] == "third-party-credential-secret"
    findings = check_shared_resources(infra("shared/invalid/jev-secret-declared.json"))
    assert any(f.check == "iac-not-declared" and "jev-api-key-secret" in f.message for f in findings)


def test_tooling_and_pipeline_stacks_hold_no_environment_data():
    assert check_shared_resources(infra("pipelines/valid/standard.json")) == []
    tooling = infra("assembly/FinplanToolingStack.template.json")
    assert check_shared_resources(tooling) == []


# ----------------------------------------------------------------- templates
def test_committed_templates_are_up_to_date():
    docs = boundaries.all_templates()
    tdir = CONTRACTS / "templates"
    for name, doc in docs.items():
        assert json.loads((tdir / name).read_text()) == doc, f"{name} is stale: run python -m finplan_contracts.boundaries"


def test_templates_contain_no_account_identifiers():
    for p in sorted((CONTRACTS / "templates").glob("*.json")):
        text = p.read_text()
        assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), p.name
        assert not re.search(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:", text), p.name


def test_boundary_names_and_naming_convention():
    assert boundaries.boundary_name("beta") == "finplan-beta-permission-boundary"
    assert boundaries.research_boundary_name("prod") == "finplan-prod-research-permission-boundary"
    assert boundaries.resource_name("gamma", "financialplanning", "plan-table") == "finplan-gamma-financialplanning-plan-table"
    with pytest.raises(ValueError):
        boundaries.boundary_name("dev")


# ----------------------------------------------------------------- 0.2.0 (design D13): deployable boundaries
#: Resource-ARN pattern CloudFormation validation (cfn-lint E3510) applies to IAM policy resources: a
#: concrete service segment, wildcards allowed elsewhere. ``arn:*:*:*:*:*finplan-<env>-*`` fails it.
E3510 = re.compile(r"^(arn:(aws[A-Za-z\-]*?|[A-Za-z?*\-]*[?*][A-Za-z?*\-]*):[^:*?]+:[^:]*(:(?:\d{12}|\*|aws)?:.+|)|\*)$")
ALL_BOUNDARIES = [(f"env-{e}", lambda e=e: boundaries.env_permission_boundary(e)) for e in boundaries.ENVIRONMENTS] + [
    (f"research-{e}", lambda e=e: boundaries.research_permission_boundary(e)) for e in boundaries.ENVIRONMENTS
] + [("shared", boundaries.shared_permission_boundary)]


def _resources(doc):
    for st in doc["Statement"]:
        for key in ("Resource", "NotResource"):
            vals = st.get(key)
            if vals is None:
                continue
            yield st.get("Sid"), vals if isinstance(vals, list) else [vals]


@pytest.mark.parametrize("name,make", ALL_BOUNDARIES, ids=[n for n, _ in ALL_BOUNDARIES])
def test_boundary_resources_are_valid_per_service_arns(name, make):
    """E3510: every resource of every boundary is '*' or an ARN with a concrete service; pseudo parameters
    are written as Fn::Sub in the CloudFormation-ready document, never as account values."""
    doc = make()
    for sid, vals in _resources(doc):
        for v in vals:
            if isinstance(v, dict):
                assert set(v) == {"Fn::Sub"} and "${AWS::Partition}" in v["Fn::Sub"], (sid, v)
                assert not re.search(r"[0-9]{12}", v["Fn::Sub"])
            else:
                assert "${AWS::" not in v, (sid, v)
        for v in concrete_boundary_doc(doc, account="4" * 12):  # a 12-digit stand-in, built at run time (leak scan)
            assert E3510.match(v), (name, sid, v)
    assert "arn:*:*:*:*:" not in json.dumps(doc)


def concrete_boundary_doc(doc, **kw):
    out = []
    for _, vals in _resources(boundaries.concrete_boundary(doc, **kw)):
        out += vals
    return out


@pytest.mark.parametrize("name,make", ALL_BOUNDARIES, ids=[n for n, _ in ALL_BOUNDARIES])
def test_boundaries_fit_the_iam_managed_policy_size_limit(name, make):
    """A boundary that exceeds IAM's managed-policy size fails at deploy; check it with the longest region names."""
    for region in ("us-east-2", "ap-southeast-2"):
        assert boundaries.policy_size(make(), region=region) < boundaries.MANAGED_POLICY_MAX_CHARS, (name, region)


@pytest.mark.parametrize(
    "action,resource",
    [
        ("sqs:SendMessage", f"arn:aws:sqs:us-east-2:{ACCT}:finplan-prod-financialplanning-ingest-dlq"),
        ("sns:Publish", f"arn:aws:sns:us-east-2:{ACCT}:finplan-prod-financeagent-alerts"),
        ("logs:GetLogEvents", f"arn:aws:logs:us-east-2:{ACCT}:log-group:/aws/lambda/finplan-prod-financialplanning-plan-api:log-stream:x"),
        ("events:PutRule", f"arn:aws:events:us-east-2:{ACCT}:rule/finplan-prod-financemodel-state-change"),
        ("scheduler:UpdateSchedule", f"arn:aws:scheduler:us-east-2:{ACCT}:schedule/default/finplan-prod-financialplanning-daily-ingest"),
        ("states:StartExecution", f"arn:aws:states:us-east-2:{ACCT}:stateMachine:finplan-prod-financemodel-run"),
        ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:us-east-2:{ACCT}:secret:finplan/prod/financeagent/ci-test-client-AbCdEf"),
        ("cloudformation:UpdateStack", f"arn:aws:cloudformation:us-east-2:{ACCT}:stack/finplan-prod-financialplanning-api/abc"),
        ("sagemaker:CreateTrainingJob", f"arn:aws:sagemaker:us-east-2:{ACCT}:training-job/finplan-prod-financemodel-backtest"),
        ("iam:PassRole", f"arn:aws:iam::{ACCT}:role/finplan-prod-financemodel-job-execution"),
        ("ssm:GetParameter", f"arn:aws:ssm:us-east-2:{ACCT}:parameter/finplan/prod"),
    ],
)
def test_named_deny_covers_each_service_of_another_environment(action, resource):
    """ENV-03/ENV-18: untagged resources named under another environment are denied service by service."""
    r = sim(action, resource, env_permission_boundary("gamma"))
    assert r.decision == EXPLICIT_DENY and any("DenyOtherEnvironmentNamed" in s for s in r.matched_deny), (action, resource)
    own = resource.replace("finplan-prod-", "finplan-gamma-").replace("finplan/prod", "finplan/gamma")
    assert sim(action, own, env_permission_boundary("gamma")).allowed, own


def test_research_api_patterns_deny_every_write_method_and_allow_reads(research):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("v1/plans", "v1/plans/pl_X/versions", "v1/portfolios", "v1/publications/pub_X/executions", "v1/executions/exe_X"):
            assert sim("execute-api:Invoke", f"arn:aws:execute-api:us-east-2:{ACCT}:abc123/gamma/{method}/{path}", research).decision == EXPLICIT_DENY, (method, path)
    for path in ("v1/plans/pl_X", "v1/snapshots/snap_X", "v1/staged-outputs/run_X"):
        assert sim("execute-api:Invoke", f"arn:aws:execute-api:us-east-2:{ACCT}:abc123/gamma/GET/{path}", research).allowed, path


def _cdk_role(boundary):
    return {"Resources": {"R": {"Type": "AWS::IAM::Role", "Properties": {"PermissionsBoundary": boundary, "Tags": [{"Key": "environment", "Value": "beta"}]}}}}


def test_renderer_resolves_pseudo_parameters_inside_fn_join():
    """The Fn::Join CDK writes for a boundary ARN (pseudo-parameter Refs) renders; no normalization needed."""
    join = lambda name: {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":iam::", {"Ref": "AWS::AccountId"}, f":policy/{name}"]]}  # noqa: E731
    assert check_role_boundaries(_cdk_role(join("finplan-beta-permission-boundary"))) == []
    assert check_role_boundaries(_cdk_role(join("finplan-beta-research-permission-boundary"))) == []
    [f] = check_role_boundaries(_cdk_role(join("finplan-gamma-permission-boundary")))
    assert "arn:aws:iam::<account-id>:policy/finplan-gamma-permission-boundary" in f.message
    sub = {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/finplan-beta-permission-boundary"}
    assert check_role_boundaries(_cdk_role(sub)) == []
    assert boundaries._render({"Ref": "AWS::Region"}, {}) == "<region>"
