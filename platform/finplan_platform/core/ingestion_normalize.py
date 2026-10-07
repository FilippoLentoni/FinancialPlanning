"""Normalization, validation and observation-kind classification (tasks 6.3, 6.4, 6.14; ING-04, ING-06, ING-18).

:func:`normalize` turns provider records into contract ``finance/v1/observation.json``
observations and quality findings:

Validation (every finding becomes a quality flag or a rejection, never a silent drop)
-------------------------------------------------------------------------------------
* rejected (``rejected_records`` with the count; the records go to the ``raw`` bucket):
  unexpected instrument, date outside the request, date not aligned with the calendar
  (weekend, holiday, outside coverage), non-monotonic or duplicate dates, missing close,
  negative price or volume, OHLC inconsistency (high below low, open/close outside
  ``[low, high]``), non-positive split ratio, negative dividend, contract schema failure;
* ``missing_sessions``: an expected session (closed, and past the settle delay when finality
  is inferred) for which the provider returned no record;
* ``partial_response``: sessions missing from a non-empty response, or records missing a
  field (open, high, low, volume or adjusted close), each named with the session;
* ``empty_response``: no rows at all for a range that contains expected sessions;
* ``stale_source``: rows exist but the most recent expected session is missing.

Observation kind (ING-04)
-------------------------
* ``completed_daily`` iff the session has closed per the calendar (early closes included)
  **and** the provider marks the bar final; a provider that cannot assert finality
  (``final`` is ``None``) gets ``completed_daily`` only once ``close + settle_delay`` has
  passed, with ``finality_inferred``;
* ``intraday_partial`` only from a provider that declares ``intraday`` (otherwise a not-yet
  final bar is excluded, never relabelled); its snapshot carries ``contains_intraday_partial``.

Normalized observations carry ``session_status`` (``regular``/``early_close``) and
``synthetic`` when the source is synthetic. The curated record adds the provider source
timestamp (the session close when the provider gives none); it never contains the retrieval
time, so the same session ingested by either trigger has identical normalized content (ING-01).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from finplan_contracts.validate import validate

from ..providers.base import ProviderCapabilities, ProviderRecord
from .calendar import SessionCalendar
from .clock import parse_timestamp, to_timestamp

__all__ = ["COMPLETENESS_FIELDS", "PRICE_FIELDS", "NormalizeResult", "NormalizedObservation", "expected_sessions", "normalize"]

PRICE_FIELDS = ("open", "high", "low", "close", "adj_close")
#: fields whose absence makes a response partial (close absence is a rejection)
COMPLETENESS_FIELDS = ("open", "high", "low", "volume", "adj_close")


@dataclass(frozen=True)
class NormalizedObservation:
    observation: dict[str, Any]
    source_ts: str

    @property
    def session_date(self) -> str:
        return str(self.observation["session_date"])

    @property
    def kind(self) -> str:
        return str(self.observation["kind"])

    @property
    def instrument_id(self) -> str:
        return str(self.observation["instrument_id"])


@dataclass
class NormalizeResult:
    observations: list[NormalizedObservation] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    missing_sessions: list[str] = field(default_factory=list)
    partial: list[dict[str, Any]] = field(default_factory=list)
    finality_inferred: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    row_count: int = 0

    def details(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.missing_sessions:
            out["missing_sessions"] = list(self.missing_sessions)
        if self.rejected:
            reasons: dict[str, int] = {}
            for r in self.rejected:
                for reason in r["reasons"]:
                    reasons[reason] = reasons.get(reason, 0) + 1
            out["rejected_records"] = {"count": len(self.rejected), "total_records": self.row_count, "reasons": dict(sorted(reasons.items()))}
        if self.partial:
            out["partial_response"] = list(self.partial)
        if self.finality_inferred:
            out["finality_inferred"] = list(self.finality_inferred)
        if self.excluded:
            out["excluded"] = list(self.excluded)
        if "empty_response" in self.flags:
            out["empty_response"] = {"expected_sessions": len(self.expected)}
        if "stale_source" in self.flags and self.expected:
            out["stale_source"] = {"latest_expected_session": self.expected[-1]}
        return out


def _is_final_kind(rec: ProviderRecord, close_at: datetime, now: datetime, settle: timedelta) -> tuple[str | None, bool]:
    """(kind or None if excluded, inferred?) for a record whose session is a trading session."""
    if rec.bar == "intraday" or now < close_at or rec.final is False:
        return None, False
    if rec.final is True:
        return "completed_daily", False
    if now >= close_at + settle:
        return "completed_daily", True
    return None, False


def expected_sessions(calendar: SessionCalendar, start: date, end: date, *, now: datetime, settle: timedelta, finality: str) -> list[str]:
    """Sessions in range that must be present as completed observations at ``now``."""
    out = []
    for sd in calendar.sessions_between(start, end):
        close_at = calendar.session_close_utc(sd.day)
        due = close_at + settle if finality == "inferred" else close_at
        if now >= due:
            out.append(sd.day.isoformat())
    return out


def _validate_observation(obs: dict[str, Any]) -> list[str]:
    res = validate(obs, "observation")
    return [f"schema:{i.pointer or '/'}" for i in res.issues]


def normalize(
    records: Iterable[ProviderRecord],
    *,
    instrument_id: str,
    start: date,
    end: date,
    calendar: SessionCalendar,
    capabilities: ProviderCapabilities,
    now: datetime,
    settle_delay: timedelta,
    synthetic: bool,
) -> NormalizeResult:
    result = NormalizeResult()
    records = list(records)
    result.row_count = len(records)
    result.expected = expected_sessions(calendar, start, end, now=now, settle=settle_delay, finality=capabilities.finality)
    intraday_ok = "intraday" in capabilities.granularities
    seen: set[str] = set()
    last: date | None = None
    for rec in records:
        reasons: list[str] = []
        d: date | None = None
        if rec.instrument_id != instrument_id:
            reasons.append("unexpected_instrument")
        try:
            d = date.fromisoformat(str(rec.session_date)[:10])
        except ValueError:
            reasons.append("invalid_date")
        if d is not None:
            if not (start <= d <= end):
                reasons.append("outside_requested_range")
            elif not calendar.covers(d):
                reasons.append("outside_calendar_coverage")
            elif not calendar.is_session(d):
                reasons.append(f"calendar_misaligned_{calendar.status(d)}")
            if last is not None and d < last:
                reasons.append("non_monotonic_date")
            elif d.isoformat() in seen:
                reasons.append("duplicate_session")
        if rec.close is None:
            reasons.append("missing_close")
        for f in (*PRICE_FIELDS, "volume", "dividend"):
            v = getattr(rec, f)
            if v is not None and v < 0:
                reasons.append(f"negative_{f}")
        if rec.split_ratio is not None and rec.split_ratio <= 0:
            reasons.append("non_positive_split_ratio")
        hi, lo = rec.high, rec.low
        if hi is not None and lo is not None and hi < lo:
            reasons.append("ohlc_high_below_low")
        else:
            for f in ("open", "close"):
                v = getattr(rec, f)
                if v is None:
                    continue
                if hi is not None and v > hi:
                    reasons.append(f"ohlc_{f}_above_high")
                if lo is not None and v < lo:
                    reasons.append(f"ohlc_{f}_below_low")
        if reasons:
            result.rejected.append({"record": _record_view(rec), "reasons": sorted(set(reasons))})
            continue
        assert d is not None
        last = d
        seen.add(d.isoformat())
        sd = calendar.day(d)
        close_at = calendar.session_close_utc(d)
        kind, inferred = _is_final_kind(rec, close_at, now, settle_delay)
        if kind is None:
            if intraday_ok and now >= calendar.session_open_utc(d):
                kind = "intraday_partial"
            else:
                result.excluded.append({"session_date": d.isoformat(), "reason": "awaiting_settle_delay" if rec.final is None and now >= close_at else "not_final"})
                continue
        obs: dict[str, Any] = {"instrument_id": rec.instrument_id, "session_date": d.isoformat(), "kind": kind, "session_status": sd.status}
        for f in ("open", "high", "low", "close", "adj_close", "dividend", "split_ratio"):
            v = getattr(rec, f)
            if v is not None:
                obs[f] = float(v)
        if rec.volume is not None:
            obs["volume"] = int(rec.volume)
        if synthetic:
            obs["synthetic"] = True
        schema_problems = _validate_observation(obs)
        if schema_problems:
            result.rejected.append({"record": _record_view(rec), "reasons": schema_problems})
            continue
        missing_fields = [f for f in COMPLETENESS_FIELDS if getattr(rec, f) is None]
        if missing_fields and kind == "completed_daily":
            result.partial.append({"session_date": d.isoformat(), "instrument_id": rec.instrument_id, "missing_fields": missing_fields})
        if inferred:
            result.finality_inferred.append(d.isoformat())
        source_ts = to_timestamp(parse_timestamp(rec.source_ts)) if rec.source_ts else to_timestamp(close_at)
        result.observations.append(NormalizedObservation(obs, source_ts))

    present = {o.session_date for o in result.observations if o.kind == "completed_daily"}
    provided = present | {str(r["record"].get("session_date")) for r in result.rejected}
    result.missing_sessions = [s for s in result.expected if s not in provided]
    if result.rejected:
        result.flags.add("rejected_records")
    if result.missing_sessions:
        result.flags.add("missing_sessions")
    if result.row_count == 0 and result.expected:
        result.flags.add("empty_response")
    elif result.missing_sessions or result.partial:
        result.flags.add("partial_response")
        if result.missing_sessions:
            for s in result.missing_sessions:
                result.partial.append({"session_date": s, "instrument_id": instrument_id, "missing_fields": ["session"]})
    if result.row_count and result.expected and result.expected[-1] in result.missing_sessions:
        result.flags.add("stale_source")
    if result.finality_inferred:
        result.flags.add("finality_inferred")
    if any(o.kind == "intraday_partial" for o in result.observations):
        result.flags.add("contains_intraday_partial")
    result.partial.sort(key=lambda p: (p["session_date"], p["instrument_id"]))
    result.observations.sort(key=lambda o: (o.instrument_id, o.session_date, o.kind))
    return result


def _record_view(rec: ProviderRecord) -> dict[str, Any]:
    out = {
        "instrument_id": rec.instrument_id,
        "session_date": rec.session_date,
        "open": rec.open,
        "high": rec.high,
        "low": rec.low,
        "close": rec.close,
        "volume": rec.volume,
        "adj_close": rec.adj_close,
        "dividend": rec.dividend,
        "split_ratio": rec.split_ratio,
        "final": rec.final,
        "source_ts": rec.source_ts,
        "bar": rec.bar,
    }
    if rec.synthetic:
        out["synthetic"] = True
    return out
