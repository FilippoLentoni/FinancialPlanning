"""Root and child plan versions (task 4.4, 4.6; API-02 schema violation, API-04, API-08; MDS-02/03 via the API)."""

from __future__ import annotations

import json
from typing import Any

from finplan_contracts.canonical import canonicalize, sha256_hex
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate
from finplan_platform.core.artifacts import key_plan_content
from finplan_platform.core.sweeps import orphan_sweep

from tests.api_support import *
from tests.api_support import Flow, content, seed_snapshot


def _versions(ddb: Any) -> int:
    return len(ddb.scan(TableName="finplan-beta-financialplanning-plan-version")["Items"])


def _setup(flow: Flow, svc: Any, clock: Any) -> tuple[str, str, dict[str, Any]]:
    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    root = flow.root(plan["plan_id"], snap)
    return plan["plan_id"], snap, root


def test_root_version_records_lineage_checksum_and_moves_the_head(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, snap, root = _setup(flow, svc, clock)
    assert root["origin"] == "model_run" and root["parent_plan_version_id"] is None and root["status"] == "pending_validation"
    assert root["checksum"] == "sha256:" + sha256_hex(canonicalize(content()))
    assert root["revision"] == 2 and root["no_effect"] is False
    _, got, _ = clients.reader.get(f"/v1/plan-versions/{root['plan_version_id']}")
    pv = got["plan_version"]
    assert validate(got, "tools/get-plan-version-response").valid
    assert pv["input_snapshot_id"] == snap and pv["run_id"].startswith("run_") and pv["model_version"].startswith("mv_")
    assert pv["content_ref"]["kind"] == "plan_content" and pv["content_ref"]["checksum"] == root["checksum"]
    data, _ = svc.store.get("plans", key_plan_content(pid, root["plan_version_id"]))
    assert "sha256:" + sha256_hex(data) == root["checksum"]  # artifact bytes are the canonical content
    assert flow.head(pid)["head"] == {"current_version_id": root["plan_version_id"], "revision": 2}


def test_manual_override_is_a_child_and_leaves_the_parent_unchanged(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    _, before, _ = clients.reader.get(f"/v1/plan-versions/{root['plan_version_id']}")
    changed = content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.7}], "cash_weight": 0.3})
    code, child, _ = clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, body_content=changed))
    assert code == 201 and validate(child, "tools/create-override-version-response").valid
    assert child["origin"] == "manual_override" and child["parent_plan_version_id"] == root["plan_version_id"]
    assert child["plan_version_id"] != root["plan_version_id"] and child["revision"] == 3
    _, after, _ = clients.reader.get(f"/v1/plan-versions/{root['plan_version_id']}")
    assert after == before
    _, cv, _ = clients.reader.get(f"/v1/plan-versions/{child['plan_version_id']}")
    assert cv["plan_version"]["input_snapshot_id"] == before["plan_version"]["input_snapshot_id"]  # inherited lineage
    assert cv["plan_version"]["run_id"] is None
    assert flow.head(pid)["head"]["current_version_id"] == child["plan_version_id"]


