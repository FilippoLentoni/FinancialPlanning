"""MDS-02 (artifact-before-metadata, orphan sweep) and STO-06 (expired snapshot reads)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
import pytest

from finplan_platform.core.artifacts import ArtifactStore, key_plan_content, key_snapshot_manifest
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.context import Caller, OperationContext
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.repository import MetadataRepository, Mutation, PutNew, version_create_mutation
from finplan_platform.core.sweeps import ensure_snapshot_readable, expired_snapshot_sweep, orphan_sweep
from tests.fakes.records import plan_doc, portfolio_doc, snapshot_doc, version_doc


class Crash(RuntimeError):
    pass


@pytest.fixture
def wall_clock() -> FrozenClock:
    # moto stamps LastModified with the wall clock; the sweep clock starts there
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def wctx(wall_clock: FrozenClock) -> OperationContext:
    return OperationContext(caller=Caller("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-metadata-sweeper-role"), env="beta", correlation_id="cor_sweep0001", clock=wall_clock)


def test_crash_after_artifact_write_leaves_no_visible_version_and_sweep_collects(repo: MetadataRepository, artifacts: ArtifactStore, wctx: OperationContext, wall_clock: FrozenClock) -> None:
    pf = portfolio_doc(wctx)
    pl = plan_doc(wctx, pf["portfolio_id"])
    repo.commit([PutNew("portfolio", doc=pf), PutNew("plan", doc=pl)])
    doc = version_doc(wctx, pl["plan_id"])
    key = key_plan_content(pl["plan_id"], doc["plan_version_id"])

    def execute() -> Mutation:
        artifacts.put_once("plans", key, json.dumps(doc["content"]).encode())  # artifact first
        raise Crash("process died before the metadata commit")

    with pytest.raises(Crash):
        repo.run_idempotent(wctx, operation="create_plan_version", idempotency_key="crash-1", request_body={"x": 1}, execute=execute)
    # no visible version, head unchanged, no idempotency record
    assert repo.get("plan_version", doc["plan_version_id"]) is None
    assert repo.require("plan", pl["plan_id"]).head()["revision"] == 0
    assert artifacts.exists("plans", key)

    # within the grace period the orphan is reported but kept
    wall_clock.advance(hours=1)
    early = orphan_sweep(wctx, repo, artifacts)
    assert early.pending == [("plans", key)] and early.deleted == [] and artifacts.exists("plans", key)

    # after the grace period it is removed
    wall_clock.advance(hours=24)
    late = orphan_sweep(wctx, repo, artifacts)
    assert late.deleted == [("plans", key)]
    assert not artifacts.exists("plans", key)


def test_sweep_keeps_committed_artifacts(repo: MetadataRepository, artifacts: ArtifactStore, wctx: OperationContext, wall_clock: FrozenClock) -> None:
    pf = portfolio_doc(wctx)
    pl = plan_doc(wctx, pf["portfolio_id"])
    repo.commit([PutNew("portfolio", doc=pf), PutNew("plan", doc=pl)])
    doc = version_doc(wctx, pl["plan_id"])
    key = key_plan_content(pl["plan_id"], doc["plan_version_id"])

    def execute() -> Mutation:
        artifacts.put_once("plans", key, b"{}")
        return version_create_mutation(repo, wctx, plan_id=pl["plan_id"], expected_revision=0, version_doc=doc, contract_version="0.1.0", response={"plan_version_id": doc["plan_version_id"]})

    repo.run_idempotent(wctx, operation="create_plan_version", idempotency_key="ok-1", request_body={"y": 1}, execute=execute)
    artifacts.put_once("plans", "notes/unknown.txt", b"?")
    wall_clock.advance(hours=48)
    report = orphan_sweep(wctx, repo, artifacts)
    assert report.deleted == [] and report.kept == 1 and report.unrecognized == [("plans", "notes/unknown.txt")]
    assert artifacts.exists("plans", key)


def test_grace_below_24h_is_rejected(repo: MetadataRepository, artifacts: ArtifactStore, wctx: OperationContext) -> None:
    with pytest.raises(ValueError):
        orphan_sweep(wctx, repo, artifacts, grace=timedelta(hours=1))


# ------------------------------------------------------------------ STO-06 expired catalog reads
def test_expired_snapshot_read_returns_not_found_naming_expiry(repo: MetadataRepository, artifacts: ArtifactStore, ctx: OperationContext, clock: FrozenClock) -> None:
    snap = snapshot_doc(ctx, status="approved")
    repo.commit([PutNew("snapshot_catalog", doc=snap)])
    artifacts.put_once("snapshots", key_snapshot_manifest(snap["input_snapshot_id"]), b"{}")
    assert ensure_snapshot_readable(repo.get("snapshot_catalog", snap["input_snapshot_id"])).id == snap["input_snapshot_id"]

    clock.advance(days=13)
    assert expired_snapshot_sweep(ctx, repo, artifacts, retention_days=14).expired == []
    clock.advance(days=2)
    report = expired_snapshot_sweep(ctx, repo, artifacts, retention_days=14)
    assert report.expired == [snap["input_snapshot_id"]]

    rec = repo.get("snapshot_catalog", snap["input_snapshot_id"])
    with pytest.raises(PlatformError) as ei:
        ensure_snapshot_readable(rec, retention_days=14)
    err = ei.value
    assert err.code == "NOT_FOUND" and err.details["reason"] == "expired" and err.details["expiry_reason"] == "retention_elapsed"
    assert err.details["retention_days"] == 14 and "expired_at" in err.details
    events = repo.audit_events(snap["input_snapshot_id"])
    assert events[-1].operation == "expire_snapshot" and events[-1].new == {"status": "expired"}
    # the sweep is idempotent
    assert expired_snapshot_sweep(ctx, repo, artifacts, retention_days=14).expired == []


def test_prod_without_retention_expires_only_when_artifact_is_gone(repo: MetadataRepository, artifacts: ArtifactStore, ctx: OperationContext, clock: FrozenClock) -> None:
    kept = snapshot_doc(ctx)
    gone = snapshot_doc(ctx)
    repo.commit([PutNew("snapshot_catalog", doc=kept), PutNew("snapshot_catalog", doc=gone)])
    artifacts.put_once("snapshots", key_snapshot_manifest(kept["input_snapshot_id"]), b"{}")
    clock.advance(days=3650)
    report = expired_snapshot_sweep(ctx, repo, artifacts, retention_days=None)
    assert report.expired == [gone["input_snapshot_id"]]
    with pytest.raises(PlatformError) as ei:
        ensure_snapshot_readable(repo.get("snapshot_catalog", gone["input_snapshot_id"]))
    assert ei.value.details["expiry_reason"] == "artifact_expired"


def test_missing_snapshot_is_not_found() -> None:
    with pytest.raises(PlatformError) as ei:
        ensure_snapshot_readable(None)
    assert ei.value.code == "NOT_FOUND"
