"""Deterministic synthetic fixture provider (phase 1; task 6.2; ING-10, ING-13).

Serves a **synthetic** daily OHLCV series under the configured ETF dataset identity
(``finance/etf-daily/<ticker>``), daily granularity only, flagged ``synthetic: true``. The
series is a closed-form function of the session index in the synthetic fixture calendar, so
every call, trigger and environment sees identical values (ING-01). It models the shape of a
tracking-ETF series (OHLC, volume, adjusted close, quarterly dividends, split ratio 1) and is
never derived from real market data.

Finality is explicit: the fixture marks a bar final once its session has closed per the
calendar (early closes included) at the provider's clock time. The current, still-open
session is never returned (daily only). History starts at :data:`HISTORY_START`, roughly two
years of synthetic sessions before the end of the fixture calendar's first year.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from ..core.calendar import SessionCalendar, fixture_calendar
from ..core.clock import Clock, SystemClock, to_timestamp
from .base import FetchRequest, ProviderCapabilities, ProviderRecord, RawResponse, decode_rows, encode_rows

__all__ = ["FIXTURE_LIBRARY", "FIXTURE_LIBRARY_VERSION", "HISTORY_START", "FixtureProvider", "synthetic_bar"]

HISTORY_START = date(2025, 1, 2)
FIXTURE_LIBRARY = "finplan-fixture-provider"
FIXTURE_LIBRARY_VERSION = "1.0.0"


def synthetic_bar(instrument_id: str, index: int, session_date: date) -> dict[str, Any]:
    """Closed-form synthetic bar for the ``index``-th session (no randomness, no real data)."""
    seed = sum(ord(c) for c in instrument_id) % 17
    trend = 100.0 * (1.0003 ** index)
    wave = 1.0 + 0.02 * math.sin((index + seed) / 9.0) + 0.005 * math.cos((index + seed) / 2.3)
    close = round(trend * wave, 4)
    prev = round(100.0 * (1.0003 ** max(index - 1, 0)) * (1.0 + 0.02 * math.sin((index - 1 + seed) / 9.0) + 0.005 * math.cos((index - 1 + seed) / 2.3)), 4)
    open_ = round((prev + close) / 2.0, 4)
    spread = round(0.004 * close + 0.1 * abs(math.sin(index / 3.0)), 4)
    high = round(max(open_, close) + spread, 4)
    low = round(min(open_, close) - spread, 4)
    volume = 1_000_000 + (index * 7919) % 250_000
    # synthetic quarterly distribution on the third Friday of Mar/Jun/Sep/Dec
    dividend = 1.25 if session_date.month in (3, 6, 9, 12) and session_date.weekday() == 4 and 15 <= session_date.day <= 21 else 0.0
    adj_close = round(close * (1.0 - 0.0001 * (index % 63)), 4)
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "adj_close": adj_close, "dividend": dividend, "split_ratio": 1.0}


class FixtureProvider:
    provider_id = "fixture"

    def __init__(self, *, dataset_id: str, instrument_id: str, calendar: SessionCalendar | None = None, clock: Clock | None = None) -> None:
        self.dataset_id = dataset_id
        self.instrument_id = instrument_id
        self.calendar = calendar or fixture_calendar()
        self.clock = clock or SystemClock()
        self.calls: list[FetchRequest] = []
        self._index = {sd.day: i for i, sd in enumerate(s for s in self.calendar.iter_days() if s.is_session and s.day >= HISTORY_START)}

    def describe(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            datasets=(self.dataset_id,),
            granularities=("daily",),
            history_depth_days=(self.calendar.coverage_end - HISTORY_START).days,
            rate_limit={"min_request_interval_seconds": 0, "requests_per_minute": None},
            finality="explicit",
            library=FIXTURE_LIBRARY,
            library_version=FIXTURE_LIBRARY_VERSION,
            synthetic=True,
        )

    def fetch(self, request: FetchRequest) -> RawResponse:
        self.calls.append(request)
        now: datetime = self.clock.now()
        rows: list[dict[str, Any]] = []
        if request.instrument_id == self.instrument_id and request.dataset_id == self.dataset_id:
            for sd in self.calendar.sessions_between(max(request.start, HISTORY_START), request.end) if request.end >= HISTORY_START else []:
                close_at = self.calendar.session_close_utc(sd.day)
                if close_at > now:
                    continue  # the open session is never served by a daily-only provider
                bar = synthetic_bar(self.instrument_id, self._index[sd.day], sd.day)
                rows.append({"date": sd.day.isoformat(), **bar, "final": True, "source_ts": to_timestamp(close_at), "synthetic": True})
        body = encode_rows(self.provider_id, request, rows, retrieved_at=now, synthetic=True, extra={"calendar_version": self.calendar.version})
        return RawResponse(self.provider_id, body, "application/json", now, attempts=1, row_count=len(rows))

    def parse(self, raw: RawResponse) -> list[ProviderRecord]:
        doc = decode_rows(raw.body)
        instrument = doc["request"]["instrument_id"]
        return [
            ProviderRecord(
                instrument_id=instrument,
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
                synthetic=True,
                raw=r,
            )
            for r in doc["rows"]
        ]
