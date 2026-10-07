"""Deterministic validation (task 5.1; API-05; MDS-04 via the API)."""

from __future__ import annotations

from typing import Any

import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.validation import evaluate_rules, validate_version_now

from tests.api_support import *
from tests.api_support import Flow, content, seed_snapshot


def _rules(body: dict[str, Any]) -> list[str]:
    return [f["details"]["rule"] for f in body["findings"]]


def _new(flow: Flow, svc: Any, clock: Any, c: dict[str, Any], **seed: Any) -> tuple[str, dict[str, Any]]:
    snap = seed_snapshot(svc, clock, **seed)
    plan = flow.plan()
    return plan["plan_id"], flow.root(plan["plan_id"], snap, body_content=c)


def test_valid_version_becomes_validated_with_the_ruleset_version(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    _pid, v = _new(flow, svc, clock, content())
    code, res, _ = clients.writer.post(f"/v1/plan-versions/{v['plan_version_id']}/validate", {"idempotency_key": "val-1"})
    assert code == 200 and validate(res, "tools/validate-plan-version-response").valid
    assert res["status"] == "validated" and res["findings"] == [] and res["validation_ruleset_version"] == "plan-rules-v1"
    _, got, _ = clients.reader.get(f"/v1/plan-versions/{v['plan_version_id']}")
    assert got["plan_version"]["status"] == "validated" and got["plan_version"]["validation_ruleset_version"] == "plan-rules-v1"
    assert got["plan_version"]["checksum"] == v["checksum"]  # content untouched by the transition


def test_weights_summing_to_1_07_are_invalid_and_unpublishable(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, v = _new(flow, svc, clock, content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.67}], "cash_weight": 0.4}, constraints={"long_only": True}))
    res = flow.validate(v["plan_version_id"])
    assert res["status"] == "invalid" and _rules(res) == ["weights_sum"]
    assert res["findings"][0]["pointer"] == "/content/allocation" and res["findings"][0]["code"] == "VALIDATION_FAILED"
    _, got, _ = clients.reader.get(f"/v1/plan-versions/{v['plan_version_id']}")
    assert validate(got["plan_version"], "plan-version").valid and len(got["plan_version"]["validation_errors"]) == 1
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/publications", {"plan_version_id": v["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-inv"})
    assert code == 422 and err["code"] == "PRECONDITION_FAILED" and err["details"]["reason"] == "version_not_validated"


def test_repeated_validation_returns_the_same_result_without_a_new_transition(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    for c in (content(), content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.67}], "cash_weight": 0.4}, constraints={"long_only": True})):
        _pid, v = _new(flow, svc, clock, c)
        first = flow.validate(v["plan_version_id"])
        events = len(svc.repo.audit_events(v["plan_version_id"]))
        stored = svc.repo.require("plan_version", v["plan_version_id"]).doc
        clock.advance(minutes=5)
        second = flow.validate(v["plan_version_id"])  # a new idempotency key: a genuinely repeated request
        assert second == first
        assert len(svc.repo.audit_events(v["plan_version_id"])) == events
        assert svc.repo.require("plan_version", v["plan_version_id"]).doc == stored


def test_invalid_never_becomes_validated(flow: Flow, svc: Any, clock: Any, ctx: Any) -> None:
    _pid, v = _new(flow, svc, clock, content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.67}], "cash_weight": 0.4}, constraints={"long_only": True}))
    flow.validate(v["plan_version_id"])
    with pytest.raises(Exception) as exc:
        svc.repo.transition(ctx, "plan_version", v["plan_version_id"], to_status="validated", operation="force")
    assert getattr(exc.value, "code", None) == "PRECONDITION_FAILED"
    assert validate_version_now(ctx, svc, v["plan_version_id"])["status"] == "invalid"


@pytest.mark.parametrize(
    ("c", "rule"),
    [
        (content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.9}], "cash_weight": 0.1}), "max_weight"),
        (content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.3}, {"instrument_id": "SPY", "weight": 0.3}], "cash_weight": 0.4}), "duplicate_instrument"),
        (content(allocation={"weights": [{"instrument_id": "QQQ", "weight": 0.6}], "cash_weight": 0.4}), "instrument_coverage"),
        (content(base_currency="EUR"), "currency"),
        (content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.1}], "cash_weight": 0.9}, constraints={"min_weight": 0.2}), "min_weight"),
    ],
)
def test_rule_findings(flow: Flow, svc: Any, clock: Any, c: dict[str, Any], rule: str) -> None:
    _pid, v = _new(flow, svc, clock, c)
    res = flow.validate(v["plan_version_id"])
    assert res["status"] == "invalid" and rule in _rules(res)


def test_missing_or_expired_snapshot_is_a_finding(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    plan = flow.plan()
    v = flow.root(plan["plan_id"], "snap_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert _rules(flow.validate(v["plan_version_id"])) == ["snapshot_exists"]
    _pid, v2 = _new(flow, svc, clock, content(), status="expired", doc_overrides={"status": "expired"})
    assert _rules(flow.validate(v2["plan_version_id"])) == ["snapshot_exists"]


def test_tolerance_comes_from_configuration(flow: Flow, svc: Any, clock: Any) -> None:
    _pid, v = _new(flow, svc, clock, content())
    doc = dict(svc.repo.require("plan_version", v["plan_version_id"]).doc)
    near = {**doc, "content": content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.6 + 1e-12}], "cash_weight": 0.4})}
    off = {**doc, "content": content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.6 + 1e-6}], "cash_weight": 0.4})}
    assert evaluate_rules(svc, near) == []
    assert [f["details"]["rule"] for f in evaluate_rules(svc, off)] == ["weights_sum"]
    assert evaluate_rules(svc, off) == evaluate_rules(svc, off)  # deterministic


def test_validation_is_audited_and_internal_entry_point_is_idempotent(flow: Flow, svc: Any, clock: Any, ctx: Any) -> None:
    _pid, v = _new(flow, svc, clock, content())
    r1 = validate_version_now(ctx, svc, v["plan_version_id"])
    r2 = validate_version_now(ctx, svc, v["plan_version_id"])
    assert r1 == r2 and r1["status"] == "validated"
    events = [e for e in svc.repo.audit_events(v["plan_version_id"]) if e.operation == "validate_plan_version"]
    assert len(events) == 1 and events[0].prior == {"status": "pending_validation"} and events[0].new == {"status": "validated"}


def test_tool_reader_cannot_validate(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    _pid, v = _new(flow, svc, clock, content())
    code, err, _ = clients.reader.post(f"/v1/plan-versions/{v['plan_version_id']}/validate", {"idempotency_key": "v-r"})
    assert code == 403 and err["code"] == "FORBIDDEN"
