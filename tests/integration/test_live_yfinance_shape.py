"""Optional live yfinance shape test (task 6.17; ING-19 "Optional live shape test"). OPT-IN ONLY.

Never part of CI, the pipeline suites or prod smoke: it is marked ``live_provider`` and skips
unless an operator sets ``FINPLAN_LIVE_PROVIDER_TEST`` to ``1`` in their own shell for one local
run. It makes exactly one small, rate-limited request (5 calendar days of the configured ETF)
through the platform adapter and asserts **shape only**: the columns, value types, and that every
returned date is an XNYS session of the shipped calendar. It never asserts, prints, stores or
commits a value; the response is discarded.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.live_provider

LIVE = os.environ.get("FINPLAN_LIVE_PROVIDER_TEST") == "1"


@pytest.mark.skipif(not LIVE, reason="opt-in live provider test (operator only, never in CI)")
def test_one_small_request_has_the_expected_shape() -> None:
    import yfinance  # real library, only in this opt-in test
    from finplan_platform.core.calendar import xnys_calendar
    from finplan_platform.providers.base import FetchRequest
    from finplan_platform.providers.yfinance_provider import COLUMNS, YFinanceProvider

    cal = xnys_calendar()
    end = min(date.today() - timedelta(days=1), cal.coverage_end)  # noqa: DTZ011 - local operator run
    start = end - timedelta(days=5)
    settings = {"min_request_interval_seconds": 2.0, "max_attempts": 1, "backoff_initial_seconds": 1.0, "backoff_max_seconds": 1.0, "jitter": False}
    p = YFinanceProvider(dataset_id="finance/etf-daily/SPY", ticker="SPY", settings=settings, library=yfinance, function_timeout_seconds=30)
    raw = p.fetch(FetchRequest("finance/etf-daily/SPY", "SPY", start, end))
    assert p.calls == 1  # exactly one request
    import json

    doc = json.loads(raw.body)
    assert set(doc["columns"]) == set(COLUMNS)
    for row in doc["rows"]:
        assert set(row) >= {"Date", "Open", "High", "Low", "Close", "Volume"}
        assert all(isinstance(row[c], (int, float)) or row[c] is None for c in COLUMNS if c in row)
        assert cal.is_session(date.fromisoformat(row["Date"])), "returned date is not an XNYS session"
    del raw, doc  # never persisted
