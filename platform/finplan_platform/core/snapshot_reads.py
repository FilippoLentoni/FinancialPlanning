"""Snapshot routes of the plan API (tasks 4.5, 4.8; API-09, API-10, API-12).

The snapshot reads themselves live in :mod:`finplan_platform.core.snapshots` (INGEST-owned:
status, approval, ``get_snapshot`` and the bounded ``read_observations``). This module is the
API-side wrapper the router calls, so there is one implementation of each read:

* :func:`get_snapshot` - ``GET /v1/snapshots/{input_snapshot_id}`` -> ``{"snapshot": <contract
  input-snapshot record>, ...}``; with ``?download=true`` it adds time-limited download grants
  for the snapshot's trusted artifact references (API-09);
* :func:`read_observations` - ``GET /v1/snapshots/{input_snapshot_id}/observations`` -> the
  contract ``query-market-data-response`` shape plus ``observations``, ``requested_range``,
  ``uncovered_ranges`` and ``missing_sessions`` (API-12: only committed content, the uncovered
  range stated, nothing fabricated).

Both validate the request with the pinned contract definitions first (identifier format,
storage-location rejection), lazily import the snapshots module (a release without it fails
only these routes, ``DEPENDENCY_UNAVAILABLE``), and echo the record's stored
``contract_version`` plus read-time defaults (:mod:`.upgrade`) without rewriting the record.

Also here, for the validation rules: :func:`snapshot_instruments` (instruments a snapshot
covers) and :func:`resolve_snapshot_artifacts` (trusted reference -> stored object, matched by
the SHA-256 the artifact store records, independent of payload file names).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any

from .artifacts import key_snapshot_manifest
from .context import OperationContext
from .contract_io import GET_BY_ID_REQUEST, OBSERVATIONS_REQUEST, ref, require_valid
from .errors import PlatformError
from .repository import Record
from .services import Services
from .upgrade import READ_DEFAULTS, serve

__all__ = ["SNAPSHOT_RESPONSE", "get_snapshot", "load_payloads", "read_observations", "resolve_snapshot_artifacts", "snapshot_instruments"]

PAYLOAD_KIND = "snapshot_payload"
SNAPSHOTS_MODULE = "finplan_platform.core.snapshots"

SNAPSHOT_RESPONSE: dict[str, Any] = {
    "x-platform-name": "snapshot-response",
    "type": "object",
    "properties": {
        "snapshot": ref("core/v1/input-snapshot.json"),
        "download_grants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artifact_id": ref("core/v1/artifact-ref.json#/$defs/artifact_id"),
                    "checksum": ref("core/v1/common.json#/$defs/checksum"),
                    "url": {"type": "string", "pattern": "^https://"},
                    "expires_at": ref("core/v1/common.json#/$defs/timestamp"),
                },
                "required": ["artifact_id", "checksum", "url", "expires_at"],
                "additionalProperties": False,
            },
        },
        "synthetic": ref("core/v1/common.json#/$defs/synthetic"),
    },
    "required": ["snapshot"],
}


def latest_approved_snapshot(ctx: OperationContext, svc: Services, query: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve approved-only dataset state; the static /latest route precedes ID matching."""
    require_valid(dict(query), "api/latest-snapshot-request")
    token = None
    while True:
        records, token = svc.repo.query_index("snapshot_catalog", "dataset-index", query["dataset_id"], newest_first=True, limit=25, page_token=token)
        for record in records:
            if record.attrs.get("status") != "approved":
                continue
            try:
                return get_snapshot(ctx, svc, record.id)
            except PlatformError as exc:
                if exc.code != "NOT_FOUND":
                    raise
        if not token:
            break
    raise PlatformError.not_found("no approved snapshot exists for this dataset", dataset_id=query["dataset_id"], reason="approved_snapshot_missing")


