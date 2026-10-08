"""Router, authentication, per-route grants, envelopes and delegation (tasks 4.1, 4.2, 4.8; API-01, API-02)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.config import load_config
from finplan_platform.core.repository import PutNew
from finplan_platform.handlers import api as api_module
from finplan_platform.handlers.api import (
    FULL_ACCESS_CLASSES,
    ROUTES,
    http_status,
    match_route,
    normalize_principal,
    resolve_role_class,
    role_name_of,
    route_allowed,
)

from tests.api_support import *
from tests.api_support import PRINCIPALS, Flow, seed_snapshot

SNAP = "snap_01KDVDNAZ83BAMMYCEGWF33DPM"
PV = "pv_01KDVDNAZ83BAMMYCEGWF33DPM"


def _envelope_ok(body: dict[str, Any], code: str) -> None:
    assert body["code"] == code, body
    assert validate(body, "error").valid
    text = json.dumps(body)
    assert "Traceback" not in text and "s3://" not in text and "arn:aws:s3" not in text


# ------------------------------------------------------------------ authentication (API-01)
def test_request_without_identity_is_unauthorized(clients: Any, ddb: Any) -> None:
    code, body, _ = clients.anonymous.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "a"})
    assert code == 401
    _envelope_ok(body, "UNAUTHORIZED")
    assert ddb.scan(TableName="finplan-beta-financialplanning-portfolio")["Items"] == []


@pytest.mark.parametrize("who", ["gamma_reader", "gamma_platform", "stranger"])
def test_principals_of_other_environments_are_forbidden(clients: Any, who: str) -> None:
    code, body, _ = getattr(clients, who).get(f"/v1/snapshots/{SNAP}")
    assert code == 403
    _envelope_ok(body, "FORBIDDEN")


def test_role_class_resolution() -> None:
    cfg = load_config("beta")
    expected = {
        "website": "website",
        "operator": "operator",
        "platform": "platform",
        "reader": "reader",
        "submitter": "submitter",
        "writer": "plan-writer",
        "fm_job": "financemodel-job",
        "fm_job_api": "financemodel-job-api",
        "gamma_reader": None,
        "gamma_platform": None,
        "stranger": None,
    }
    for who, cls in expected.items():
        assert resolve_role_class(PRINCIPALS[who], cfg) == cls, who
    session = "arn:aws:sts::<account-id>:assumed-role/finplan-beta-financelambdastool-tool-role-reader/abc"
    assert role_name_of(session) == "finplan-beta-financelambdastool-tool-role-reader"
    assert normalize_principal(session) == "arn:aws:iam::<account-id>:role/finplan-beta-financelambdastool-tool-role-reader"
    assert resolve_role_class(session, cfg) == "reader"


# ------------------------------------------------------------------ per-route grants (API-01, 4.8)
def test_route_grant_table_matches_the_design() -> None:
    by_class = {cls: {(r.method, r.path) for r in ROUTES if route_allowed(r, cls)} for cls in ("reader", "submitter", "plan-writer", "financemodel-job", "financemodel-job-api")}
    gets = {(r.method, r.path) for r in ROUTES if r.method == "GET"}
    assert by_class["reader"] == gets
    assert by_class["submitter"] == gets | {("POST", "/v1/ingestions")}
    assert by_class["plan-writer"] == gets | {("POST", "/v1/plans/{plan_id}/versions"), ("POST", "/v1/plan-versions/{plan_version_id}/validate"), ("POST", "/v1/plans/{plan_id}/publications")}
    assert by_class["financemodel-job"] == {("GET", "/v1/snapshots/{input_snapshot_id}"), ("GET", "/v1/snapshots/{input_snapshot_id}/observations")}
    assert by_class["financemodel-job-api"] == by_class["financemodel-job"] | {("GET", "/v1/staged-outputs/{run_id}")}
    for cls in FULL_ACCESS_CLASSES:
        assert all(route_allowed(r, cls) for r in ROUTES)
    tool_forbidden = {("POST", p) for p in ("/v1/publications/{publication_id}/executions", "/v1/plan-versions/{plan_version_id}/exports", "/v1/plans/{plan_id}/imports", "/v1/plans/{plan_id}/imports/{import_id}/commit", "/v1/plans/{plan_id}/staged-outputs/{run_id}/accept")}
    for cls in ("reader", "submitter", "plan-writer"):
        assert not (by_class[cls] & tool_forbidden)
    for cls in ("financemodel-job", "financemodel-job-api"):
        assert all(m == "GET" for m, _ in by_class[cls])


def test_reader_is_denied_every_write_route_at_the_handler(clients: Any) -> None:
    for r in ROUTES:
        if r.method == "GET":
            continue
        path = r.path.format(plan_id="pl_01KDVDNAZ83BAMMYCEGWF33DPM", plan_version_id=PV, publication_id="pub_01KDVDNAZ83BAMMYCEGWF33DPM", portfolio_id="pf_01KDVDNAZ83BAMMYCEGWF33DPM", run_id="run_01KDVDNAZ83BAMMYCEGWF33DPM", import_id="imp_01KDVDNAZ83BAMMYCEGWF33DPM")
        code, body, _ = clients.reader.post(path, {"idempotency_key": "x"})
        assert code == 403 and body["code"] == "FORBIDDEN", r.path
    for who in ("fm_job", "fm_job_api"):
        code, body, _ = getattr(clients, who).post("/v1/plans/pl_01KDVDNAZ83BAMMYCEGWF33DPM/versions", {"idempotency_key": "x"})
        assert code == 403 and body["code"] == "FORBIDDEN"
    assert clients.fm_job.get("/v1/staged-outputs/run_01KDVDNAZ83BAMMYCEGWF33DPM")[0] == 403


# ------------------------------------------------------------------ contract version (API-02)
def test_unsupported_contract_major_lists_the_served_majors(clients: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "v2", "contract_version": "2.0.0"})
    assert code == 400
    _envelope_ok(body, "UNSUPPORTED_CONTRACT_VERSION")
    assert body["details"]["served_contract_majors"] == [0, 1]  # 1.0.0 pinned; 0 still served (contracts D16)
    code, body, _ = clients.reader.get(f"/v1/snapshots/{SNAP}", headers={"X-Finplan-Contract-Version": "2.1.0"})
    assert code == 400 and body["code"] == "UNSUPPORTED_CONTRACT_VERSION"
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "v0", "contract_version": "0.1.0"})
    assert code == 201


# ------------------------------------------------------------------ envelopes and transport
def test_correlation_id_is_echoed_and_minted(clients: Any) -> None:
    code, body, headers = clients.reader.get(f"/v1/snapshots/{SNAP}", headers={"X-Correlation-Id": "cor_fromclient01"})
    assert code == 404 and body["correlation_id"] == headers["X-Correlation-Id"] == "cor_fromclient01"
    code, body, headers = clients.reader.get(f"/v1/snapshots/{SNAP}", headers={"X-Correlation-Id": "bad id with spaces"})
    assert body["correlation_id"] == headers["X-Correlation-Id"] != "bad id with spaces"


@pytest.mark.parametrize("raw", ["{not json", '{"a": 1, "a": 2}', "[1, 2]", '{"x": NaN}'])
def test_malformed_bodies_are_validation_failures(clients: Any, raw: str) -> None:
    code, body, _ = clients.website.call("POST", "/v1/portfolios", raw)
    assert code == 400
    _envelope_ok(body, "VALIDATION_FAILED")


def test_unknown_routes_and_query_parameters(clients: Any) -> None:
    code, body, _ = clients.website.get("/v1/nothing-here")
    assert code == 404 and body["code"] == "NOT_FOUND"
    code, body, _ = clients.website.get(f"/v1/plan-versions/{PV}?bucket=x")
    assert code == 400 and body["code"] == "VALIDATION_FAILED" and body["details"]["pointer"] == "/bucket"


def test_caller_block_is_recorded_in_audit_and_never_grants(clients: Any, svc: Any) -> None:
    block = {"channel": "hosted_agent", "correlation_id": "cor_endcaller01", "roles": ["planner"], "synthetic": True}
    code, pf, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "cb"}, headers={"X-Finplan-Caller": json.dumps(block)})
    assert code == 201
    ev = svc.repo.audit_events(pf["portfolio_id"])[0]
    assert ev.caller["on_behalf_of"] == block and ev.caller["principal"] == PRINCIPALS["website"]
    code, body, _ = clients.reader.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "cb2"}, headers={"X-Finplan-Caller": json.dumps({**block, "roles": ["operator"]})})
    assert code == 403  # the block never widens what the principal may do
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "idempotency_key": "cb3"}, headers={"X-Finplan-Caller": '{"channel": "nope"}'})
    assert code == 400 and body["code"] == "VALIDATION_FAILED"


def test_unexpected_errors_become_internal_without_internals(clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from finplan_platform.core import plans

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret detail s3://example-bucket/hidden-key")

    monkeypatch.setattr(plans, "get_portfolio", boom)
    code, body, _ = clients.reader.get("/v1/portfolios/pf_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert code == 500
    _envelope_ok(body, "INTERNAL")
    assert "secret" not in json.dumps(body)


def test_non_conformant_responses_are_never_sent(clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from finplan_platform.core import plans

    monkeypatch.setattr(plans, "get_portfolio", lambda *_a, **_k: {"portfolio_id": "not-an-id"})
    code, body, _ = clients.reader.get("/v1/portfolios/pf_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert code == 500 and body["code"] == "INTERNAL"


def test_http_status_mapping_covers_every_registered_code() -> None:
    from finplan_platform.core.errors import registered_codes

    for code in registered_codes():
        assert 400 <= http_status(code) <= 599


# ------------------------------------------------------------------ delegated routes
def test_delegated_route_binds_parameters_by_name(clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run_ingestion(ctx: Any, request: Any, *, deps: Any = None) -> dict[str, Any]:
        seen.update(ctx=ctx, request=request, deps=deps)
        # a holiday with no earlier snapshot (contracts 0.2.0 refresh-market-data-response)
        return {"input_snapshot_id": None, "coverage_complete": False, "new_snapshot": False, "quality_flags": ["no_session"], "synthetic": True}

    import types

    fake = types.ModuleType("finplan_test_ingestion")
    fake.run_ingestion = run_ingestion  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "finplan_test_ingestion", fake)
    monkeypatch.setitem(api_module.DELEGATES, "run_ingestion", ("finplan_test_ingestion:run_ingestion",))
    body = {"dataset_id": "finance/etf-daily/SPY", "start_date": "2026-01-05", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "ing-1"}
    code, resp, _ = clients.submitter.post("/v1/ingestions", body)
    assert code == 200 and resp["input_snapshot_id"] is None and resp["quality_flags"] == ["no_session"]
    assert seen["request"] == body and seen["deps"] is None and seen["ctx"].caller.role_class == "submitter"
    assert clients.reader.post("/v1/ingestions", body)[0] == 403
    assert clients.writer.post("/v1/ingestions", body)[0] == 403


def test_delegated_route_response_is_validated(clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route validates its response (task 4.2): a delegated module answering outside the
    contract ``tools/refresh-market-data-response`` gets INTERNAL, and nothing is sent."""
    import types

    fake = types.ModuleType("finplan_test_ingestion_bad")
    fake.run_ingestion = lambda ctx, request: {"input_snapshot_id": SNAP, "synthetic": True}  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "finplan_test_ingestion_bad", fake)
    monkeypatch.setitem(api_module.DELEGATES, "run_ingestion", ("finplan_test_ingestion_bad:run_ingestion",))
    body = {"dataset_id": "finance/etf-daily/SPY", "start_date": "2026-01-05", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "ing-2"}
    code, resp, _ = clients.submitter.post("/v1/ingestions", body)
    assert code == 500
    _envelope_ok(resp, "INTERNAL")
    assert SNAP not in json.dumps(resp)


