"""Daily sweeps (tasks 2.4 and 3.4).

Orphan sweep (plan-metadata-store, "Artifact-before-metadata commit ordering"; MDS-02)
-------------------------------------------------------------------------------------
Artifacts are written before their metadata commit. When a process dies between the two,
the artifact is an *orphan*: invisible (no metadata row, so no API read can reach it) and
garbage-collected here. :func:`orphan_sweep` lists the ``plans`` and ``snapshots``
buckets, maps each key to the record that would own it (``<plan_id>/<plan_version_id>/...``
-> ``plan_version``; ``<input_snapshot_id>/...`` -> ``snapshot_catalog``) and deletes an
artifact only when no row exists **and** it is older than the grace period (24 h by
configuration, far above the Lambda maximum duration, so an in-flight commit is never
touched). Younger orphans are reported as pending. The sweeper role is the only principal
the bucket policies let delete in those buckets.

Expired-snapshot catalog sweep (platform-storage, "Per-environment retention"; STO-06)
-------------------------------------------------------------------------------------
Lifecycle rules expire beta/gamma artifacts. :func:`expired_snapshot_sweep` marks the
catalog row ``expired`` (audited conditional transition) when the snapshot is older than
the environment's ``snapshots`` retention or its manifest object is gone, recording
``expired_at``. :func:`ensure_snapshot_readable` turns an expired row into ``NOT_FOUND``
with details naming the expiry, for every snapshot read path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from .artifacts import ArtifactStore, key_snapshot_manifest
from .clock import parse_timestamp, to_timestamp
from .context import OperationContext
from .errors import PlatformError
from .repository import MetadataRepository, Record

__all__ = ["SweepReport", "orphan_sweep", "expired_snapshot_sweep", "ensure_snapshot_readable", "owner_of_key"]

DEFAULT_GRACE = timedelta(hours=24)


@dataclass
class SweepReport:
    deleted: list[tuple[str, str]] = field(default_factory=list)
    pending: list[tuple[str, str]] = field(default_factory=list)
    kept: int = 0
    unrecognized: list[tuple[str, str]] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # bucket roles and record IDs only; keys never leave the platform logs
        return {
            "deleted": len(self.deleted),
            "pending": len(self.pending),
            "kept": self.kept,
            "unrecognized": len(self.unrecognized),
            "expired_snapshots": list(self.expired),
        }


def owner_of_key(bucket_role: str, key: str) -> tuple[str, str] | None:
    """(table, record_id) that owns an artifact key, or None for an unrecognized key."""
    parts = key.split("/")
    if bucket_role == "plans" and len(parts) >= 3 and parts[0].startswith("pl_") and parts[1].startswith("pv_"):
        return "plan_version", parts[1]
    if bucket_role == "snapshots" and len(parts) >= 2 and parts[0].startswith("snap_"):
        return "snapshot_catalog", parts[0]
    return None


def orphan_sweep(ctx: OperationContext, repo: MetadataRepository, store: ArtifactStore, *, grace: timedelta = DEFAULT_GRACE, dry_run: bool = False) -> SweepReport:
    if grace < DEFAULT_GRACE:
        raise ValueError("the orphan grace period must be at least 24 hours")
    report = SweepReport()
    now = ctx.clock.now()
    exists_cache: dict[tuple[str, str], bool] = {}
    for role in ("plans", "snapshots"):
        for key, last_modified in store.list_objects(role):
            owner = owner_of_key(role, key)
            if owner is None:
                # never delete what we cannot attribute; surface it for a human
                report.unrecognized.append((role, key))
                continue
            if owner not in exists_cache:
                exists_cache[owner] = repo.get(owner[0], owner[1]) is not None
            if exists_cache[owner]:
                report.kept += 1
                continue
            age = now - last_modified
            if age < grace:
                report.pending.append((role, key))
                continue
            if not dry_run:
                store.delete(role, key)
            report.deleted.append((role, key))
    return report


def expired_snapshot_sweep(ctx: OperationContext, repo: MetadataRepository, store: ArtifactStore, *, retention_days: int | None) -> SweepReport:
    """Mark catalog rows ``expired`` when past retention or when the manifest artifact is gone."""
    report = SweepReport()
    now = ctx.clock.now()
    for rec in repo.scan("snapshot_catalog"):
        status = rec.attrs.get("status")
        if status not in ("committed", "approved"):
            continue
        sid = rec.id
        created = parse_timestamp(str(rec.doc.get("created_at")))
        reason = None
        if retention_days is not None and now >= created + timedelta(days=retention_days):
            reason = "retention_elapsed"
        elif not store.exists("snapshots", key_snapshot_manifest(sid)):
            reason = "artifact_expired"
        if reason is None:
            continue
        try:
            repo.transition(
                ctx,
                "snapshot_catalog",
                sid,
                to_status="expired",
                operation="expire_snapshot",
                audit_details={"reason": reason, "retention_days": retention_days},
                extra_attrs={"expired_at": to_timestamp(now), "expiry_reason": reason},
            )
        except PlatformError as exc:
            if exc.code != "PRECONDITION_FAILED":  # a concurrent transition already moved it
                raise
            continue
        report.expired.append(sid)
    return report


def ensure_snapshot_readable(record: Record | None, *, retention_days: Mapping[str, Any] | int | None = None) -> Record:
    """Snapshot read guard: missing or expired -> ``NOT_FOUND`` (details name the expiry)."""
    if record is None:
        raise PlatformError.not_found("snapshot not found", record_type="snapshot")
    if record.attrs.get("status") == "expired" or record.doc.get("status") == "expired":
        details: dict[str, Any] = {"record_type": "snapshot", "reason": "expired", "status": "expired"}
        if record.attrs.get("expired_at"):
            details["expired_at"] = record.attrs["expired_at"]
        if record.attrs.get("expiry_reason"):
            details["expiry_reason"] = record.attrs["expiry_reason"]
        if isinstance(retention_days, int):
            details["retention_days"] = retention_days
        raise PlatformError.not_found("snapshot has expired under the environment retention policy", **details)
    return record
