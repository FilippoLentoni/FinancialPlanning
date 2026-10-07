"""Portfolios, plans, plan head and version list (tasks 4.3, 4.6, 4.8; API-03, API-08, API-11)."""

from __future__ import annotations

from typing import Any

from finplan_contracts.validate import validate

from tests.api_support import *
from tests.api_support import PRINCIPALS, Flow, seed_snapshot


def _items(ddb: Any, table: str) -> int:
    return len(ddb.scan(TableName=f"finplan-beta-financialplanning-{table}")["Items"])


# ------------------------------------------------------------------ API-03
def test_create_synthetic_portfolio_mints_id_with_revision_1(clients: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "Synthetic", "base_currency": "USD", "synthetic": True, "idempotency_key": "pf-1"})
    assert code == 201
    assert body["portfolio_id"].startswith("pf_") and body["revision"] == 1 and body["synthetic"] is True
    assert validate(body, "portfolio").valid


def test_non_synthetic_portfolio_is_not_permitted_and_writes_nothing(clients: Any, ddb: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "Real", "base_currency": "USD", "synthetic": False, "idempotency_key": "pf-2"})
    assert code == 403 and body["code"] == "OPERATION_NOT_PERMITTED"
    assert validate(body, "error").valid
    assert _items(ddb, "portfolio") == 0 and _items(ddb, "idempotency") == 0


def test_client_supplied_portfolio_id_is_rejected(clients: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD", "portfolio_id": "pf_01KDVDNAZ83BAMMYCEGWF33DPM", "idempotency_key": "pf-3"})
    assert code == 400 and body["code"] == "VALIDATION_FAILED" and body["details"]["pointer"] == "/portfolio_id"


def test_create_plan_moves_the_portfolio_revision(flow: Flow, clients: Any) -> None:
    pf = flow.portfolio()
    plan = flow.plan(portfolio=pf)
    assert plan["plan_id"].startswith("pl_") and plan["head"] == {"current_version_id": None, "revision": 1}
    assert plan["publication_revision"] == 1 and plan["portfolio_revision"] == 2
    code, got, _ = clients.website.get(f"/v1/portfolios/{pf['portfolio_id']}")
    assert code == 200 and got["revision"] == 2 and got["synthetic"] is True


def test_create_plan_with_stale_portfolio_revision_conflicts(flow: Flow, clients: Any, ddb: Any) -> None:
    pf = flow.portfolio()
    flow.plan(portfolio=pf)  # portfolio revision 1 -> 2
    before = _items(ddb, "plan")
    code, body, _ = clients.website.post(f"/v1/portfolios/{pf['portfolio_id']}/plans", {"name": "Late", "expected_revision": 1, "idempotency_key": "pl-late"})
    assert code == 409 and body["code"] == "CONFLICT" and body["retryable"] is False
    assert _items(ddb, "plan") == before


def test_unknown_portfolio_is_not_found(clients: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios/pf_01KDVDNAZ83BAMMYCEGWF33DPM/plans", {"name": "X", "expected_revision": 1, "idempotency_key": "pl-x"})
    assert code == 404 and body["code"] == "NOT_FOUND"


def test_wrong_prefix_in_path_is_invalid_identifier(clients: Any) -> None:
    code, body, _ = clients.website.get("/v1/portfolios/pl_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert code == 400 and body["code"] == "INVALID_IDENTIFIER" and body["details"]["field"] == "portfolio_id"


# ------------------------------------------------------------------ API-08 on portfolio/plan writes
def test_missing_idempotency_key_is_rejected(clients: Any) -> None:
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "X", "base_currency": "USD"})
    assert code == 400 and body["code"] == "VALIDATION_FAILED" and body["details"]["pointer"] == "/idempotency_key"


def test_duplicate_create_returns_the_original(clients: Any, ddb: Any) -> None:
    req = {"name": "Dup", "base_currency": "USD", "idempotency_key": "pf-dup"}
    c1, b1, h1 = clients.website.post("/v1/portfolios", req)
    c2, b2, h2 = clients.website.post("/v1/portfolios", req)
    assert c1 == c2 == 201 and b1 == b2
    assert "X-Idempotent-Replay" not in h1 and h2["X-Idempotent-Replay"] == "true"
    assert _items(ddb, "portfolio") == 1


def test_reused_key_with_different_body_fails(clients: Any, ddb: Any) -> None:
    clients.website.post("/v1/portfolios", {"name": "A", "base_currency": "USD", "idempotency_key": "pf-re"})
    code, body, _ = clients.website.post("/v1/portfolios", {"name": "B", "base_currency": "USD", "idempotency_key": "pf-re"})
    assert code == 422 and body["code"] == "IDEMPOTENCY_KEY_REUSED" and body["retryable"] is False
    assert _items(ddb, "portfolio") == 1


def test_same_key_different_principals_are_independent(clients: Any, ddb: Any) -> None:
    req = {"name": "Same", "base_currency": "USD", "idempotency_key": "pf-shared"}
    _, a, _ = clients.website.post("/v1/portfolios", req)
    _, b, _ = clients.operator.post("/v1/portfolios", req)
    assert a["portfolio_id"] != b["portfolio_id"] and _items(ddb, "portfolio") == 2


def test_assumed_role_sessions_share_the_role_idempotency_scope(clients: Any, ddb: Any) -> None:
    session = lambda n: PRINCIPALS["website"].replace(":iam::", ":sts::").replace(":role/", ":assumed-role/") + f"/session-{n}"
    req = {"name": "Session", "base_currency": "USD", "idempotency_key": "pf-sess"}
    _, a, _ = clients.as_principal(session(1)).post("/v1/portfolios", req)
    _, b, h = clients.as_principal(session(2)).post("/v1/portfolios", req)
    assert a == b and h.get("X-Idempotent-Replay") == "true" and _items(ddb, "portfolio") == 1


# ------------------------------------------------------------------ API-11
def test_plan_head_read_by_tool_reader_matches_the_website_read(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    root = flow.root(plan["plan_id"], snap)
    child = flow.child(plan["plan_id"], root["plan_version_id"], body_content=content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5}))
    c1, web, _ = clients.website.get(f"/v1/plans/{plan['plan_id']}")
    c2, tool, _ = clients.reader.get(f"/v1/plans/{plan['plan_id']}")
    assert c1 == c2 == 200 and web == tool
    assert tool["plan"]["head"] == {"current_version_id": child["plan_version_id"], "revision": 3}
    assert tool["plan"]["publication_revision"] == 1 and tool["current_publication"] is None
    assert validate(tool, "tools/get-plan-response").valid