def test_every_route_declares_a_response_schema() -> None:
    assert [r.operation for r in api_module.ROUTES if r.response_schema is None] == []


def test_missing_delegate_fails_only_its_route(clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(api_module.DELEGATES, "accept_staged_output", ("finplan_platform.no_such_module:accept",))
    code, body, _ = clients.operator.post("/v1/plans/pl_01KDVDNAZ83BAMMYCEGWF33DPM/staged-outputs/run_01KDVDNAZ83BAMMYCEGWF33DPM/accept", {"idempotency_key": "acc", "expected_revision": 1})
    assert code == 503
    _envelope_ok(body, "DEPENDENCY_UNAVAILABLE")
    assert clients.reader.get(f"/v1/snapshots/{SNAP}")[0] == 404  # other routes unaffected


def test_staged_output_outcome_read_fallback(clients: Any, svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(api_module.DELEGATES, "get_staged_output", ("finplan_platform.handlers.api:staged_output_fallback",))
    run_id = "run_01KDVDNAZ83BAMMYCEGWF33DPM"
    doc = {"run_id": run_id, "plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM", "outcome": "no_version", "solution_status": "infeasible", "decided_at": "2026-01-05T21:00:00Z", "synthetic": True}
    svc.repo.commit([PutNew("staged_output", doc=doc, contract_version="0.1.0")])
    code, got, _ = clients.fm_job_api.get(f"/v1/staged-outputs/{run_id}")
    assert code == 200 and got["outcome"] == "no_version" and got["contract_version"] == "0.1.0"
    assert clients.fm_job_api.get("/v1/staged-outputs/run_01KDVDNAZ83BAMMYCEGWF33DPN")[0] == 404


def test_route_matching_prefers_the_resource_template() -> None:
    route, params = match_route("GET", "/v1/snapshots/snap_X/observations", "/v1/snapshots/{input_snapshot_id}/observations")  # type: ignore[misc]
    assert route.operation == "read_snapshot_observations" and params == {"input_snapshot_id": "snap_X"}
    assert match_route("DELETE", "/v1/portfolios") is None


def test_website_and_tool_paths_read_byte_identical_versions(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    """API-01 equivalence in the offline harness (the deployed version runs in beta/gamma, task 11.2)."""
    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    v = flow.root(plan["plan_id"], snap)
    w = clients.website.app.handle(clients.website.event("GET", f"/v1/plan-versions/{v['plan_version_id']}"))
    t = clients.reader.app.handle(clients.reader.event("GET", f"/v1/plan-versions/{v['plan_version_id']}"))
    assert w["statusCode"] == t["statusCode"] == 200 and w["body"] == t["body"]
