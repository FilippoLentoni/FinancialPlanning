"""Issued evidence and paper execution are durable, self-financing and single use."""
from __future__ import annotations

import base64
import gzip
import json
import os
import threading

import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.config import load_config
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.plans import create_portfolio, get_portfolio_state, put_portfolio_state
from finplan_platform.core.portfolio_lifecycle import (
    create_decision, get_decision, get_history, history_id, list_activity,
    list_decisions, list_history, record_activity, resolve_decision,
)
from finplan_platform.core.repository import HeadMove
from finplan_platform.core.services import Services
from finplan_platform.core.snapshot_reads import list_approved_snapshots, load_payloads
from tests.api_support import *


def _book():
    return {"positions": [{"instrument_id": "SPY", "quantity": 20}], "cash_balance": 1000,
            "high_watermark": 20000, "as_of": "2026-01-09", "base_currency": "USD",
            "mode": "paper", "source": "paper_initialization"}


def _setup(svc, clock, ctx_factory, *, legacy=False):
    ctx = ctx_factory(role_class="operator")
    pid = create_portfolio(ctx, svc, {"name": "Lifecycle paper book", "base_currency": "USD", "synthetic": True, "idempotency_key": "create-book"}).response["portfolio_id"]
    if legacy:
        svc.repo.commit([HeadMove("portfolio", record_id=pid, expected_revision=0, revision_attr="paper_state_revision", allow_missing_revision=True, set_attrs={"paper_state": _book()})])
    else:
        put_portfolio_state(ctx, svc, pid, {"paper_state": _book(), "expected_revision": 0, "idempotency_key": "initialize"})
    sid = seed_snapshot(svc, clock)
    observations = load_payloads(svc, svc.repo.require("snapshot_catalog", sid))[0]["observations"]
    observation = max(observations, key=lambda o: o["session_date"])
    request = {"portfolio_id": pid, "algorithm_family": "reinforcement_learning", "algorithm": "ppo",
               "input_snapshot_id": sid, "portfolio_revision": 1,
               "recommendation": {"weights": {"SPY": 0.5, "USD_CASH": 0.5}},
               "provenance": {"model_version": "frozen-policy", "feature_checksum": "fixed"},
               "execution": {"reference_date": observation["session_date"], "reference_prices": {"SPY": observation["close"]},
                             "target_weights": {"SPY": 0.5, "USD_CASH": 0.5}, "transaction_cost_bps": 2},
               "idempotency_key": "issue"}
    return ctx, pid, sid, request


def _resolve_body(**changes):
    return {"action": "accept", "expected_revision": 1, "confirmed_by_user": True, "idempotency_key": "accept", **changes}


def _issue(ctx, svc, pid, request):
    return create_decision(ctx, svc, pid, request).response["decision"]


def test_acceptance_persists_exact_evidence_and_conserves_wealth(repo_any, artifacts, clock, ctx_factory):
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, sid, request = _setup(svc, clock, ctx_factory)
    decision = _issue(ctx, svc, pid, request)
    assert validate({"decision": decision, "synthetic": True, "contract_version": "1.5.0"}, "api/portfolio-decision-response").valid
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 1
    proposal_bytes, _ = svc.store.get("reports", svc.repo.require("portfolio_decision", decision["decision_id"]).doc["artifact_key"])
    clock.advance(seconds=60)
    resolution = resolve_decision(ctx, svc, decision["decision_id"], _resolve_body()).response
    assert validate(resolution, "api/resolve-portfolio-decision-response").valid
    assert resolution["before_revision"] == 1 and resolution["after_revision"] == 2
    assert resolution["paper_execution"] is True
    assert resolution["recorded_at"] == ctx.now_ts() and resolution["recorded_at"][:10] != resolution["reference_date"]
    assert resolution["caller"]["principal"] == ctx.caller.principal
    price = request["execution"]["reference_prices"]["SPY"]
    value_before = 1000 + 20 * price
    state = resolution["paper_state"]
    value_after = state["cash_balance"] + sum(p["quantity"] * price for p in state["positions"])
    assert value_after + resolution["transaction_cost"] == pytest.approx(value_before)
    assert state["cash_balance"] == pytest.approx(0.5 * value_after)
    assert resolution["transaction_cost"] == pytest.approx(sum(f["notional"] for f in resolution["simulated_fills"]) * 0.0002)
    assert get_portfolio_state(ctx, svc, pid)["paper_state"] == state
    history = list_history(ctx, svc, pid, {})["history"]
    assert [h["revision"] for h in history] == [2, 1]
    assert history[0]["input_snapshot_id"] == sid and history[0]["decision_id"] == decision["decision_id"]
    assert history[0]["recorded_at"] == resolution["recorded_at"] and history[1]["paper_state"] == _book()
    assert get_history(ctx, svc, pid, "2")["history"] == history[0]
    stored = get_decision(ctx, svc, decision["decision_id"])["decision"]
    assert stored["recommendation"] == request["recommendation"] and stored["status"] == "accepted"
    assert svc.store.get("reports", svc.repo.require("portfolio_decision", decision["decision_id"]).doc["artifact_key"])[0] == proposal_bytes


