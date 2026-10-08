"""Research-universe ingestion: the ``equity-etf-daily`` dataset (research-universe-dataset; tasks 2.2, 2.3;
UNI-02, UNI-03, UNI-04; design U1-U3).

The dataset ``finance/equity-etf-daily/<subject>`` holds the configured instruments (``ingest.universe``:
VOO, GOOGL, NFLX, AAPL and NVDA plus the modeled ``USD_CASH``). It is served by the same ingestion
operation and triggers as ``etf-daily`` (:func:`.ingestion.ingest` dispatches here by ``dataset_id``),
with these differences:

* **Full-history refetch (U1).** Every run fetches, per non-cash instrument, the complete daily history
  from the effective history start (``history_start``, 2010-10-01, clamped to the session calendar's
  coverage, which only the synthetic fixture calendar limits) through the target session: one request
  per ticker through the provider's rate limiter. Incremental append is not used because the provider
  rewrites past ``adj_close`` after each dividend or split.
* **Revision diffing.** The new observations are compared with the previous snapshot's payload
  (read checksum-verified; snapshots are write-once, so the previous checksum never changes). Changed
  values are flagged ``source_revised`` with a per-instrument summary in ``quality_details``.
* **Modeled cash (U2).** The cash instrument appears in the payload's ``instruments`` and ``universe``
  block with ``return_assumption`` ``zero_nominal`` and has no observations.
* **Disclosures (U3).** The configured ``bias_disclosures`` (``hindsight_selection`` and
  ``survivorship``) are written to the snapshot metadata, payload and manifest.
* **All-or-nothing approval.** ``approval-v2-universe`` (:mod:`.snapshots`) approves only when every
  non-cash instrument has the session's ``completed_daily`` bar and full coverage from the history
  start, with no blocking flag. A ticker that still fails after the provider's retries is recorded
  as missing (``partial_response`` naming it, ``missing_sessions``): the snapshot is committed and
  stays ``committed``, so FinanceModel can never read it. Only when every ticker fails is the error
  raised (``RATE_LIMITED`` / ``DEPENDENCY_UNAVAILABLE``, no snapshot).

Curated data is one write-once object per instrument and run (the normalized full series), not one
object per observation, so a run is a handful of storage writes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from finplan_contracts.canonical import canonicalize
from finplan_contracts.validate import validate

from ..providers.base import (
    FetchRequest,
    ProviderAdapter,
    ProviderThrottled,
    ProviderUnavailable,
)
from .artifacts import (
    SNAPSHOT_STATUS_TAG,
    key_curated,
    key_raw_provider,
    key_snapshot_manifest,
    key_snapshot_payload,
)
from .audit import audit_event
from .calendar import parse_date
from .clock import parse_timestamp, to_timestamp
from .config import UniverseSpec
from .context import OperationContext
from .errors import PlatformError, from_validation
from .ingestion_normalize import NormalizeResult, expected_sessions, normalize
from .repository import Mutation, PutNew
from .snapshots import latest_snapshot, settle_snapshot_status

__all__ = ["DATASET_VERSION", "DETAIL_LIMIT", "UNIVERSE_OPERATION", "effective_history_start", "ingest_universe", "is_universe_request"]

UNIVERSE_OPERATION = "ingest_market_data"
DATASET_VERSION = "universe-daily-norm-v1"
PAYLOAD_NAME = "observations.json"
#: list-valued quality details are capped so the catalog record stays small (counts stay exact)
DETAIL_LIMIT = 100
_COMPARED = ("open", "high", "low", "close", "volume", "adj_close", "dividend", "split_ratio")


def is_universe_request(cfg: Any, body: Any) -> bool:
    u = cfg.universe
    return u is not None and isinstance(body, Mapping) and body.get("dataset_id") == u.dataset_id


def effective_history_start(spec: UniverseSpec, calendar: Any) -> date:
    """The first session on or after ``history_start`` inside the calendar's coverage."""
    start = max(date.fromisoformat(spec.history_start), calendar.coverage_start)
    for sd in calendar.sessions_between(start, min(calendar.coverage_end, date(start.year + 1, start.month, 28))):
        return sd.day
    return start  # pragma: no cover - a calendar without sessions


