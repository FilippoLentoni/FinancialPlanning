"""Programmable provider mock for tests (task 6.2; ING-04, ING-05, ING-06, ING-07, ING-17, ING-18).

The mock serves the same closed-form synthetic series as the fixture provider (always
``synthetic: true``) and can be programmed per call and per session:

* ``script``: outcomes consumed one per :meth:`fetch` call: ``"ok"``, ``"throttle"``
  (raises :class:`ProviderThrottled`), ``"unavailable"`` (:class:`ProviderUnavailable`) or
  ``"empty"`` (a response with no rows); afterwards every call is ``"ok"``;
* ``drop_dates``: sessions the provider omits (gaps -> ``missing_sessions``);
* ``overrides``: ``{date: {field: value}}`` per-session changes (a bad bar with ``high`` below
  ``low``, a revised ``close``, ``adj_close: None`` for a missing field);
* ``final``: the finality marker of completed bars (``True``, ``False`` or ``None`` when the
  mock behaves like a provider that cannot assert finality);
* ``intraday``: declare the ``intraday`` granularity; the open session is then served as a
  partial bar (``final`` False, ``bar`` ``intraday``). A daily-only mock never serves it.

``calls`` records every request so tests can assert that no provider call happened.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from ..core.calendar import SessionCalendar, xnys_calendar
from ..core.clock import Clock, SystemClock, to_timestamp
from .base import FetchRequest, ProviderCapabilities, ProviderRecord, ProviderThrottled, ProviderUnavailable, RawResponse, decode_rows, encode_rows
from .fixture import synthetic_bar

__all__ = ["MockProvider"]

_MISSING = object()


class MockProvider:
    def __init__(
        self,
        *,
        dataset_id: str = "finance/etf-daily/SPY",
        instrument_id: str = "SPY",
        calendar: SessionCalendar | None = None,
        clock: Clock | None = None,
        provider_id: str = "mock",
        intraday: bool = False,
        final: bool | None = True,
        script: Iterable[str] = (),
        drop_dates: Iterable[str] = (),
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
        history_start: date = date(2024, 1, 2),
    ) -> None:
        self.provider_id = provider_id
        self.dataset_id = dataset_id
        self.instrument_id = instrument_id
        self.calendar = calendar or xnys_calendar()
        self.clock = clock or SystemClock()
        self.intraday = intraday
        self.final = final
        self.script: list[str] = list(script)
        self.drop_dates: set[str] = set(drop_dates)
        self.overrides: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (overrides or {}).items()}
        self.history_start = history_start
        self.calls: list[FetchRequest] = []
        self._index = {sd.day: i for i, sd in enumerate(s for s in self.calendar.iter_days() if s.is_session)}

    def describe(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            datasets=(self.dataset_id,),
            granularities=("daily", "intraday") if self.intraday else ("daily",),
            history_depth_days=(self.calendar.coverage_end - self.history_start).days,
            rate_limit={"min_request_interval_seconds": 0, "requests_per_minute": None},
            finality="explicit" if self.final is not None else "inferred",
            library="finplan-mock-provider",
            library_version="1.0.0",
            synthetic=True,
        )

    def fetch(self, request: FetchRequest) -> RawResponse:
        self.calls.append(request)
        outcome = self.script.pop(0) if self.script else "ok"
        now: datetime = self.clock.now()
        if outcome == "throttle":
            raise ProviderThrottled("mock provider throttled the request")
        if outcome == "unavailable":
            raise ProviderUnavailable("mock provider is unavailable")
        rows: list[dict[str, Any]] = []
        if outcome == "ok" and request.instrument_id == self.instrument_id and request.dataset_id == self.dataset_id:
            for sd in self.calendar.sessions_between(max(request.start, self.history_start), request.end):
                key = sd.day.isoformat()
                if key in self.drop_dates:
                    continue
                closed = self.calendar.session_close_utc(sd.day) <= now
                if not closed and not self.intraday:
                    continue
                if not closed and self.calendar.session_open_utc(sd.day) > now:
                    continue
                bar: dict[str, Any] = {"date": key, **synthetic_bar(self.instrument_id, self._index[sd.day], sd.day), "synthetic": True}
                if closed:
                    bar["final"] = self.final
                    bar["bar"] = "daily"
                    bar["source_ts"] = to_timestamp(self.calendar.session_close_utc(sd.day))
                else:
                    bar["final"] = False
                    bar["bar"] = "intraday"
                    bar["source_ts"] = to_timestamp(now)
                for f, v in self.overrides.get(key, {}).items():
                    if v is None:
                        bar.pop(f, None)
                    else:
                        bar[f] = v
                rows.append(bar)
        elif outcome not in ("ok", "empty"):
            raise ValueError(f"unknown mock outcome {outcome!r}")
        body = encode_rows(self.provider_id, request, rows, retrieved_at=now, synthetic=True)
        return RawResponse(self.provider_id, body, "application/json", now, attempts=1, row_count=len(rows))

    def parse(self, raw: RawResponse) -> list[ProviderRecord]:
        doc = decode_rows(raw.body)
        instrument = doc["request"]["instrument_id"]
        out = []
        for r in doc["rows"]:
            out.append(
                ProviderRecord(
                    instrument_id=r.get("instrument_id", instrument),
                    session_date=r["date"],
                    open=r.get("open"),
                    high=r.get("high"),
                    low=r.get("low"),
                    close=r.get("close"),
                    volume=r.get("volume"),
                    adj_close=r.get("adj_close"),
                    dividend=r.get("dividend"),
                    split_ratio=r.get("split_ratio"),
                    final=r.get("final"),
                    source_ts=r.get("source_ts"),
                    bar=r.get("bar", "daily"),
                    synthetic=True,
                    raw=r,
                )
            )
        return out
