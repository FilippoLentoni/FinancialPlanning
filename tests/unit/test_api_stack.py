"""Plan API stack: template assertions and resource-policy simulation (task 4.1, 4.8; API-01).

The API resource policy is generated from the same route table the handler enforces; the
simulation shows, per environment and principal, exactly which routes API Gateway lets through
before the handler runs. Consumers get a worst-case identity policy (``execute-api:*`` on ``*``)
so the denials shown come from the platform's resource policy alone.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from typing import Any

import pytest
from finplan_contracts.ownership import check_template
from finplan_contracts.validate import validate
from finplan_platform.core.config import load_config
from finplan_platform.handlers.api import FULL_ACCESS_CLASSES, ROUTES, route_allowed

from infra.policy_sim import Principal, role_arn, simulate
from infra.stacks.api import GATEWAY_RESPONSES, STAGE_NAME, compact_route_resources, resource_policy_document

pytestmark = pytest.mark.synth

ENVS = ("beta", "gamma", "prod")
WORST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "execute-api:*", "Resource": "*"}]}
IDS = {"plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM", "plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM", "publication_id": "pub_01KDVDNAZ83BAMMYCEGWF33DPM", "portfolio_id": "pf_01KDVDNAZ83BAMMYCEGWF33DPM", "run_id": "run_01KDVDNAZ83BAMMYCEGWF33DPM", "import_id": "imp_01KDVDNAZ83BAMMYCEGWF33DPM", "execution_id": "exe_01KDVDNAZ83BAMMYCEGWF33DPM", "input_snapshot_id": "snap_01KDVDNAZ83BAMMYCEGWF33DPM", "session_date": "2026-01-09"}


def roles(env: str) -> dict[str, str]:
    return {
        "platform": f"finplan-{env}-financialplanning-plan-api-handler-role",
        "website": f"finplan-{env}-financialplanning-website-backend",
        "operator": f"finplan-{env}-financialplanning-operator",
        "reader": f"finplan-{env}-financelambdastool-tool-role-reader",
        "submitter": f"finplan-{env}-financelambdastool-tool-role-submitter",
        "plan-writer": f"finplan-{env}-financelambdastool-tool-role-plan-writer",
        "financemodel-job": f"finplan-{env}-financemodel-job-execution-role",
        "financemodel-job-api": f"finplan-{env}-financemodel-job-api-handler-role",
    }


def invoke_arn(route: Any) -> str:
    return f"execute-api:/{STAGE_NAME}/{route.method}{route.path.format(**IDS)}"


@pytest.fixture(scope="module")
def api_synth() -> dict[str, Any]:
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from infra.app import build_app
    from infra.policy_sim import TemplateResolver

    app = build_app(cdk.App(), list(ENVS), env_modules=("infra.stacks.api",), app_modules=())
    templates: dict[str, Any] = {}
    for env in ENVS:
        stage = app.node.find_child(env.capitalize())
        for part in ("Storage", "Metadata", "Api"):
            st = stage.node.find_child(part)
            templates[st.stack_name] = Template.from_stack(st).to_json()
    resolver = TemplateResolver(templates)
    policies = {}
    for stack, _lid, r in resolver.resources("AWS::ApiGateway::RestApi"):
        env = stack.split("-")[1]
        policies[env] = resolver.resolve(r["Properties"]["Policy"], stack)
    return {"templates": templates, "resolver": resolver, "policies": policies}


def api_template(api_synth: dict[str, Any], env: str) -> dict[str, Any]:
    return api_synth["templates"][f"finplan-{env}-financialplanning-api"]


def resources(t: dict[str, Any], typ: str) -> list[dict[str, Any]]:
    return [r for r in t["Resources"].values() if r["Type"] == typ]


# ------------------------------------------------------------------ template
@pytest.mark.parametrize("env", ENVS)
def test_every_method_requires_iam_auth(api_synth: dict[str, Any], env: str) -> None:
    t = api_template(api_synth, env)
    methods = resources(t, "AWS::ApiGateway::Method")
    assert len(methods) == len(ROUTES)
    assert {m["Properties"]["AuthorizationType"] for m in methods} == {"AWS_IAM"}
    api = resources(t, "AWS::ApiGateway::RestApi")
    assert len(api) == 1 and api[0]["Properties"]["EndpointConfiguration"]["Types"] == ["REGIONAL"] and api[0]["Properties"]["Policy"]


@pytest.mark.parametrize("env", ENVS)
def test_function_role_output_and_tags(api_synth: dict[str, Any], env: str) -> None:
    t = api_template(api_synth, env)
    (fn,) = resources(t, "AWS::Lambda::Function")
    p = fn["Properties"]
    assert p["Runtime"] == "python3.12" and p["Architectures"] == ["arm64"] and "VpcConfig" not in p
    assert "ReservedConcurrentExecutions" not in p and p["Timeout"] <= 29
    assert p["Environment"]["Variables"]["FINPLAN_ENV"] == env
    (role,) = resources(t, "AWS::IAM::Role")
    assert role["Properties"]["RoleName"] == f"finplan-{env}-financialplanning-plan-api-handler-role"
    assert "PermissionsBoundary" in role["Properties"]
    (param,) = resources(t, "AWS::SSM::Parameter")
    assert param["Properties"]["Name"] == f"/finplan/{env}/financialplanning/api/plan-endpoint"
    for res in (fn, role, resources(t, "AWS::ApiGateway::RestApi")[0]):
        tags = {x["Key"]: x["Value"] for x in res["Properties"].get("Tags", [])}
        assert tags["environment"] == env and tags["owner-repo"] == "financialplanning" and tags["logical-role"] in ("plan-api", "plan-api-handler")
    assert not resources(t, "AWS::ApiGateway::Account")  # no account-level CloudWatch role


def test_gateway_rejections_are_contract_envelopes(api_synth: dict[str, Any]) -> None:
    t = api_template(api_synth, "beta")
    gws = resources(t, "AWS::ApiGateway::GatewayResponse")
    assert {g["Properties"]["ResponseType"] for g in gws} == set(GATEWAY_RESPONSES)
    for g in gws:
        body = json.loads(g["Properties"]["ResponseTemplates"]["application/json"].replace("$context.requestId", "0f8fad5b-d9cb-469f-a165-70867728950e"))
        assert validate(body, "error").valid, body
    by_type = {g["Properties"]["ResponseType"]: json.loads(g["Properties"]["ResponseTemplates"]["application/json"])["code"] for g in gws}
    assert by_type["MISSING_AUTHENTICATION_TOKEN"] == "UNAUTHORIZED" and by_type["ACCESS_DENIED"] == "FORBIDDEN"


def test_api_templates_pass_the_leak_scan(api_synth: dict[str, Any]) -> None:
    from finplan_contracts.leak_scan import scan_text

    for env in ENVS:
        findings = scan_text(json.dumps(api_template(api_synth, env)), f"api-{env}.json")
        assert findings == [], [str(f) for f in findings]


def test_ownership_check_on_api_templates(api_synth: dict[str, Any]) -> None:
    """Contract OWN-01: every resource of the API stack is attributed (contracts 0.2.0 matrix rows)."""
    for env in ENVS:
        report = check_template(api_template(api_synth, env), "financialplanning", name=f"api-{env}").to_dict()
        assert report["problems"] == [], [(p["resource_type"], p["message"]) for p in report["problems"]][:5]


# ------------------------------------------------------------------ resource-policy simulation
@pytest.mark.parametrize("env", ENVS)
def test_resource_policy_grants_exactly_the_route_table(api_synth: dict[str, Any], env: str) -> None:
    policy = api_synth["policies"][env]
    for cls, name in roles(env).items():
        principal = Principal.role(name, WORST)
        for route in ROUTES:
            res = simulate("execute-api:Invoke", invoke_arn(route), principal, resource_policy=policy)
            assert res.allowed == route_allowed(route, cls), (env, cls, route.method, route.path, res)


@pytest.mark.parametrize("env", ENVS)
def test_other_environments_and_strangers_are_denied_everything(api_synth: dict[str, Any], env: str) -> None:
    policy = api_synth["policies"][env]
    others = [e for e in ENVS if e != env]
    principals = [Principal.role(n, WORST) for o in others for n in roles(o).values()] + [Principal.role("some-unrelated-role", WORST), Principal(role_arn("finplan-shared-financialplanning-bootstrap"), (WORST,))]
    for p in principals:
        for route in ROUTES:
            assert not simulate("execute-api:Invoke", invoke_arn(route), p, resource_policy=policy).allowed, (env, p.arn, route.path)


def test_platform_roles_without_identity_policy_are_allowed_by_the_resource_policy(api_synth: dict[str, Any]) -> None:
    policy = api_synth["policies"]["beta"]
    for cls in FULL_ACCESS_CLASSES:
        p = Principal.role(roles("beta")[cls])
        assert all(simulate("execute-api:Invoke", invoke_arn(r), p, resource_policy=policy).allowed == route_allowed(r, cls) for r in ROUTES)


def test_plan_api_role_synthesized_grants(api_synth: dict[str, Any]) -> None:
    """The handler role's real identity policy + boundary against the storage and table resource policies."""
    from finplan_contracts.boundaries import env_permission_boundary

    r = api_synth["resolver"]
    name = "finplan-beta-financialplanning-plan-api-handler-role"
    p = Principal(role_arn(name), tuple(r.role_policies(name)), env_permission_boundary("beta"))
    buckets, tables = r.bucket_policies(), r.table_policies()
    acct = "<account-id>"
    plans = f"finplan-beta-financialplanning-plans-{acct}"
    key = f"arn:aws:s3:::{plans}/pl_A/pv_A/content.json"
    assert simulate("s3:GetObject", key, p, resource_policy=buckets[plans]).allowed
    assert simulate("s3:PutObject", key, p, resource_policy=buckets[plans], context={"s3:if-none-match": "*"}).allowed
    assert not simulate("s3:PutObject", key, p, resource_policy=buckets[plans]).allowed  # write-once
    assert not simulate("s3:DeleteObject", key, p, resource_policy=buckets[plans]).allowed
    tarn = lambda env, logical: f"arn:aws:dynamodb:us-east-2:{acct}:table/finplan-{env}-financialplanning-{logical}"
    for logical in ("plan", "plan-version", "publication", "execution", "idempotency", "audit-event"):
        rp = tables.get(f"finplan-beta-financialplanning-{logical}")
        assert simulate("dynamodb:PutItem", tarn("beta", logical), p, resource_policy=rp).allowed, logical
        assert not simulate("dynamodb:DeleteItem", tarn("beta", logical), p, resource_policy=rp).allowed, logical
    assert simulate("dynamodb:UpdateItem", tarn("beta", "plan"), p, resource_policy=tables.get("finplan-beta-financialplanning-plan")).allowed
    assert not simulate("dynamodb:UpdateItem", tarn("beta", "audit-event"), p, resource_policy=tables.get("finplan-beta-financialplanning-audit-event")).allowed
    assert not simulate("dynamodb:GetItem", tarn("gamma", "plan"), p, resource_policy=tables.get("finplan-gamma-financialplanning-plan")).allowed


