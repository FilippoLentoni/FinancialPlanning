"""Snapshot status, approval and reads (market-data-ingestion "Snapshot approval status"; task 6.11;
plan-lifecycle-api snapshot read routes; ING-12, API-12).

Status lifecycle
----------------
``committed`` (ingestion commit) -> ``approved`` (versioned approval rule) -> ``expired``
(retention sweep, :mod:`.sweeps`); ``committed -> expired`` too. Every change is a conditional
transition with an audit event (:meth:`MetadataRepository.transition`). The catalog status is
mirrored as the object tag ``snapshot-status`` on the manifest and payload objects; the
``snapshots`` bucket policy lets the FinanceModel job role ``GetObject`` only objects tagged
``approved``. The tag is written **after** the transition commits, so an object is never
readable by FinanceModel while the catalog says it is not approved; a failed tag write is
repaired by the next :func:`settle_snapshot_status` call (ingestion replays call it).

Approval rule ``approval-v1`` (configuration ``ingest.approval``)
-----------------------------------------------------------------
A snapshot is approved when it has completed daily observations and none of the blocking
conditions holds: ``stale_source``, ``empty_response``, or ``rejected_records`` above the
configured ratio (``rejected_records_max_ratio``, 0 in every environment by default). The rule
version is recorded in ``approval_rule_version`` and in the audit event.

Reads (for the API routes; the API agent owns routing and authorization)
------------------------------------------------------------------------
* :func:`get_snapshot` -> ``GET /v1/snapshots/{input_snapshot_id}``: the catalog record with
  its trusted artifact references (never bucket names, keys or object version IDs);
* :func:`read_observations` -> ``GET /v1/snapshots/{input_snapshot_id}/observations``: a
  bounded, paginated read filtered by instrument and date range, shaped like the contract
  ``query-market-data-response`` plus ``observations``, ``uncovered_ranges`` and
  ``missing_sessions``; observations are read from the checksum-verified payload, uncovered
  dates are reported and never fabricated (API-12).

Expired snapshots read as ``NOT_FOUND`` with details naming the expiry
(:func:`.sweeps.ensure_snapshot_readable`). ``require_approved`` (default: true for the
FinanceModel role classes) refuses unapproved snapshots with ``PRECONDITION_FAILED``
``snapshot_not_approved``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, Protocol

from .artifacts import SNAPSHOT_STATUS_TAG, ArtifactStore, key_snapshot_manifest, key_snapshot_payload
from .config import EnvConfig
from .context import OperationContext
from .errors import PlatformError
from .repository import MetadataRepository, Record
from .sweeps import ensure_snapshot_readable

__all__ = [
    "APPROVAL_RULES",
    "APPROVED_ONLY_ROLE_CLASSES",
    "evaluate_approval",
    "get_snapshot",
    "latest_snapshot",
    "mirror_status_tags",
    "read_observations",
    "settle_snapshot_status",
]

PAYLOAD_NAME = "observations.json"
APPROVED_ONLY_ROLE_CLASSES = frozenset({"financemodel-job", "financemodel-job-api"})


class _Deps(Protocol):
    config: EnvConfig
    repo: MetadataRepository
    store: ArtifactStore


# ===================================================================== approval rule
def _rule_v1(doc: Mapping[str, Any], *, rejected_max_ratio: float) -> list[str]:
    flags = set(doc.get("quality_flags") or [])
    blocking = sorted(flags & {"stale_source", "empty_response"})
    if "rejected_records" in flags:
        rr = (doc.get("quality_details") or {}).get("rejected_records") or {}
        total = int(rr.get("total_records") or 0)
        ratio = (int(rr.get("count") or 0) / total) if total else 1.0
        if ratio > rejected_max_ratio:
            blocking.append("rejected_records")
    summary = doc.get("observation_summary") or {}
    if int(summary.get("completed_daily", 1) or 0) == 0:
        blocking.append("no_completed_observations")
    return blocking


def _rule_v2_universe(doc: Mapping[str, Any], *, rejected_max_ratio: float) -> list[str]:
    """``approval-v2-universe`` (research-universe-dataset, all-or-nothing; UNI-03): every non-cash
    instrument has the session's completed bar and full coverage from the history start (no
    ``missing_sessions``/``partial_response``), and no blocking flag holds."""
    flags = set(doc.get("quality_flags") or [])
    blocking = sorted(flags & {"stale_source", "empty_response", "partial_response", "missing_sessions"})
    if "rejected_records" in flags:
        rr = (doc.get("quality_details") or {}).get("rejected_records") or {}
        total = int(rr.get("total_records") or 0)
        ratio = (int(rr.get("count") or 0) / total) if total else 1.0
        if ratio > rejected_max_ratio:
            blocking.append("rejected_records")
    summary = doc.get("observation_summary") or {}
    expected = int(summary.get("instruments_expected") or 0)
    if expected == 0 or int(summary.get("instruments_complete") or 0) < expected:
        blocking.append("incomplete_universe")
    if not doc.get("bias_disclosures"):
        blocking.append("missing_bias_disclosures")
    return blocking


APPROVAL_RULES: dict[str, Callable[..., list[str]]] = {"approval-v1": _rule_v1, "approval-v2-universe": _rule_v2_universe}


def evaluate_approval(doc: Mapping[str, Any], cfg: EnvConfig) -> tuple[bool, list[str], str]:
    """(approved?, blocking reasons, rule version) for a catalog record under the rule configured for its
    dataset (``ingest.approval`` for ``etf-daily``; ``ingest.universe.approval`` for the universe)."""
    approval = cfg.ingest["approval"]
    universe = cfg.universe
    if universe is not None and (doc.get("dataset") or {}).get("dataset_id") == universe.dataset_id:
        approval = universe.approval
    version = str(approval["rule_version"])
    rule = APPROVAL_RULES[version]
    blocking = rule(doc, rejected_max_ratio=float(approval["rejected_records_max_ratio"]))
    return not blocking, blocking, version


# ===================================================================== status helpers
def latest_snapshot(repo: MetadataRepository, dataset_id: str, *, statuses: tuple[str, ...] = ("committed", "approved")) -> Record | None:
    """Newest snapshot of a dataset (ULID order) whose status is in ``statuses``."""
    token: str | None = None
    while True:
        records, token = repo.query_index("snapshot_catalog", "dataset-index", dataset_id, newest_first=True, limit=25, page_token=token)
        for rec in records:
            if rec.attrs.get("status") in statuses:
                return rec
        if not token:
            return None


def mirror_status_tags(store: ArtifactStore, input_snapshot_id: str, status: str) -> None:
    for key in (key_snapshot_manifest(input_snapshot_id), key_snapshot_payload(input_snapshot_id, PAYLOAD_NAME)):
        if store.get_tags("snapshots", key).get(SNAPSHOT_STATUS_TAG) != status:
            store.put_tags("snapshots", key, {SNAPSHOT_STATUS_TAG: status})


def settle_snapshot_status(ctx: OperationContext, deps: _Deps, input_snapshot_id: str) -> Record:
    """Apply the approval rule to a ``committed`` snapshot and keep the object tags in step (idempotent)."""
    rec = deps.repo.require("snapshot_catalog", input_snapshot_id)
    if rec.attrs.get("status") == "committed":
        approved, _blocking, version = evaluate_approval(rec.doc, deps.config)
        if approved:
            try:
                rec = deps.repo.transition(
                    ctx,
                    "snapshot_catalog",
                    input_snapshot_id,
                    to_status="approved",
                    changes={"approval_rule_version": version},
                    operation="approve_snapshot",
                    audit_details={"approval_rule_version": version, "input_snapshot_id": input_snapshot_id, "quality_flags": list(rec.doc.get("quality_flags") or [])},
                )
            except PlatformError as exc:
                if exc.code != "PRECONDITION_FAILED":  # a concurrent settle already moved it
                    raise
                rec = deps.repo.require("snapshot_catalog", input_snapshot_id)
    if rec.attrs.get("status") == "approved":
        mirror_status_tags(deps.store, input_snapshot_id, "approved")
    return rec


# ===================================================================== reads
def _require_approved_default(ctx: OperationContext) -> bool:
    return (ctx.caller.role_class or "") in APPROVED_ONLY_ROLE_CLASSES


def _snapshot_id_pattern() -> re.Pattern[str]:
    from finplan_contracts.schemas import load_store

    spec = load_store().resolve_pointer("https://contracts.finplan.invalid/core/v1/identifiers.json#/$defs/input_snapshot_id")
    return re.compile(spec["pattern"])


def _load(ctx: OperationContext, deps: _Deps, input_snapshot_id: Any, require_approved: bool | None) -> Record:
    if not isinstance(input_snapshot_id, str) or not _snapshot_id_pattern().match(input_snapshot_id):
        raise PlatformError("INVALID_IDENTIFIER", "input_snapshot_id is not a valid snapshot identifier", pointer="/input_snapshot_id")
    rec = ensure_snapshot_readable(deps.repo.get("snapshot_catalog", input_snapshot_id), retention_days=deps.config.retention_days("snapshots"))
    must = _require_approved_default(ctx) if require_approved is None else require_approved
    if must and rec.attrs.get("status") != "approved":
        raise PlatformError.precondition("the snapshot is not approved", reason="snapshot_not_approved", status=rec.attrs.get("status"))
    return rec


def get_snapshot(ctx: OperationContext, input_snapshot_id: Any, *, deps: _Deps, require_approved: bool | None = None) -> dict[str, Any]:
    """``GET /v1/snapshots/{input_snapshot_id}``: catalog record (status, lineage, trusted refs)."""
    rec = _load(ctx, deps, input_snapshot_id, require_approved)
    out: dict[str, Any] = {"snapshot": rec.view()}
    if rec.doc.get("synthetic"):
        out["synthetic"] = True
    return out


def _encode_token(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_token(token: str, fingerprint: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        offset = int(data["o"])
        if data["f"] != fingerprint or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise PlatformError.validation("invalid next_token for this query", pointer="/next_token") from None


def _ranges_outside(start: date, end: date, cov_start: date, cov_end: date) -> list[dict[str, str]]:
    out = []
    if start < cov_start:
        out.append({"start": start.isoformat(), "end": min(end, cov_start - timedelta(days=1)).isoformat()})
    if end > cov_end:
        out.append({"start": max(start, cov_end + timedelta(days=1)).isoformat(), "end": end.isoformat()})
    return out


def read_observations(
    ctx: OperationContext,
    input_snapshot_id: Any,
    *,
    deps: _Deps,
    instrument_ids: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page_size: int | None = None,
    next_token: str | None = None,
    require_approved: bool | None = None,
) -> dict[str, Any]:
    """``GET /v1/snapshots/{id}/observations``: bounded page of the snapshot's own observations (API-12)."""
    rec = _load(ctx, deps, input_snapshot_id, require_approved)
    doc = rec.view()
    limit_max = int(deps.config.limits["page_size_max"])
    size = limit_max if page_size is None else page_size
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= limit_max:
        raise PlatformError.validation(f"page_size must be between 1 and {limit_max}", pointer="/page_size")
    cov_start, cov_end = date.fromisoformat(doc["coverage"]["start"]), date.fromisoformat(doc["coverage"]["end"])
    try:
        start = date.fromisoformat(start_date) if start_date else cov_start
        end = date.fromisoformat(end_date) if end_date else cov_end
    except (TypeError, ValueError):
        raise PlatformError.validation("start_date and end_date must be YYYY-MM-DD", pointer="/start_date") from None
    if start > end:
        raise PlatformError.validation("start_date must not be after end_date", pointer="/start_date")
    payload_ref = next(a for a in doc["artifacts"] if a["kind"] == "snapshot_payload")
    body, _ = deps.store.get("snapshots", key_snapshot_payload(rec.id, PAYLOAD_NAME), expected_checksum=payload_ref["checksum"])
    payload = json.loads(body)
    wanted = set(instrument_ids or [])
    rows = [
        o
        for o in payload["observations"]
        if (not wanted or o["instrument_id"] in wanted) and start.isoformat() <= o["session_date"] <= end.isoformat()
    ]
    fingerprint = hashlib.sha256(json.dumps([rec.id, sorted(wanted), start.isoformat(), end.isoformat(), size]).encode()).hexdigest()[:16]
    offset = _decode_token(next_token, fingerprint) if next_token else 0
    page = rows[offset : offset + size]
    more = offset + size < len(rows)
    summaries: dict[str, dict[str, Any]] = {}
    for o in rows:
        s = summaries.setdefault(o["instrument_id"], {"instrument_id": o["instrument_id"], "observation_count": 0, "first_date": o["session_date"], "last_date": o["session_date"], "close_min": o["close"], "close_max": o["close"], "close_last": o["close"]})
        s["observation_count"] += 1
        s["first_date"] = min(s["first_date"], o["session_date"])
        if o["session_date"] >= s["last_date"]:
            s["last_date"], s["close_last"] = o["session_date"], o["close"]
        s["close_min"] = min(s["close_min"], o["close"])
        s["close_max"] = max(s["close_max"], o["close"])
    uncovered = _ranges_outside(start, end, cov_start, cov_end)
    missing = [d for d in (doc.get("quality_details") or {}).get("missing_sessions", []) if start.isoformat() <= d <= end.isoformat()]
    out: dict[str, Any] = {
        "snapshot": doc,
        "observation_kinds": sorted({o["kind"] for o in rows}),
        "instruments": [summaries[k] for k in sorted(summaries)],
        "partial": bool(uncovered or missing),
        "data_refs": [payload_ref],
        "next_token": _encode_token(offset + size, fingerprint) if more else None,
        "observations": page,
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "uncovered_ranges": uncovered,
        "missing_sessions": missing,
    }
    if doc.get("synthetic"):
        out["synthetic"] = True
    return out