@dataclass(frozen=True)
class _UPlan:
    spec: UniverseSpec
    start: date
    end: date
    idempotency_key: str
    request_body: dict[str, Any]
    no_session: bool = False
    session_status: str | None = None

    @property
    def requested_range(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def _plan(ctx: OperationContext, body: Mapping[str, Any], deps: Any) -> _UPlan:
    from .ingestion import scheduled_idempotency_key

    spec: UniverseSpec = deps.config.universe
    cal = deps.calendar
    start = effective_history_start(spec, cal)
    if ctx.trigger == "scheduled":
        raw_time = body.get("scheduled_time")
        try:
            scheduled_at = parse_timestamp(str(raw_time)) if raw_time else ctx.now()
        except ValueError:
            raise PlatformError.validation("scheduled_time must be an RFC 3339 timestamp", pointer="/scheduled_time") from None
        fire_date = cal.local_date(scheduled_at)
        cal.require_covered(fire_date)
        key = scheduled_idempotency_key(ctx.env, spec.dataset_id, fire_date)
        request_body = {"trigger": "scheduled", "dataset_id": spec.dataset_id, "scheduled_session_date": fire_date.isoformat(), "granularity": "daily"}
        status = cal.status(fire_date)
        if status not in ("regular", "early_close"):
            return _UPlan(spec, fire_date, fire_date, key, request_body, no_session=True, session_status=status)
        target = cal.latest_closed_session(scheduled_at).day
        return _UPlan(spec, start, target, key, request_body, session_status=status)
    # on demand: contract refresh-market-data-request; the range end names the target session and the
    # whole universe is always refetched from the history start (instrument_ids may only narrow nothing)
    res = validate(dict(body), "tools/refresh-market-data-request")
    if not res.valid:
        raise from_validation(res)
    if body.get("granularity") != "daily":
        raise PlatformError.precondition("the research universe is daily only", reason="provider_capability_unsupported", requested_granularity=body.get("granularity"))
    ids = body.get("instrument_ids")
    if ids is not None and set(ids) != set(spec.tickers):
        raise PlatformError.validation("the research universe is ingested all-or-nothing; omit instrument_ids or list every instrument", pointer="/instrument_ids", allowed=list(spec.tickers))
    end = parse_date(body["end_date"], "/end_date")
    cal.require_covered(end)
    sessions = cal.sessions_between(min(start, end), end)
    return _UPlan(spec, start, end, str(body["idempotency_key"]), dict(body), no_session=not sessions or end < start, session_status=None if sessions else cal.status(end))


def _no_session(ctx: OperationContext, plan: _UPlan, deps: Any) -> dict[str, Any]:
    latest = latest_snapshot(deps.repo, plan.spec.dataset_id)
    resp: dict[str, Any] = {
        "input_snapshot_id": latest.id if latest else None,
        "requested_range": plan.requested_range,
        "coverage_complete": False,
        "new_snapshot": False,
        "quality_flags": ["no_session"],
        "session_status": plan.session_status,
        "trigger": ctx.trigger,
        "synthetic": bool(deps.provider.describe().synthetic),
    }
    if latest is not None:
        resp["snapshot"] = latest.view()
    return resp


def _previous_observations(deps: Any, dataset_id: str) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """(instrument, session date) -> observation of the newest committed/approved snapshot, and its payload checksum."""
    prev = latest_snapshot(deps.repo, dataset_id)
    if prev is None:
        return {}, None
    ref = next((a for a in prev.doc.get("artifacts") or [] if a.get("kind") == "snapshot_payload"), None)
    if ref is None:
        return {}, None
    body, _ = deps.store.get("snapshots", key_snapshot_payload(prev.id, PAYLOAD_NAME), expected_checksum=ref["checksum"])
    payload = json.loads(body)
    return {(o["instrument_id"], o["session_date"]): o for o in payload.get("observations", []) if o.get("kind") == "completed_daily"}, ref["checksum"]


def _revisions(new: list[dict[str, Any]], prev: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for o in new:
        old = prev.get((o["instrument_id"], o["session_date"]))
        if old is None or o.get("kind") != "completed_daily":
            continue
        changed = sorted(f for f in _COMPARED if old.get(f) != o.get(f))
        if changed:
            s = out.setdefault(o["instrument_id"], {"count": 0, "first": o["session_date"], "last": o["session_date"], "fields": []})
            s["count"] += 1
            s["first"], s["last"] = min(s["first"], o["session_date"]), max(s["last"], o["session_date"])
            s["fields"] = sorted(set(s["fields"]) | set(changed))
    return out


def _fetch_one(deps: Any, provider: ProviderAdapter, ctx: OperationContext, plan: _UPlan, ticker: str) -> tuple[NormalizeResult | None, str | None, int]:
    """(normalized result or None when the provider failed after its retries, failure kind, attempts)."""
    caps = provider.describe()
    try:
        raw = provider.fetch(FetchRequest(plan.spec.dataset_id, ticker, plan.start, plan.end, "daily"))
    except ProviderThrottled as exc:
        return None, "RATE_LIMITED", exc.attempts
    except ProviderUnavailable as exc:
        return None, "DEPENDENCY_UNAVAILABLE", exc.attempts
    deps.store.put_once("raw", key_raw_provider(caps.provider_id, plan.spec.dataset_id, raw.retrieved_at.strftime("%Y%m%dT%H%M%SZ"), f"{ctx.ids.ulid()}-{ticker}"), raw.body, raw.content_type)
    try:
        records = provider.parse(raw)
    except (ValueError, KeyError, TypeError):
        return None, "DEPENDENCY_UNAVAILABLE", raw.attempts
    norm = normalize(
        records,
        instrument_id=ticker,
        start=plan.start,
        end=plan.end,
        calendar=deps.calendar,
        capabilities=caps,
        now=raw.retrieved_at,
        settle_delay=deps.settle_delay,
        synthetic=bool(caps.synthetic),
    )
    return norm, None, raw.attempts


def _instrument_entry(i: Mapping[str, Any], currency: str, synthetic: bool) -> dict[str, Any]:
    kind = str(i["kind"])
    out: dict[str, Any] = {"instrument_id": i["instrument_id"], "asset_class": kind, "kind": kind, "currency": currency}
    if "return_assumption" in i:
        out["return_assumption"] = i["return_assumption"]
    if "exchange_mic" in i:
        out["exchange_mic"] = i["exchange_mic"]
    if synthetic:
        out["synthetic"] = True
    return out


def _execute(ctx: OperationContext, plan: _UPlan, deps: Any, providers: Callable[[str], ProviderAdapter]) -> Mutation:
    if deps.budget_gate is not None:
        deps.budget_gate(ctx)  # BUDGET_EXCEEDED before any provider call
    if plan.no_session:
        return Mutation(ops=[], response=_no_session(ctx, plan, deps))
    spec, cal = plan.spec, deps.calendar
    adapters = {t: providers(t) for t in spec.tickers}
    caps = next(iter(adapters.values())).describe()
    if not expected_sessions(cal, plan.end, plan.end, now=ctx.now(), settle=deps.settle_delay, finality=caps.finality):
        raise PlatformError.precondition("the target session has not completed yet", reason="session_not_completed", latest_session=plan.end.isoformat())
    results: dict[str, NormalizeResult | None] = {}
    failures: dict[str, str] = {}
    attempts: dict[str, int] = {}
    for ticker, adapter in adapters.items():
        norm, failure, n = _fetch_one(deps, adapter, ctx, plan, ticker)
        results[ticker], attempts[ticker] = norm, n
        if failure:
            failures[ticker] = failure
    if len(failures) == len(adapters):
        code = "RATE_LIMITED" if "RATE_LIMITED" in failures.values() else "DEPENDENCY_UNAVAILABLE"
        raise PlatformError(code, "the market-data provider failed for every universe instrument; retry later", retryable=True, provider=caps.provider_id)

    synthetic = bool(caps.synthetic)
    target = plan.end.isoformat()
    flags: set[str] = set()
    missing: set[str] = set()
    partial: list[dict[str, Any]] = []
    rejected = 0
    received = 0
    observations: list[dict[str, Any]] = []
    complete: list[str] = []
    per_instrument: dict[str, dict[str, Any]] = {}
    for ticker in spec.tickers:
        norm = results.get(ticker)
        if norm is None:
            flags.update({"partial_response", "missing_sessions"})
            partial.append({"session_date": target, "instrument_id": ticker, "missing_fields": ["session"], "reason": failures[ticker]})
            missing.add(target)
            per_instrument[ticker] = {"completed_daily": 0, "failed": failures[ticker]}
            continue
        flags.update(norm.flags - {"no_new_observations"})
        missing.update(norm.missing_sessions)
        partial.extend(norm.partial)
        rejected += len(norm.rejected)
        received += norm.row_count
        obs = [n.observation for n in norm.observations]
        observations.extend(obs)
        completed = [o for o in obs if o["kind"] == "completed_daily"]
        dates = sorted(o["session_date"] for o in completed)
        has_target = target in dates
        if not has_target:
            flags.add("partial_response")
            if not any(p["instrument_id"] == ticker and p["session_date"] == target for p in partial):
                partial.append({"session_date": target, "instrument_id": ticker, "missing_fields": ["session"]})
        if has_target and not norm.missing_sessions and not norm.rejected and not norm.partial:
            complete.append(ticker)
        per_instrument[ticker] = {"completed_daily": len(completed), "first": dates[0] if dates else None, "last": dates[-1] if dates else None}
    observations.sort(key=lambda o: (o["instrument_id"], o["session_date"], o["kind"]))

    prev, prev_checksum = _previous_observations(deps, spec.dataset_id)
    revised = _revisions(observations, prev)
    if revised:
        flags.add("source_revised")

    details: dict[str, Any] = {}
    if missing:
        details["missing_sessions"] = sorted(missing)[:DETAIL_LIMIT]
    if partial:
        partial.sort(key=lambda p: (p["session_date"], p["instrument_id"]))
        details["partial_response"] = partial[:DETAIL_LIMIT]
    if rejected:
        details["rejected_records"] = {"count": rejected, "total_records": received}
        flags.add("rejected_records")
    if revised:
        details["source_revised"] = {"previous_payload_checksum": prev_checksum, "instruments": revised}
    if "empty_response" in flags:
        details["empty_response"] = {"instruments": sorted(t for t, r in results.items() if r is not None and "empty_response" in r.flags)}

    sid = ctx.new_id("input_snapshot_id")
    retrieved_at: datetime = ctx.now()
    disclosures = spec.disclosures()
    payload: dict[str, Any] = {
        "dataset_id": spec.dataset_id,
        "calendar": cal.exchange,
        "instruments": [_instrument_entry(i, spec.currency, synthetic) for i in spec.instruments],
        "observations": observations,
        "universe": spec.universe_block(),
        "bias_disclosures": disclosures,
    }
    if synthetic:
        payload["synthetic"] = True
    payload_body = canonicalize(payload)
    tags = {SNAPSHOT_STATUS_TAG: "committed"}
    payload_art = deps.store.put_once("snapshots", key_snapshot_payload(sid, PAYLOAD_NAME), payload_body, tags=tags)
    if prev_checksum == payload_art.checksum:
        flags.add("no_new_observations")
    compact = retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    for ticker in spec.tickers:
        series = [o for o in observations if o["instrument_id"] == ticker]
        if series:
            deps.store.put_once_or_verify("curated", key_curated(spec.dataset_id, ticker, target, "full_history", f"{compact}-{sid[-8:]}"), canonicalize({"dataset_id": spec.dataset_id, "instrument_id": ticker, "observations": series}))

    lineage: dict[str, Any] = {
        "provider": caps.provider_id,
        "provider_library": caps.library,
        "library_version": caps.library_version,
        "retrieved_at": to_timestamp(retrieved_at),
        "calendar_version": cal.version,
    }
    firsts = [v["first"] for v in per_instrument.values() if v.get("first")]
    lasts = [v["last"] for v in per_instrument.values() if v.get("last")]
    coverage = {"start": max(firsts), "end": min(lasts)} if firsts and len(firsts) == len(spec.tickers) else plan.requested_range
    if coverage["start"] > coverage["end"]:
        coverage = plan.requested_range
    stamps = sorted({o.get("session_date") for o in observations})
    source_timestamps = {"earliest": f"{stamps[0]}T00:00:00Z", "latest": to_timestamp(retrieved_at)} if stamps else {"earliest": to_timestamp(retrieved_at), "latest": to_timestamp(retrieved_at)}
    completed_total = sum(1 for o in observations if o["kind"] == "completed_daily")
    summary = {
        "completed_daily": completed_total,
        "intraday_partial": len(observations) - completed_total,
        "records_received": received,
        "records_rejected": rejected,
        "instruments_expected": len(spec.tickers),
        "instruments_complete": len(complete),
        "missing_sessions_count": len(missing),
        "source_revised_count": sum(v["count"] for v in revised.values()),
    }
    quality_flags = sorted(flags)
    payload_ref = payload_art.to_ref(ctx.new_id("artifact_id"), "snapshot_payload", synthetic=synthetic if synthetic else None)
    manifest = {
        "manifest_version": "snapshot-manifest-v1",
        "input_snapshot_id": sid,
        "domain": "finance",
        "domain_schema_version": "1.0",
        "dataset": {"dataset_id": spec.dataset_id, "dataset_version": DATASET_VERSION},
        "adjustment_basis": spec.adjustment_basis,
        "return_basis": spec.return_basis,
        "currency": spec.currency,
        "lineage": lineage,
        "calendar": {"exchange": cal.exchange, "version": cal.version, "synthetic": cal.synthetic},
        "requested_range": plan.requested_range,
        "coverage": coverage,
        "source_timestamps": source_timestamps,
        "quality_flags": quality_flags,
        "quality_details": details,
        "observation_summary": summary,
        "instruments": per_instrument,
        "universe": spec.universe_block(),
        "bias_disclosures": disclosures,
        "payload": {"artifact_id": payload_ref["artifact_id"], "checksum": payload_art.checksum, "size_bytes": payload_art.size_bytes},
        "raw_attempts": attempts,
        "trigger": ctx.trigger,
        "created_at": ctx.now_ts(),
        "contract_version": deps.contract_version,
    }
    if synthetic:
        manifest["synthetic"] = True
    manifest_art = deps.store.put_once("snapshots", key_snapshot_manifest(sid), canonicalize(manifest), tags=tags)
    manifest_ref = manifest_art.to_ref(ctx.new_id("artifact_id"), "snapshot_manifest", synthetic=synthetic if synthetic else None)
    doc: dict[str, Any] = {
        "input_snapshot_id": sid,
        "domain": "finance",
        "domain_schema_version": "1.0",
        "dataset": {"dataset_id": spec.dataset_id, "dataset_version": DATASET_VERSION},
        "manifest_checksum": manifest_art.checksum,
        "source_timestamps": source_timestamps,
        "lineage": lineage,
        "coverage": coverage,
        "quality_flags": quality_flags,
        "status": "committed",
        "artifacts": [manifest_ref, payload_ref],
        "created_at": ctx.now_ts(),
        "quality_details": details,
        "observation_summary": summary,
        "bias_disclosures": disclosures,
    }
    if synthetic:
        doc["synthetic"] = True
    res = validate(doc, "input-snapshot")
    if not res.valid:  # pragma: no cover - a platform bug
        raise PlatformError.internal("universe snapshot record failed contract validation", issues=[i.pointer for i in res.issues][:5])
    response: dict[str, Any] = {
        "snapshot": doc,
        "input_snapshot_id": sid,
        "requested_range": plan.requested_range,
        "coverage_complete": len(complete) == len(spec.tickers),
        "new_snapshot": True,
        "content_checksum": payload_art.checksum,
        "trigger": ctx.trigger,
    }
    if synthetic:
        response["synthetic"] = True
    ev = audit_event(ctx, record_id=sid, record_type="snapshot_catalog", operation="commit_snapshot", prior=None, new={"status": "committed"}, dataset_id=spec.dataset_id, manifest_checksum=manifest_art.checksum, quality_flags=quality_flags, provider=caps.provider_id)
    extra = {"trigger": ctx.trigger, "caller_principal": ctx.caller.principal, "correlation_id": ctx.correlation_id, "content_checksum": payload_art.checksum}
    return Mutation(ops=[PutNew("snapshot_catalog", doc=doc, contract_version=deps.contract_version, extra_attrs=extra)], response=response, audit=[ev])


def ingest_universe(ctx: OperationContext, body: Mapping[str, Any], deps: Any, *, providers: Callable[[str], ProviderAdapter] | None = None) -> tuple[dict[str, Any], bool]:
    """Run one universe ingestion; returns (response, replayed)."""
    if providers is None:
        providers = getattr(deps, "universe_providers", None)
    if providers is None:
        from ..providers.registry import provider_for_instrument

        dataset_id = deps.config.universe.dataset_id

        def providers(ticker: str) -> ProviderAdapter:
            return provider_for_instrument(deps.config, dataset_id, ticker, clock=ctx.clock)

    plan = _plan(ctx, body, deps)
    outcome = deps.repo.run_idempotent(ctx, operation=UNIVERSE_OPERATION, idempotency_key=plan.idempotency_key, request_body=plan.request_body, execute=lambda: _execute(ctx, plan, deps, providers))
    response = dict(outcome.response)
    if response.get("new_snapshot") and response.get("input_snapshot_id"):
        rec = settle_snapshot_status(ctx, deps, str(response["input_snapshot_id"]))
        response["snapshot"] = rec.view()
    return response, outcome.replayed
