"""Snapshot status transitions and reads (task 6.11; ING-12, ING-18, STO-03; API-12 read helpers).

The FinanceModel read decision is simulated against the **synthesized** ``snapshots`` bucket
policy with the object tags actually stored on the snapshot objects (moto), so the test
covers the chain catalog status -> object tag -> bucket policy condition.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts.boundaries import research_permission_boundary
from finplan_contracts.validate import validate
from finplan_platform.core.artifacts import SNAPSHOT_STATUS_TAG, key_snapshot_manifest, key_snapshot_payload
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.ingestion import run_ingestion
from finplan_platform.core.snapshots import evaluate_approval, get_snapshot, read_observations, settle_snapshot_status
from finplan_platform.core.sweeps import expired_snapshot_sweep

from infra.policy_sim import Principal, simulate

from .conftest import config_with, on_demand

ACCT = "<account-id>"
WORST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": "*"}]}


def _fm_read(foundation_synth: dict[str, Any], key: str, tags: dict[str, str]) -> bool:
    bucket = f"finplan-beta-financialplanning-snapshots-{ACCT}"
    policy = foundation_synth["resolver"].bucket_policies()[bucket]
    fm = Principal.role("finplan-beta-financemodel-job-execution-role", WORST, boundary=research_permission_boundary("beta"))
    ctx = {f"s3:ExistingObjectTag/{k}": v for k, v in tags.items()}
    return bool(simulate("s3:GetObject", f"arn:aws:s3:::{bucket}/{key}", fm, resource_policy=policy, context=ctx))


def _tags(deps: Any, sid: str) -> list[dict[str, str]]:
    return [deps.store.get_tags("snapshots", k) for k in (key_snapshot_manifest(sid), key_snapshot_payload(sid, "observations.json"))]


# ------------------------------------------------------------------ ING-12 clean snapshot
def test_clean_fixture_snapshot_is_approved_audited_and_readable_by_financemodel(make_deps: Any, octx: Any, foundation_synth: dict[str, Any]) -> None:
    deps = make_deps()
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    sid = res["input_snapshot_id"]
    snap = res["snapshot"]
    assert snap["status"] == "approved" and snap["approval_rule_version"] == "approval-v1"
    assert validate(snap, "input-snapshot").valid
    events = deps.repo.audit_events(sid)
    assert [e.operation for e in events] == ["commit_snapshot", "approve_snapshot"]
    approve = events[-1]
    assert approve.prior == {"status": "committed"} and approve.new == {"status": "approved"}
    assert approve.details["approval_rule_version"] == "approval-v1" and approve.correlation_id
    for tags in _tags(deps, sid):
        assert tags == {SNAPSHOT_STATUS_TAG: "approved"}
        assert _fm_read(foundation_synth, f"{sid}/manifest.json", tags)


# ------------------------------------------------------------------ ING-12 blocking flag
def test_stale_source_snapshot_stays_committed_and_financemodel_read_denied(make_deps: Any, mock_provider: Any, octx: Any, foundation_synth: dict[str, Any]) -> None:
    deps = make_deps(provider=mock_provider(drop_dates=["2026-01-09"]))  # latest expected session missing
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    sid = res["input_snapshot_id"]
    assert "stale_source" in res["snapshot"]["quality_flags"] and res["snapshot"]["status"] == "committed"
    assert [e.operation for e in deps.repo.audit_events(sid)] == ["commit_snapshot"]
    for tags in _tags(deps, sid):
        assert tags == {SNAPSHOT_STATUS_TAG: "committed"}
        assert not _fm_read(foundation_synth, f"{sid}/payload/observations.json", tags)


# ------------------------------------------------------------------ ING-18 empty response blocks approval
def test_empty_response_snapshot_denied_to_financemodel(make_deps: Any, mock_provider: Any, octx: Any, foundation_synth: dict[str, Any]) -> None:
    deps = make_deps(provider=mock_provider(script=["empty"]))
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    assert "empty_response" in res["snapshot"]["quality_flags"] and res["snapshot"]["status"] == "committed"
    approved, blocking, _ = evaluate_approval(res["snapshot"], deps.config)
    assert not approved and "empty_response" in blocking
    assert not _fm_read(foundation_synth, f"{res['input_snapshot_id']}/manifest.json", _tags(deps, res["input_snapshot_id"])[0])


def test_rejected_records_block_unless_below_the_configured_ratio(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    over = {"2026-01-07": {"high": 1.0, "low": 2.0}}
    strict = make_deps(provider=mock_provider(overrides=over))
    assert run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=strict)["snapshot"]["status"] == "committed"
    lenient = make_deps(provider=mock_provider(overrides=over), cfg=config_with(**{"ingest.approval.rejected_records_max_ratio": 0.5}))
    assert run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=lenient)["snapshot"]["status"] == "approved"


# ------------------------------------------------------------------ tag repair and idempotent settle
def test_settle_repairs_a_missing_approved_tag(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    sid = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09"), deps=deps)["input_snapshot_id"]
    deps.store.put_tags("snapshots", key_snapshot_manifest(sid), {SNAPSHOT_STATUS_TAG: "committed"})  # simulate a lost tag write
    rec = settle_snapshot_status(octx(), deps, sid)
    assert rec.attrs["status"] == "approved"
    assert _tags(deps, sid)[0] == {SNAPSHOT_STATUS_TAG: "approved"}
    assert [e.operation for e in deps.repo.audit_events(sid)].count("approve_snapshot") == 1


def test_approved_snapshot_expires_and_reads_not_found(make_deps: Any, octx: Any, clock: Any) -> None:
    deps = make_deps()
    sid = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09"), deps=deps)["input_snapshot_id"]
    clock.advance(days=15)  # beta snapshot retention: 14 days
    expired_snapshot_sweep(octx(), deps.repo, deps.store, retention_days=deps.config.retention_days("snapshots"))
    with pytest.raises(PlatformError) as ei:
        get_snapshot(octx(), sid, deps=deps)
    assert ei.value.code == "NOT_FOUND" and ei.value.details["reason"] == "expired"


# ------------------------------------------------------------------ reads (API-12 helpers)
def test_get_snapshot_returns_trusted_refs_and_same_manifest_checksum(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    website = get_snapshot(octx(principal="arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-website"), res["input_snapshot_id"], deps=deps)
    agent = get_snapshot(octx(principal="arn:aws:iam::<account-id>:role/finplan-beta-financelambdastool-tool-role-reader"), res["input_snapshot_id"], deps=deps)
    assert website["snapshot"]["manifest_checksum"] == agent["snapshot"]["manifest_checksum"] == res["snapshot"]["manifest_checksum"]
    assert {a["kind"] for a in website["snapshot"]["artifacts"]} == {"snapshot_manifest", "snapshot_payload"}
    assert validate(website["snapshot"], "input-snapshot").valid


def test_financemodel_role_class_reads_only_approved_snapshots(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(script=["empty"]))
    sid = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)["input_snapshot_id"]
    with pytest.raises(PlatformError) as ei:
        get_snapshot(octx(role_class="financemodel-job"), sid, deps=deps)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "snapshot_not_approved"
    assert get_snapshot(octx(), sid, deps=deps)["snapshot"]["status"] == "committed"


def test_invalid_snapshot_id_is_invalid_identifier(make_deps: Any, octx: Any) -> None:
    with pytest.raises(PlatformError) as ei:
        get_snapshot(octx(), "snap_not-a-ulid", deps=make_deps())
    assert ei.value.code == "INVALID_IDENTIFIER"


def test_observation_read_states_uncovered_range_and_never_fabricates(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    sid = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)["input_snapshot_id"]
    out = read_observations(octx(), sid, deps=deps, start_date="2026-01-01", end_date="2026-01-14")
    assert [o["session_date"] for o in out["observations"]] == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    assert out["uncovered_ranges"] == [{"start": "2026-01-01", "end": "2026-01-04"}, {"start": "2026-01-10", "end": "2026-01-14"}]
    assert out["partial"] is True and out["observation_kinds"] == ["completed_daily"]
    assert out["data_refs"][0]["kind"] == "snapshot_payload"
    core = {k: out[k] for k in ("snapshot", "observation_kinds", "instruments", "partial", "data_refs", "next_token")}
    assert validate(core, "tools/query-market-data-response").valid


def test_observation_read_paginates_stably(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    sid = run_ingestion(octx(), on_demand("2025-12-01", "2026-01-09"), deps=deps)["input_snapshot_id"]
    seen: list[str] = []
    token = None
    while True:
        page = read_observations(octx(), sid, deps=deps, page_size=7, next_token=token)
        seen += [o["session_date"] for o in page["observations"]]
        token = page["next_token"]
        if token is None:
            break
    assert seen == sorted(set(seen)) and len(seen) == page["instruments"][0]["observation_count"]
    with pytest.raises(PlatformError) as ei:
        read_observations(octx(), sid, deps=deps, page_size=5, next_token=token or "eyJvIjo3LCJmIjoieCJ9")
    assert ei.value.code == "VALIDATION_FAILED"
    json.dumps(page)  # serializable