def test_a_child_cannot_name_its_own_lineage(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    """Only a root names input_snapshot_id; a child request (tools/create-override-version-request) inherits it."""
    pid, snap, root = _setup(flow, svc, clock)
    n = _versions(ddb)
    body = {**flow.child_body(root["plan_version_id"], expected_revision=2), "input_snapshot_id": snap}
    code, err, _ = clients.website.post(f"/v1/plans/{pid}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED"
    assert _versions(ddb) == n


def test_contract_tool_request_fixture_is_accepted_unchanged(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    store = load_store()
    fixture = json.loads(next((store.fixtures_dir("tools/create-override-version-request") / "valid").glob("*.json")).read_text())
    fixture.update({"plan_id": pid, "parent_plan_version_id": root["plan_version_id"], "expected_revision": 2})
    assert validate(fixture, "tools/create-override-version-request").valid
    code, body, _ = clients.writer.post(f"/v1/plans/{pid}/versions", fixture)
    assert code == 201, body
    assert validate(body, "tools/create-override-version-response").valid


def test_no_effect_override_creates_a_child_with_the_parent_checksum(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    same_reordered = json.loads(json.dumps(content(), sort_keys=True))  # same content, different key order
    code, child, _ = clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, body_content=same_reordered))
    assert code == 201
    assert child["no_effect"] is True and child["checksum"] == root["checksum"] and child["plan_version_id"] != root["plan_version_id"]


def test_parent_from_another_plan_is_rejected(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    _pid, _snap, root = _setup(flow, svc, clock)
    other = flow.plan()
    code, body, _ = clients.writer.post(f"/v1/plans/{other['plan_id']}/versions", flow.child_body(root["plan_version_id"], expected_revision=1))
    assert code == 400 and body["code"] == "VALIDATION_FAILED" and body["details"]["pointer"] == "/parent_plan_version_id"


def test_client_supplied_plan_version_id_is_rejected(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    body = {**flow.child_body(root["plan_version_id"], expected_revision=2), "plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM"}
    n = _versions(ddb)
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["details"]["pointer"] == "/plan_version_id"
    assert _versions(ddb) == n


def test_override_without_parent_is_rejected(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, snap, _root = _setup(flow, svc, clock)
    body = {**flow.root_body(snap, expected_revision=2), "origin": "manual_override"}
    code, err, _ = clients.website.post(f"/v1/plans/{pid}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED"


def test_schema_violation_names_the_missing_field(flow: Flow, clients: Any) -> None:
    """API-02: a create-version request without input_snapshot_id (a root names its lineage)."""
    plan = flow.plan()
    body = flow.root_body("snap_01KDVDNAZ83BAMMYCEGWF33DPM", expected_revision=1)
    del body["input_snapshot_id"]
    code, err, _ = clients.website.post(f"/v1/plans/{plan['plan_id']}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["retryable"] is False
    assert err["details"]["pointer"] == "/input_snapshot_id"
    assert validate(err, "error").valid


def test_identifier_in_the_wrong_field_is_invalid_identifier(flow: Flow, clients: Any) -> None:
    plan = flow.plan()
    body = flow.child_body(plan["plan_id"], expected_revision=1)  # a pl_ value in parent_plan_version_id
    code, err, _ = clients.writer.post(f"/v1/plans/{plan['plan_id']}/versions", body)
    assert code == 400 and err["code"] == "INVALID_IDENTIFIER" and err["details"]["field"] == "parent_plan_version_id"


def test_storage_locations_in_requests_are_rejected(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    body = {**flow.child_body(root["plan_version_id"], expected_revision=2), "reason": "s3://example-bucket/else"}
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["details"]["pointer"] == "/reason"
    assert "s3://" not in json.dumps(err)


def test_invalid_content_is_rejected_before_anything_is_written(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    bad = content(allocation={"weights": [{"instrument_id": "SPY", "weight": 1.5}]})
    n = _versions(ddb)
    objects = list(svc.store.list_objects("plans"))
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, body_content=bad))
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["details"]["pointer"].startswith("/content/allocation")
    assert _versions(ddb) == n and list(svc.store.list_objects("plans")) == objects


# ------------------------------------------------------------------ API-08
def test_missing_idempotency_key_fails(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    body = flow.child_body(root["plan_version_id"], expected_revision=2)
    del body["idempotency_key"]
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", body)
    assert code == 400 and err["code"] == "VALIDATION_FAILED" and err["details"]["pointer"] == "/idempotency_key"


def test_retry_after_commit_returns_the_original_version(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    body = flow.child_body(root["plan_version_id"], expected_revision=2, key="retry-1")
    c1, first, _ = clients.writer.post(f"/v1/plans/{pid}/versions", body)
    n = _versions(ddb)
    c2, second, h = clients.writer.post(f"/v1/plans/{pid}/versions", body)
    assert c1 == c2 == 201 and first == second and h["X-Idempotent-Replay"] == "true"
    assert _versions(ddb) == n


def test_reused_key_with_a_different_body_fails(flow: Flow, clients: Any, svc: Any, clock: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, key="reuse"))
    other = flow.child_body(root["plan_version_id"], expected_revision=3, key="reuse", body_content=content(fees={"transaction_cost_bps": 7}))
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", other)
    assert code == 422 and err["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_stale_expected_revision_conflicts(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    pid, _snap, root = _setup(flow, svc, clock)
    n = _versions(ddb)
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=1))
    assert code == 409 and err["code"] == "CONFLICT" and err["details"]["current_revision"] == 2
    assert _versions(ddb) == n


def test_concurrent_children_with_the_same_revision_have_one_winner(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    """MDS-03 through the API: both requests pass the early revision check, the transaction decides."""
    pid, _snap, root = _setup(flow, svc, clock)
    n = _versions(ddb)
    second: list[Any] = []

    def interleave(_items: Any) -> None:
        ddb.before_transact_hooks.clear()  # one shot: the second request commits first
        second.append(clients.website.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, body_content=content(fees={"transaction_cost_bps": 2}))))

    ddb.before_transact_hooks.append(interleave)
    code, err, _ = clients.writer.post(f"/v1/plans/{pid}/versions", flow.child_body(root["plan_version_id"], expected_revision=2, body_content=content(fees={"transaction_cost_bps": 1})))
    assert second[0][0] == 201
    assert code == 409 and err["code"] == "CONFLICT"
    assert _versions(ddb) == n + 1
    assert flow.head(pid)["head"] == {"current_version_id": second[0][1]["plan_version_id"], "revision": 3}
    # the loser's content artifact is an invisible orphan that the sweep removes after the grace period
    from finplan_platform.core.context import Caller, OperationContext

    sweep_ctx = OperationContext(caller=Caller("metadata-sweeper"), env="beta", correlation_id="cor_sweeptest", clock=clock)
    assert len(orphan_sweep(sweep_ctx, svc.repo, svc.store, dry_run=True).deleted) == 0
    # moto stamps LastModified with wall-clock time; move the frozen clock past it plus the grace
    from datetime import UTC, datetime, timedelta

    clock.set(datetime.now(UTC) + timedelta(hours=25))
    assert len(orphan_sweep(sweep_ctx, svc.repo, svc.store).deleted) == 1


def test_tool_roles_create_override_children_only(flow: Flow, clients: Any, svc: Any, clock: Any, ddb: Any) -> None:
    snap = seed_snapshot(svc, clock)
    plan = flow.plan()
    n = _versions(ddb)
    code, err, _ = clients.writer.post(f"/v1/plans/{plan['plan_id']}/versions", flow.root_body(snap, expected_revision=1))
    assert code == 403 and err["code"] == "FORBIDDEN" and _versions(ddb) == n