def test_version_list_pages_have_no_duplicates_while_a_version_is_committed(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    pid = plan["plan_id"]
    root = flow.root(pid, snap)
    ids = [root["plan_version_id"]]
    for i in range(4):
        clock.advance(seconds=1)
        ids.append(flow.child(pid, ids[-1], body_content=content(fees={"transaction_cost_bps": i + 1}))["plan_version_id"])
    seen: list[str] = []
    code, page, _ = clients.reader.get(f"/v1/plans/{pid}/versions?page_size=2")
    assert code == 200 and validate(page, "tools/list-plan-versions-response").valid
    seen += [v["plan_version_id"] for v in page["versions"]]
    clock.advance(seconds=1)
    late = flow.child(pid, ids[-1], body_content=content(fees={"transaction_cost_bps": 99}))["plan_version_id"]
    token = page["next_token"]
    while token:
        code, page, _ = clients.reader.get(f"/v1/plans/{pid}/versions?page_size=2&next_token={token}")
        assert code == 200
        seen += [v["plan_version_id"] for v in page["versions"]]
        for v in page["versions"]:
            _, single, _ = clients.reader.get(f"/v1/plan-versions/{v['plan_version_id']}")
            assert single["plan_version"]["checksum"] == v["checksum"]
        token = page["next_token"]
    assert len(seen) == len(set(seen)) == 5  # each pre-existing version exactly once
    assert seen == list(reversed(ids))  # newest first
    _, fresh, _ = clients.reader.get(f"/v1/plans/{pid}/versions?page_size=10")
    assert fresh["versions"][0]["plan_version_id"] == late


def test_page_token_of_another_plan_is_rejected(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    snap = seed_snapshot(svc, clock)
    p1, p2 = flow.plan(), flow.plan()
    r = flow.root(p1["plan_id"], snap)
    flow.child(p1["plan_id"], r["plan_version_id"], body_content=content(fees={"transaction_cost_bps": 9}))
    _, page, _ = clients.reader.get(f"/v1/plans/{p1['plan_id']}/versions?page_size=1")
    assert page["next_token"]
    code, body, _ = clients.reader.get(f"/v1/plans/{p2['plan_id']}/versions?page_size=1&next_token={page['next_token']}")
    assert code == 400 and body["code"] == "VALIDATION_FAILED" and body["details"]["pointer"] == "/next_token"


def test_page_size_bounds(flow: Flow, clients: Any) -> None:
    plan = flow.plan()
    for bad in ("0", "101"):
        code, body, _ = clients.reader.get(f"/v1/plans/{plan['plan_id']}/versions?page_size={bad}")
        assert code == 400 and body["code"] == "VALIDATION_FAILED"