def list_approved_snapshots(ctx: OperationContext, svc: Services, query: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded catalog pagination returning only approved, committed snapshots."""
    from .portfolio_lifecycle import _check_beta, _page
    from .upgrade import current_version
    _check_beta(ctx)
    require_valid({"dataset_id": query.get("dataset_id")}, "api/latest-snapshot-request")
    dataset_id = query["dataset_id"]
    limit, token = _page(svc, query, partition="dataset_id", value=dataset_id)
    snapshots = []
    examined = 0
    while len(snapshots) < limit and examined < 500:
        records, token = svc.repo.query_index("snapshot_catalog", "dataset-index", dataset_id, limit=1, page_token=token)
        examined += len(records)
        for record in records:
            if record.attrs.get("status") == "approved":
                try:
                    snapshots.append(get_snapshot(ctx, svc, record.id)["snapshot"])
                except PlatformError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
        if not token:
            break
    return {"snapshots": snapshots, "next_token": token, "contract_version": current_version()}


def _snapshots_module() -> Any:
    try:
        return importlib.import_module(SNAPSHOTS_MODULE)
    except ModuleNotFoundError as exc:  # pragma: no cover - only in a release without the module
        if exc.name == SNAPSHOTS_MODULE:
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "snapshot reads are not available in this release", retryable=False) from None
        raise


def _served_snapshot(svc: Services, input_snapshot_id: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Echo the stored ``contract_version`` and apply read-time defaults (never rewrites the record)."""
    out = dict(snapshot)
    rec = svc.repo.get("snapshot_catalog", input_snapshot_id)
    if rec is not None:
        served = serve(rec, overlay=False)
        if "contract_version" in served:
            out["contract_version"] = served["contract_version"]
    for k, v in READ_DEFAULTS.get("snapshot_catalog", {}).items():
        out.setdefault(k, v)
    return out


# ------------------------------------------------------------------ artifact resolution
def resolve_snapshot_artifacts(svc: Services, rec: Record) -> dict[str, str]:
    """artifact_id -> object key, matched by recorded SHA-256 under the snapshot's own prefix."""
    refs = [r for r in rec.doc.get("artifacts") or [] if isinstance(r, Mapping)]
    if not refs:
        return {}
    store = svc.store
    by_checksum: dict[str, str] = {}
    prefix = key_snapshot_manifest(rec.id).rsplit("/", 1)[0] + "/"
    for key, _modified in store.list_objects("snapshots", prefix):
        head = store.head("snapshots", key)
        if head is not None and head.checksum:
            by_checksum.setdefault(head.checksum, key)
    return {str(r["artifact_id"]): by_checksum[r["checksum"]] for r in refs if r.get("checksum") in by_checksum}


def load_payloads(svc: Services, rec: Record) -> list[dict[str, Any]]:
    """The snapshot's payload documents, bytes verified against their reference checksums."""
    refs = [dict(r) for r in rec.doc.get("artifacts") or [] if isinstance(r, Mapping) and r.get("kind") == PAYLOAD_KIND]
    keys = resolve_snapshot_artifacts(svc, rec) if refs else {}
    out = []
    for r in refs:
        key = keys.get(str(r["artifact_id"]))
        if key is None:
            raise PlatformError.not_found("snapshot payload artifact is not available (expired or not yet written)", record_type="snapshot")
        data, _stored = svc.store.get("snapshots", key, expected_checksum=r["checksum"])
        out.append(json.loads(data))
    return out


def snapshot_instruments(svc: Services, rec: Record) -> set[str]:
    """Instruments a snapshot covers: the payloads' instruments, else the dataset subject."""
    try:
        found = {str(i["instrument_id"]) for p in load_payloads(svc, rec) for i in p.get("instruments", []) if isinstance(i, Mapping) and "instrument_id" in i}
        if found:
            return found
    except (PlatformError, ValueError):
        pass
    dataset_id = str((rec.doc.get("dataset") or {}).get("dataset_id", ""))
    return {dataset_id.rsplit("/", 1)[-1]} if "/" in dataset_id else set()


# ------------------------------------------------------------------ routes
def get_snapshot(ctx: OperationContext, svc: Services, input_snapshot_id: str, *, download: bool = False) -> dict[str, Any]:
    require_valid({"input_snapshot_id": input_snapshot_id}, GET_BY_ID_REQUEST("input_snapshot_id"))
    out = dict(_snapshots_module().get_snapshot(ctx, input_snapshot_id, deps=svc))
    out["snapshot"] = _served_snapshot(svc, input_snapshot_id, out["snapshot"])
    if download:
        rec = svc.repo.require("snapshot_catalog", input_snapshot_id)
        keys = resolve_snapshot_artifacts(svc, rec)
        ttl = int(svc.cfg.limits["download_grant_ttl_seconds"])
        grants = []
        for r in out["snapshot"].get("artifacts") or []:
            key = keys.get(str(r.get("artifact_id")))
            if key is not None:
                g = svc.store.download_grant("snapshots", key, ttl)
                grants.append({"artifact_id": r["artifact_id"], "checksum": r["checksum"], "url": g["url"], "expires_at": g["expires_at"]})
        out["download_grants"] = grants
    return out


def read_observations(ctx: OperationContext, svc: Services, input_snapshot_id: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    q = {k: v for k, v in dict(query or {}).items() if v is not None}
    require_valid({**q, "input_snapshot_id": input_snapshot_id}, OBSERVATIONS_REQUEST)
    out = dict(_snapshots_module().read_observations(ctx, input_snapshot_id, deps=svc, **q))
    out["snapshot"] = _served_snapshot(svc, input_snapshot_id, out["snapshot"])
    return out