def test_pure_policy_builder_matches_the_synthesized_policy(api_synth: dict[str, Any]) -> None:
    built = resource_policy_document(load_config("beta"), lambda pat: role_arn(pat))
    assert built == api_synth["policies"]["beta"]


@pytest.mark.parametrize("env", ENVS)
def test_effective_rest_api_policy_fits_aws_size_limit(api_synth: dict[str, Any], env: str) -> None:
    """API Gateway permits 8192 bytes; resolve deployment tokens before measuring.

    Normal JSON whitespace is deliberately retained, reserving 512 bytes beyond
    today's policy so changes cannot pass synth and then roll back in AWS.
    """
    effective = json.dumps(api_synth["policies"][env]).replace("<account-id>", "0" * 12).replace("<region>", "us-east-2")
    assert "<account-id>" not in effective and "<region>" not in effective
    size = len(effective.encode("utf-8"))
    assert size <= 8192 - 512, (env, size)


def test_compaction_removes_only_existing_wildcard_redundancy() -> None:
    resources = ["execute-api:/*/GET/v1/snapshots/*", "execute-api:/*/GET/v1/snapshots/latest", "execute-api:/*/GET/v1/snapshots/*/observations", "execute-api:/*/GET/v1/portfolios/*/state", "execute-api:/*/GET/v1/plans/*", "execute-api:/*/GET/v1/plans/*/versions"]
    reduced = compact_route_resources(resources)
    assert reduced == ["execute-api:/*/GET/v1/plans/*", "execute-api:/*/GET/v1/portfolios/*/state", "execute-api:/*/GET/v1/snapshots/*"]
    candidates = ["execute-api:/live/GET/v1/snapshots/latest", "execute-api:/live/GET/v1/snapshots/snap_fixture/observations", "execute-api:/live/GET/v1/plans/pl_fixture/versions", "execute-api:/live/GET/v1/portfolios/pf_fixture/state", "execute-api:/live/GET/v1/portfolios/pf_fixture", "execute-api:/live/PUT/v1/portfolios/pf_fixture/state", "execute-api:/live/POST/v1/plans/pl_fixture/versions"]
    for candidate in candidates:
        assert any(fnmatchcase(candidate, r) for r in resources) == any(fnmatchcase(candidate, r) for r in reduced)


@pytest.mark.parametrize("env", ENVS)
def test_compacted_policy_preserves_original_allow_and_deny_semantics(api_synth: dict[str, Any], env: str, monkeypatch: Any) -> None:
    import infra.stacks.api as api
    monkeypatch.setattr(api, "compact_route_resources", lambda resources: sorted(set(resources)))
    original = api.resource_policy_document(load_config(env), role_arn)
    compacted = api_synth["policies"][env]
    for cls, name in roles(env).items():
        principal = Principal.role(name, WORST)
        for route in ROUTES:
            resource = invoke_arn(route)
            assert simulate("execute-api:Invoke", resource, principal, resource_policy=original).allowed == simulate("execute-api:Invoke", resource, principal, resource_policy=compacted).allowed, (env, cls, route.path)
    denied_descendants = next(s for s in compacted["Statement"] if s.get("Sid") == "DenyFinancemodelJobApiExcludedDescendants")
    assert denied_descendants["Effect"] == "Deny" and denied_descendants["Resource"]