def test_permanent_resolution_prevents_second_fill_after_idempotency_expiry(svc, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    did = _issue(ctx, svc, pid, request)["decision_id"]
    first = resolve_decision(ctx, svc, did, _resolve_body())
    replay = resolve_decision(ctx, svc, did, _resolve_body())
    assert replay.replayed and replay.response == first.response
    # A fresh key exercises the permanent decision status rather than the TTL cache.
    clock.advance(days=10)
    assert resolve_decision(ctx, svc, did, _resolve_body(idempotency_key="after-ttl")).response == first.response
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 2
    assert len(list_history(ctx, svc, pid, {})["history"]) == 2
    with pytest.raises(PlatformError) as exc:
        resolve_decision(ctx, svc, did, _resolve_body(action="reject", idempotency_key="opposite"))
    assert exc.value.code == "CONFLICT"


def test_rejection_preserves_holdings_and_new_request_uses_accepted_revision(svc, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    rejected = _issue(ctx, svc, pid, request)["decision_id"]
    result = resolve_decision(ctx, svc, rejected, _resolve_body(action="reject")).response
    assert result["simulated_fills"] == [] and result["transaction_cost"] == 0
    assert result["after_revision"] == 1 and not result["paper_execution"]
    assert get_portfolio_state(ctx, svc, pid)["paper_state"] == _book()
    next_decision = _issue(ctx, svc, pid, {**request, "idempotency_key": "second-proposal"})
    resolve_decision(ctx, svc, next_decision["decision_id"], _resolve_body(idempotency_key="second-resolution"))
    next_day = _issue(ctx, svc, pid, {**request, "portfolio_revision": 2, "idempotency_key": "latest-state"})
    assert next_day["portfolio_revision"] == get_portfolio_state(ctx, svc, pid)["revision"] == 2


def test_stale_holdings_and_new_market_snapshot_cannot_be_accepted(svc, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    first = _issue(ctx, svc, pid, request)["decision_id"]
    second = _issue(ctx, svc, pid, {**request, "idempotency_key": "second-proposal"})["decision_id"]
    resolve_decision(ctx, svc, first, _resolve_body())
    with pytest.raises(PlatformError) as exc:
        resolve_decision(ctx, svc, second, _resolve_body(idempotency_key="stale"))
    assert exc.value.code == "CONFLICT"
    third = _issue(ctx, svc, pid, {**request, "portfolio_revision": 2, "idempotency_key": "third-proposal"})["decision_id"]
    clock.advance(seconds=1)
    seed_snapshot(svc, clock)
    with pytest.raises(PlatformError) as exc:
        resolve_decision(ctx, svc, third, _resolve_body(expected_revision=2, idempotency_key="old-market"))
    assert exc.value.code == "CONFLICT" and exc.value.details["reason"] == "stale_market_snapshot"
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 2
    assert get_decision(ctx, svc, third)["decision"]["status"] == "proposed"


def test_concurrent_acceptance_commits_one_head_history_and_audit(svc, ddb, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    dids = [_issue(ctx, svc, pid, {**request, "idempotency_key": f"proposal-{i}"})["decision_id"] for i in range(2)]
    barrier = threading.Barrier(2)
    ddb.before_transact_hooks.append(lambda _: barrier.wait(timeout=10))
    results = []
    def worker(i):
        try:
            results.append(resolve_decision(ctx_factory(role_class="operator"), svc, dids[i], _resolve_body(idempotency_key=f"accept-{i}")))
        except PlatformError as exc:
            results.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert len(results) == 2
    assert len([r for r in results if not isinstance(r, PlatformError)]) == 1
    assert [r.code for r in results if isinstance(r, PlatformError)] == ["CONFLICT"]
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 2
    assert [h["revision"] for h in list_history(ctx, svc, pid, {})["history"]] == [2, 1]
    assert sum(get_decision(ctx, svc, d)["decision"]["status"] == "accepted" for d in dids) == 1
    assert len([e for e in svc.repo.audit_events(pid) if e.operation == "resolve_portfolio_decision"]) == 1


def test_legacy_baseline_and_history_pagination_are_immutable(svc, clock, ctx_factory):
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory, legacy=True)
    assert svc.repo.get("portfolio_history", history_id(pid, 1)) is None
    assert list_history(ctx, svc, pid, {})["history"][0]["reason"] == "legacy_baseline"
    for revision in (1, 2):
        book = _book(); book["cash_balance"] -= revision * 10
        put_portfolio_state(ctx, svc, pid, {"paper_state": book, "expected_revision": revision, "idempotency_key": f"operator-{revision}"})
    first = list_history(ctx, svc, pid, {"page_size": 2})
    second = list_history(ctx, svc, pid, {"page_size": 2, "next_token": first["next_token"]})
    assert [h["revision"] for h in first["history"] + second["history"]] == [3, 2, 1]
    assert second["history"][0]["paper_state"] == _book()
    other = create_portfolio(ctx, svc, {"name": "Other", "base_currency": "USD", "idempotency_key": "other"}).response["portfolio_id"]
    with pytest.raises(PlatformError) as exc:
        list_history(ctx, svc, other, {"next_token": first["next_token"]})
    assert exc.value.code == "VALIDATION_FAILED"


@pytest.mark.parametrize("change", [
    {"reference_prices": {"SPY": 1}}, {"reference_date": "2026-01-08"},
    {"target_weights": {"SPY": 0.7, "USD_CASH": 0.7}}, {"transaction_cost_bps": 1001},
])
def test_untrusted_execution_cannot_change_approved_prices_or_accounting(svc, clock, ctx_factory, change):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    request["execution"].update(change)
    with pytest.raises(PlatformError):
        _issue(ctx, svc, pid, request)
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 1
    assert list_decisions(ctx, svc, pid, {})["decisions"] == []


def test_market_history_filters_unapproved_and_paginates(svc, clock, ctx_factory):
    ctx = ctx_factory(role_class="reader")
    approved = []
    for status in ("approved", "committed", "approved", "expired", "approved"):
        clock.advance(seconds=1)
        sid = seed_snapshot(svc, clock, status=status)
        if status == "approved":
            approved.append(sid)
    dataset = svc.repo.require("snapshot_catalog", approved[0]).doc["dataset"]["dataset_id"]
    first = list_approved_snapshots(ctx, svc, {"dataset_id": dataset, "page_size": 2})
    second = list_approved_snapshots(ctx, svc, {"dataset_id": dataset, "page_size": 2, "next_token": first["next_token"]})
    assert [s["input_snapshot_id"] for s in first["snapshots"] + second["snapshots"]] == list(reversed(approved))


def test_activity_is_sanitized_durable_and_binds_authenticated_identity(repo_any, artifacts, clock, ctx_factory):
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    request = {"event_kind": "tool_result", "portfolio_id": pid, "session_id": "durable-session", "correlation_id": ctx.correlation_id,
               "payload": {"tool": "generate_portfolio_recommendation_ppo", "quantity": 10, "authorization": "Bearer secret", "api_key": "hidden",
                           "nested": {"aws_access_key_id": "hidden", "download_grant": {"url": "signed"}, "ok": "retained"},
                           "strings": ["Bearer hidden", "https://example.test/?x-amz-signature=hidden"]}, "idempotency_key": "activity"}
    result = record_activity(ctx, svc, request)
    assert record_activity(ctx, svc, request).replayed
    got = list_activity(ctx, svc, {"portfolio_id": pid})["events"][0]
    text = json.dumps(got)
    assert "hidden" not in text and "secret" not in text
    assert got["payload"]["nested"] == {"ok": "retained"}
    assert got["caller"]["principal"] == ctx.caller.principal and got["payload"]["quantity"] == 10
    assert list_activity(ctx, svc, {"session_id": "durable-session"})["events"][0] == got
    assert got["activity_event_id"] == result.response["activity_event_id"]
    with pytest.raises(PlatformError):
        record_activity(ctx, svc, {**request, "correlation_id": "cor_spoofed", "idempotency_key": "spoof"})


def test_routes_require_model_producer_or_confirmed_authenticated_writer(clients, svc, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    path = f"/v1/portfolios/{pid}/decisions"
    for principal in ("reader", "writer", "fm_job", "gamma_reader"):
        assert getattr(clients, principal).post(path, request)[0] == 403
    code, body, _ = clients.fm_job_api.post(path, request)
    assert code == 201, body
    did = body["decision"]["decision_id"]
    resolve_path = f"/v1/portfolio-decisions/{did}/resolution"
    assert clients.reader.post(resolve_path, _resolve_body())[0] == 403
    assert clients.fm_job_api.post(resolve_path, _resolve_body())[0] == 403
    assert clients.writer.post(resolve_path, _resolve_body())[0] == 403
    caller = {"channel": "hosted_agent", "subject_hash": "sha256:" + "a" * 64, "correlation_id": "cor_verified_user", "roles": ["planner"]}
    headers = {"X-Finplan-Caller": json.dumps(caller)}
    assert clients.writer.post(resolve_path, _resolve_body(confirmed_by_user=False), headers=headers)[0] == 400
    code, resolved, _ = clients.writer.post(resolve_path, _resolve_body(), headers=headers)
    assert code == 200, resolved
    assert resolved["caller"]["principal"] == PRINCIPALS["writer"]
    assert resolved["caller"]["on_behalf_of"] == caller


def test_lifecycle_is_beta_only(svc, clock, ctx_factory):
    ctx = ctx_factory(env="gamma", role_class="operator")
    with pytest.raises(PlatformError) as exc:
        list_activity(ctx, svc, {"session_id": "x"})
    assert exc.value.code == "OPERATION_NOT_PERMITTED"


def test_historical_issuance_is_available_but_cannot_backdate_paper_fills(svc, clock, ctx_factory):
    ctx, pid, sid, request = _setup(svc, clock, ctx_factory)
    observations = load_payloads(svc, svc.repo.require("snapshot_catalog", sid))[0]["observations"]
    earlier = min(observations, key=lambda o: o["session_date"])
    request["execution"].update(reference_date=earlier["session_date"], reference_prices={"SPY": earlier["close"]})
    did = _issue(ctx, svc, pid, request)["decision_id"]
    with pytest.raises(PlatformError) as exc:
        resolve_decision(ctx, svc, did, _resolve_body())
    assert exc.value.code == "CONFLICT" and exc.value.details["reason"] == "stale_reference_date"
    assert get_portfolio_state(ctx, svc, pid)["revision"] == 1


def test_storage_checksum_corruption_is_detected_without_exposing_locations(svc, s3, buckets, clock, ctx_factory):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    did = _issue(ctx, svc, pid, request)["decision_id"]
    record = svc.repo.require("portfolio_decision", did)
    key = record.doc["artifact_key"]
    # Simulate out-of-band object corruption; the public reader must not trust it.
    s3.put_object(Bucket=buckets["reports"], Key=key, Body=b'{"tampered":true}', Metadata={"sha256": record.doc["checksum"][7:]})
    with pytest.raises(PlatformError) as exc:
        get_decision(ctx, svc, did)
    assert exc.value.code == "INTERNAL"
    assert "reports" not in exc.value.message and "s3://" not in exc.value.message


@pytest.mark.parametrize("weight,cost_bps", [(0, 2), (1, 2), (1, 0), (0.2, 1000)])
def test_cash_only_fully_invested_and_high_fee_rebalances_self_finance(svc, clock, ctx_factory, weight, cost_bps):
    ctx, pid, _, request = _setup(svc, clock, ctx_factory)
    request["execution"].update(target_weights={"SPY": weight, "USD_CASH": 1-weight}, transaction_cost_bps=cost_bps)
    did = _issue(ctx, svc, pid, request)["decision_id"]
    got = resolve_decision(ctx, svc, did, _resolve_body()).response
    price = request["execution"]["reference_prices"]["SPY"]
    before_value = 1000 + 20 * price
    after = got["paper_state"]
    after_value = after["cash_balance"] + sum(p["quantity"] * price for p in after["positions"])
    assert after["cash_balance"] >= 0 and after_value + got["transaction_cost"] == pytest.approx(before_value)
    assert after["cash_balance"] == pytest.approx(after_value * (1-weight), abs=1e-7)


def _activity_cursor(value):
    return "ae1_" + base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def test_exact_activity_lookup_returns_one_full_large_event_and_checksum(repo_any, artifacts, clock, ctx_factory):
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    payload = {"large_result": "observable evidence " * 5000, "decision_references": ["retained"]}
    request = {"event_kind": "large_tool_result", "portfolio_id": pid, "session_id": "large-session", "correlation_id": ctx.correlation_id,
               "payload": payload, "idempotency_key": "large-receipt"}
    stored = record_activity(ctx, svc, request).response
    cursor = _activity_cursor({"activity_event_id": stored["activity_event_id"]})
    for partition in ({"portfolio_id": pid}, {"session_id": "large-session"}):
        result = list_activity(ctx, svc, {**partition, "next_token": cursor, "page_size": 1})
        assert validate(result, "api/list-activity-events-response").valid
        assert result["next_token"] is None and len(result["events"]) == 1
        assert result["events"][0]["payload"] == payload
        assert result["events"][0]["checksum"] == stored["checksum"]
        assert result["events"][0]["activity_event_id"] == stored["activity_event_id"]


def test_exact_activity_lookup_cannot_cross_portfolio_or_session_partition(svc, clock, ctx_factory):
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    stored = record_activity(ctx, svc, {"event_kind": "tool_result", "portfolio_id": pid, "session_id": "source-session",
                                      "correlation_id": ctx.correlation_id, "payload": {"private_evidence": 1}, "idempotency_key": "receipt"}).response
    other_pid = create_portfolio(ctx, svc, {"name": "Other", "base_currency": "USD", "idempotency_key": "other"}).response["portfolio_id"]
    cursor = _activity_cursor({"activity_event_id": stored["activity_event_id"]})
    for partition in ({"portfolio_id": other_pid}, {"session_id": "other-session"}):
        with pytest.raises(PlatformError) as exc:
            list_activity(ctx, svc, {**partition, "next_token": cursor})
        assert exc.value.code == "VALIDATION_FAILED" and exc.value.details["pointer"] == "/next_token"


@pytest.mark.parametrize("cursor", [
    "ae1_", "ae1_!!!!", "ae1_8A", "ae1_" + "a" * 257,
    _activity_cursor([]), _activity_cursor({}),
    _activity_cursor({"activity_event_id": 1}),
    _activity_cursor({"activity_event_id": "s3://example-untrusted/path"}),
    _activity_cursor({"activity_event_id": "act_81KES9T7J05DMZFBP5SJAHGHFH"}),
    _activity_cursor({"activity_event_id": "act_01KES9T7J05DMZFBP5SJAHGHFH", "key": "untrusted"}),
])
def test_invalid_reserved_activity_cursor_is_a_validation_error(svc, ctx_factory, cursor):
    with pytest.raises(PlatformError) as exc:
        list_activity(ctx_factory(role_class="reader"), svc, {"session_id": "source-session", "next_token": cursor})
    assert exc.value.code == "VALIDATION_FAILED" and exc.value.details["pointer"] == "/next_token"


def test_exact_activity_lookup_detects_tampered_immutable_payload(svc, s3, buckets, clock, ctx_factory):
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    stored = record_activity(ctx, svc, {"event_kind": "tool_result", "portfolio_id": pid, "correlation_id": ctx.correlation_id,
                                      "payload": {"original": 1}, "idempotency_key": "receipt"}).response
    aid = stored["activity_event_id"]
    record = svc.repo.require("activity_event", aid)
    name = svc.repo.names["activity_event"]
    # The DynamoDB content copy is read first and checksum-verified against the index.
    part = svc.repo.client.get_item(TableName=name, Key={"pk": {"S": f"content#{record.doc['artifact_key']}#0000"}})["Item"]
    svc.repo.client.put_item(TableName=name, Item={**part, "data": {"B": gzip.compress(b'{"tampered":true}', mtime=0)}})
    with pytest.raises(PlatformError) as exc:
        list_activity(ctx, svc, {"portfolio_id": pid, "next_token": _activity_cursor({"activity_event_id": aid})})
    assert exc.value.code == "INTERNAL"
    # Events recorded before mirroring fall back to S3, which is verified the same way.
    svc.repo.client.delete_item(TableName=name, Key={"pk": part["pk"]})
    assert list_activity(ctx, svc, {"portfolio_id": pid})["events"][0]["payload"] == {"original": 1}
    s3.put_object(Bucket=buckets["reports"], Key=record.doc["artifact_key"], Body=b'{"tampered":true}', Metadata={"sha256": stored["checksum"][7:]})
    with pytest.raises(PlatformError) as exc:
        list_activity(ctx, svc, {"portfolio_id": pid, "next_token": _activity_cursor({"activity_event_id": aid})})
    assert exc.value.code == "INTERNAL"


def test_ordinary_activity_pagination_remains_partition_scoped(svc, clock, ctx_factory):
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    ids = []
    for i in range(3):
        ids.append(record_activity(ctx, svc, {"event_kind": "tool_result", "portfolio_id": pid, "correlation_id": ctx.correlation_id,
                                             "payload": {"sequence": i}, "idempotency_key": f"receipt-{i}"}).response["activity_event_id"])
    first = list_activity(ctx, svc, {"portfolio_id": pid, "page_size": 2})
    assert not first["next_token"].startswith("ae1_")
    second = list_activity(ctx, svc, {"portfolio_id": pid, "page_size": 2, "next_token": first["next_token"]})
    assert [e["activity_event_id"] for e in first["events"] + second["events"]] == list(reversed(ids))
    assert second["next_token"] is None


# ---------------------------------------------------------------- tool-call evidence in DynamoDB
def _content_items(svc, key):
    name = svc.repo.names["activity_event"]
    items, i = [], 0
    while True:
        item = svc.repo.client.get_item(TableName=name, Key={"pk": {"S": f"content#{key}#{i:04d}"}}).get("Item")
        if not item:
            return items
        items.append(item); i += 1


def _tool_call(ctx, pid, payload, key, session="agent-session"):
    return {"event_kind": "tool_call", "portfolio_id": pid, "session_id": session, "correlation_id": ctx.correlation_id,
            "payload": payload, "idempotency_key": key}


def test_tool_call_arguments_and_result_are_stored_in_dynamodb(repo_any, artifacts, s3, buckets, clock, ctx_factory):
    from finplan_platform.core import content_mirror
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    payload = {"tool": "generate_portfolio_recommendation_cvar", "arguments": {"instruments": ["AAPL", "VOO"], "alpha": 0.95},
               "result": {"weights": {"AAPL": 0.25, "VOO": 0.75}, "narrative": "x" * 1000}}
    stored = record_activity(ctx, svc, _tool_call(ctx, pid, payload, "tool-1")).response
    record = svc.repo.require("activity_event", stored["activity_event_id"])
    day = clock.now().date().isoformat()
    assert record.attrs["correlation_id"] == ctx.correlation_id and record.attrs["day_key"] == day
    assert record.doc["artifact_key"] == f"activity-events/{day}/{stored['activity_event_id']}.json"
    parts = _content_items(svc, record.doc["artifact_key"])
    assert len(parts) == 1 and parts[0]["record_type"]["S"] == "artifact_content" and parts[0]["checksum"]["S"] == stored["checksum"]
    # No index attributes on content items: they stay out of every GSI.
    assert not {"portfolio_id", "session_id", "correlation_id", "activity_event_id"} & set(parts[0])
    raw = content_mirror.read_content_bytes(svc.repo.client, svc.repo.names["activity_event"], record.doc["artifact_key"],
                                            expected_checksum=stored["checksum"], allowed_prefixes=("activity-events/",))
    doc = json.loads(raw)
    assert doc["payload"] == payload and doc["correlation_id"] == ctx.correlation_id
    # DynamoDB alone serves the read: S3 can disappear.
    s3.delete_object(Bucket=buckets["reports"], Key=record.doc["artifact_key"])
    assert list_activity(ctx, svc, {"session_id": "agent-session"})["events"][0]["payload"] == payload


def test_large_tool_result_is_chunked_and_reassembled(repo_any, artifacts, clock, ctx_factory):
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    payload = {"tool": "big", "result": {"blob": os.urandom(600_000).hex()}}  # ~1.2 MB, incompressible-ish hex
    stored = record_activity(ctx, svc, _tool_call(ctx, pid, payload, "tool-big")).response
    record = svc.repo.require("activity_event", stored["activity_event_id"])
    parts = _content_items(svc, record.doc["artifact_key"])
    assert len(parts) >= 2 and all(len(p["data"]["B"]) <= 350_000 for p in parts)
    assert {int(p["parts"]["N"]) for p in parts} == {len(parts)}
    event = list_activity(ctx, svc, {"portfolio_id": pid})["events"][0]
    assert event["payload"] == payload and event["checksum"] == stored["checksum"]


def test_activity_size_cap_is_four_mib(svc, clock, ctx_factory):
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    stored = record_activity(ctx, svc, _tool_call(ctx, pid, {"blob": "a" * (4 * 1024 * 1024 - 2048)}, "just-fits")).response
    record = svc.repo.require("activity_event", stored["activity_event_id"])
    assert all(len(p["data"]["B"]) <= 350_000 for p in _content_items(svc, record.doc["artifact_key"]))
    with pytest.raises(PlatformError) as exc:
        record_activity(ctx, svc, _tool_call(ctx, pid, {"blob": "a" * (4 * 1024 * 1024)}, "too-big"))
    assert exc.value.code == "VALIDATION_FAILED"


def test_tool_calls_are_found_by_correlation_id_newest_first(repo_any, artifacts, clock, ctx_factory):
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    ids = [record_activity(ctx, svc, _tool_call(ctx, pid, {"n": i}, f"corr-{i}")).response["activity_event_id"] for i in range(3)]
    other = ctx_factory(role_class="operator")
    assert other.correlation_id != ctx.correlation_id
    record_activity(other, svc, _tool_call(other, pid, {"n": 99}, "corr-other"))
    records, token = svc.repo.query_index("activity_event", "correlation-index", ctx.correlation_id)
    assert [r.id for r in records] == list(reversed(ids)) and token is None
    assert all(not r.id.startswith("content#") for r in records)


def test_idempotent_replay_does_not_duplicate_content(repo_any, artifacts, clock, ctx_factory):
    from finplan_platform.core import content_mirror
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    request = _tool_call(ctx, pid, {"tool": "t", "result": {"ok": True}}, "replay")
    first = record_activity(ctx, svc, request)
    again = record_activity(ctx, svc, request)
    assert again.replayed and again.response == first.response
    key = svc.repo.require("activity_event", first.response["activity_event_id"]).doc["artifact_key"]
    assert len(_content_items(svc, key)) == 1
    with pytest.raises(PlatformError):
        record_activity(ctx, svc, {**request, "payload": {"different": 1}})
    # A content retry with the same bytes is a no-op; different bytes are rejected.
    name = svc.repo.names["activity_event"]
    raw = gzip.decompress(bytes(_content_items(svc, key)[0]["data"]["B"]))
    content_mirror.put_content(svc.repo.client, name, key, first.response["checksum"], raw)
    with pytest.raises(PlatformError) as exc:
        content_mirror.put_content(svc.repo.client, name, key, first.response["checksum"], raw + b" ")
    assert exc.value.code == "IMMUTABLE_RECORD"


def test_list_pages_stay_under_the_response_budget(repo_any, artifacts, clock, ctx_factory, monkeypatch):
    from finplan_platform.core import portfolio_lifecycle as pl
    svc = Services(cfg=load_config("beta"), repo=repo_any, artifacts=artifacts)
    ctx, pid, _, _ = _setup(svc, clock, ctx_factory)
    ids = [record_activity(ctx, svc, _tool_call(ctx, pid, {"blob": "b" * 2000, "n": i}, f"budget-{i}")).response["activity_event_id"] for i in range(5)]
    monkeypatch.setattr(pl, "ACTIVITY_PAGE_BUDGET_BYTES", 5000)
    seen, token = [], None
    for _ in range(10):
        page = list_activity(ctx, svc, {"session_id": "agent-session", "page_size": 10, **({"next_token": token} if token else {})})
        assert 1 <= len(page["events"]) <= 2
        seen += [e["activity_event_id"] for e in page["events"]]
        token = page["next_token"]
        if not token:
            break
    assert seen == list(reversed(ids))
