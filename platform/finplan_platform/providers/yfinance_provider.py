"""``yfinance`` provider adapter (design P6a; tasks 6.12-6.14; ING-14, ING-16, ING-17, ING-18).

Fetches the configured S&P 500 tracking ETF's **daily** history with
``Ticker(<ticker>).history(start, end, interval="1d", auto_adjust=False, actions=True,
raise_errors=True)``: unadjusted ``Open``/``High``/``Low``/``Close``/``Volume`` plus
``Adj Close``, ``Dividends`` and ``Stock Splits``. Unadjusted OHLC is stored with the
configured ``adjustment_basis``; adjusted close, dividend and split ratio stay separate fields
(a ``Stock Splits`` value of ``0`` means "no split" and becomes ratio ``1``).

Recorded caveats (user decision 2026-10-07): ``yfinance`` is unofficial and not affiliated
with Yahoo, needs no API key or secret, is rate-limited, can break when Yahoo changes
upstream, and Yahoo's terms are personal/research use. Consequences here:

* retrieved data goes only to the platform's private per-environment buckets; nothing
  retrieved is ever committed (test fixtures are synthetic shape fixtures);
* the library is imported **lazily** and only when the adapter actually fetches, so phase 1
  (fixture provider) never loads it, and tests inject a mocked library interface;
* requests go through :class:`ResilientCaller` (minimum interval, exponential backoff with
  jitter, bounded by the function timeout); exhausted throttling maps to ``RATE_LIMITED``,
  other repeated failures to ``DEPENDENCY_UNAVAILABLE`` (both retryable) in the ingestion
  operation, and no partial snapshot is committed;
* an empty answer is retried; still empty after the attempts, it is returned as data and
  ingestion flags ``empty_response`` (blocking approval) and ``missing_sessions``;
* the library gives no finality marker: every record has ``final=None``, so ingestion marks a
  bar ``completed_daily`` only after the session close plus the configured settle delay and
  flags ``finality_inferred``.

Only this adapter calls the provider, and only inside the platform ingestion function; it is
enabled in an environment only by a phase 2 configuration (``provider: yfinance``).
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from ..core.clock import Clock, SystemClock
from .base import FetchRequest, ProviderCapabilities, ProviderRecord, ProviderThrottled, ProviderUnavailable, RawResponse, decode_rows, encode_rows
from .resilience import ResilientCaller, RetryPolicy

__all__ = ["COLUMNS", "YFinanceProvider", "pinned_library_version"]

LIBRARY = "yfinance"
#: yfinance column -> observation field
COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Adj Close": "adj_close",
    "Dividends": "dividend",
    "Stock Splits": "split_ratio",
}
_THROTTLE_NAMES = {"YFRateLimitError"}
_EMPTY_NAMES = {"YFPricesMissingError", "YFTzMissingError", "YFTickerMissingError"}


def pinned_library_version() -> str:
    """The pinned ``yfinance`` version (installed distribution metadata; no import of the library)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(LIBRARY)
    except PackageNotFoundError:  # pragma: no cover - the providers extra is not installed
        return "unknown"


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


class YFinanceProvider:
    provider_id = "yfinance"

    def __init__(
        self,
        *,
        dataset_id: str,
        ticker: str,
        settings: Mapping[str, Any],
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        library: Any = None,
        function_timeout_seconds: int | None = None,
        history_depth_days: int = 365 * 30,
    ) -> None:
        self.dataset_id = dataset_id
        self.ticker = ticker
        self.clock = clock or SystemClock()
        self.policy = RetryPolicy.from_settings(settings)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._library = library
        self.function_timeout_seconds = function_timeout_seconds
        self.history_depth_days = history_depth_days
        self.calls = 0
        self.last_caller: ResilientCaller | None = None
        self._caller = ResilientCaller(self.policy, clock=self.clock, sleep=sleep, rng=self._rng)

    # ------------------------------------------------------------ library
    def library(self) -> Any:
        if self._library is None:
            import yfinance  # lazy: only phase 2 environments ever import it

            self._library = yfinance
        return self._library

    def library_version(self) -> str:
        lib = self._library
        v = getattr(lib, "__version__", None) if lib is not None else None
        return str(v) if v else pinned_library_version()

    # ------------------------------------------------------------ interface
    def describe(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            datasets=(self.dataset_id,),
            granularities=("daily",),
            history_depth_days=self.history_depth_days,
            rate_limit={
                "min_request_interval_seconds": self.policy.min_request_interval_seconds,
                "max_attempts": self.policy.max_attempts,
                "backoff_initial_seconds": self.policy.backoff_initial_seconds,
                "backoff_max_seconds": self.policy.backoff_max_seconds,
            },
            finality="inferred",
            library=LIBRARY,
            library_version=self.library_version(),
            synthetic=False,
            requires_secret=False,
        )

    def _fetch_once(self, request: FetchRequest) -> list[dict[str, Any]]:
        self.calls += 1
        lib = self.library()
        try:
            frame = lib.Ticker(self.ticker).history(
                start=request.start.isoformat(),
                end=(request.end + timedelta(days=1)).isoformat(),  # yfinance's end is exclusive
                interval="1d",
                auto_adjust=False,
                actions=True,
                raise_errors=True,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to typed provider errors below
            name = type(exc).__name__
            text = str(exc).lower()
            if name in _THROTTLE_NAMES or "too many requests" in text or "rate limit" in text:
                raise ProviderThrottled("provider rate-limited the request") from None
            if name in _EMPTY_NAMES:
                return []
            raise ProviderUnavailable(f"provider request failed ({name})") from None
        return self._rows(frame)

    @staticmethod
    def _rows(frame: Any) -> list[dict[str, Any]]:
        if frame is None or getattr(frame, "empty", True):
            return []
        columns = [str(c) for c in frame.columns]
        rows: list[dict[str, Any]] = []
        for idx, values in zip(frame.index, frame.itertuples(index=False, name=None)):
            day = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
            row: dict[str, Any] = {"Date": day}
            for col, val in zip(columns, values):
                if col in COLUMNS:
                    row[col] = _num(val)
            rows.append(row)
        return rows

    def fetch(self, request: FetchRequest) -> RawResponse:
        if request.granularity != "daily":
            raise ValueError("the yfinance adapter serves daily bars only")
        caller = self._caller
        if self.function_timeout_seconds:
            caller.deadline = self.clock.now() + timedelta(seconds=self.function_timeout_seconds)
        caller.waits.clear()
        caller.backoffs.clear()
        self.last_caller = caller
        rows, attempts = caller.call(lambda: self._fetch_once(request), is_empty=lambda r: not r)
        now: datetime = self.clock.now()
        body = encode_rows(
            self.provider_id,
            request,
            rows,
            retrieved_at=now,
            # a mocked library interface in tests declares FINPLAN_SYNTHETIC (synthetic shape fixtures)
            synthetic=bool(getattr(self._library, "FINPLAN_SYNTHETIC", False)),
            extra={"provider_library": LIBRARY, "library_version": self.library_version(), "ticker": self.ticker, "attempts": attempts, "columns": list(COLUMNS)},
        )
        return RawResponse(self.provider_id, body, "application/json", now, attempts=attempts, row_count=len(rows), metadata={"backoffs": list(caller.backoffs)})

    def parse(self, raw: RawResponse) -> list[ProviderRecord]:
        doc = decode_rows(raw.body)
        instrument = doc["request"]["instrument_id"]
        out: list[ProviderRecord] = []
        for r in doc["rows"]:
            split = _num(r.get("Stock Splits"))
            vol = _num(r.get("Volume"))
            out.append(
                ProviderRecord(
                    instrument_id=instrument,
                    session_date=str(r["Date"]),
                    open=_num(r.get("Open")),
                    high=_num(r.get("High")),
                    low=_num(r.get("Low")),
                    close=_num(r.get("Close")),
                    volume=int(vol) if vol is not None and float(vol).is_integer() else vol,
                    adj_close=_num(r.get("Adj Close")),
                    dividend=_num(r.get("Dividends")),
                    split_ratio=(1.0 if split == 0.0 else split) if split is not None else None,
                    final=None,  # the library cannot assert finality (settle delay applies)
                    source_ts=None,
                    synthetic=bool(doc.get("synthetic", False)),
                    raw=r,
                )
            )
        return out
