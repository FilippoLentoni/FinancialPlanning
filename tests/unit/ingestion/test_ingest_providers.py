"""Provider adapters (tasks 6.2, 6.12, 6.13): interface, fixture, mock, yfinance adapter (mocked library).

ING-05 (declared capabilities), ING-14 (yfinance normalization fields, finality), ING-17
(rate limiting and backoff). The real ``yfinance`` library is never called: ``fake_yf`` is a
mocked library interface over a synthetic shape fixture, and the socket layer is blocked.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

import pytest
from finplan_platform.core.clock import FrozenClock
from finplan_platform.providers.base import FetchRequest, ProviderAdapter, ProviderThrottled, ProviderUnavailable
from finplan_platform.providers.fixture import FixtureProvider
from finplan_platform.providers.registry import provider_for_config
from finplan_platform.providers.resilience import ResilientCaller, RetryPolicy
from finplan_platform.providers.yfinance_provider import YFinanceProvider

from .conftest import config_with

DS = "finance/etf-daily/SPY"
SETTINGS = {"min_request_interval_seconds": 2.0, "max_attempts": 4, "backoff_initial_seconds": 1.0, "backoff_max_seconds": 8.0, "jitter": True}


def _yf(fake: Any, clock: FrozenClock, **kw: Any) -> YFinanceProvider:
    return YFinanceProvider(dataset_id=DS, ticker="SPY", settings={**SETTINGS, **kw.pop("settings", {})}, clock=clock, sleep=lambda s: clock.advance(seconds=s), rng=random.Random(7), library=fake, function_timeout_seconds=kw.pop("timeout", 120))


# ------------------------------------------------------------------ interface and fixture
def test_every_adapter_implements_the_interface(clock: FrozenClock, mock_provider: Any, fake_yf: Any) -> None:
    for p in (FixtureProvider(dataset_id=DS, instrument_id="SPY", clock=clock), mock_provider(), _yf(fake_yf(), clock)):
        assert isinstance(p, ProviderAdapter)
        caps = p.describe()
        assert caps.datasets == (DS,) and "daily" in caps.granularities
        assert caps.requires_secret is False


def test_fixture_provider_is_deterministic_synthetic_and_daily_only(clock: FrozenClock) -> None:
    a = FixtureProvider(dataset_id=DS, instrument_id="SPY", clock=clock)
    b = FixtureProvider(dataset_id=DS, instrument_id="SPY", clock=clock)
    req = FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9))
    ra, rb = a.fetch(req), b.fetch(req)
    assert ra.body == rb.body and ra.row_count == 6
    recs = a.parse(ra)
    assert all(r.synthetic and r.final is True for r in recs)
    caps = a.describe()
    assert caps.synthetic and caps.granularities == ("daily",) and caps.finality == "explicit"
    assert not caps.supports(DS, "intraday")  # intraday is never inferred from daily


def test_fixture_provider_never_serves_the_open_session(clock: FrozenClock) -> None:
    clock.set("2026-01-12T15:00:00Z")  # 10:00 ET Monday, session open
    p = FixtureProvider(dataset_id=DS, instrument_id="SPY", clock=clock)
    recs = p.parse(p.fetch(FetchRequest(DS, "SPY", date(2026, 1, 9), date(2026, 1, 12))))
    assert [r.session_date for r in recs] == ["2026-01-09"]


def test_mock_programs_throttle_unavailable_empty_gaps_overrides(mock_provider: Any, clock: FrozenClock) -> None:
    m = mock_provider(script=["throttle", "unavailable", "empty"], drop_dates=["2026-01-07"], overrides={"2026-01-08": {"high": 1.0}, "2026-01-06": {"adj_close": None}})
    req = FetchRequest(DS, "SPY", date(2026, 1, 5), date(2026, 1, 9))
    with pytest.raises(ProviderThrottled):
        m.fetch(req)
    with pytest.raises(ProviderUnavailable):
        m.fetch(req)
    assert m.fetch(req).row_count == 0
    recs = {r.session_date: r for r in m.parse(m.fetch(req))}
    assert "2026-01-07" not in recs and recs["2026-01-08"].high == 1.0 and recs["2026-01-06"].adj_close is None
    assert len(m.calls) == 4


def test_intraday_capable_mock_serves_partial_bar_during_session(mock_provider: Any, clock: FrozenClock) -> None:
    clock.set("2026-01-12T16:00:00Z")  # 11:00 ET
    m = mock_provider(intraday=True)
    recs = m.parse(m.fetch(FetchRequest(DS, "SPY", date(2026, 1, 12), date(2026, 1, 12), "intraday")))
    assert len(recs) == 1 and recs[0].bar == "intraday" and recs[0].final is False
    assert m.describe().granularities == ("daily", "intraday")


# ------------------------------------------------------------------ ING-14 yfinance adapter
def test_yfinance_adapter_declares_daily_only_no_secret_inferred_finality(fake_yf: Any, clock: FrozenClock) -> None:
    caps = _yf(fake_yf(), clock).describe()
    assert caps.provider_id == "yfinance" and caps.granularities == ("daily",)
    assert caps.finality == "inferred" and caps.library == "yfinance" and caps.library_version == "1.7.0"
    assert caps.requires_secret is False and caps.synthetic is False


def test_yfinance_adapter_maps_adjusted_close_dividends_and_splits_separately(fake_yf: Any, clock: FrozenClock, no_network: None) -> None:
    fake = fake_yf()
    p = _yf(fake, clock)
    raw = p.fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    call = fake.history_calls[0]
    assert call["interval"] == "1d" and call["auto_adjust"] is False and call["actions"] is True
    assert call["start"] == "2026-01-02" and call["end"] == "2026-01-10"  # end is exclusive in yfinance
    recs = {r.session_date: r for r in p.parse(raw)}
    assert len(recs) == 6
    r = recs["2026-01-07"]
    assert (r.open, r.close, r.adj_close, r.dividend, r.split_ratio) == (103.0, 103.5, 103.25, 1.0, 1.0)
    assert recs["2026-01-08"].split_ratio == 2.0  # a split row keeps its ratio; 0 means "no split" -> 1
    assert all(x.final is None for x in recs.values())  # the library cannot assert finality
    assert isinstance(recs["2026-01-02"].volume, int)


def test_yfinance_adapter_requests_daily_bars_only(fake_yf: Any, clock: FrozenClock) -> None:
    with pytest.raises(ValueError, match="daily"):
        _yf(fake_yf(), clock).fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9), "intraday"))


def test_yfinance_is_imported_lazily_never_at_construction(clock: FrozenClock) -> None:
    p = YFinanceProvider(dataset_id=DS, ticker="SPY", settings=SETTINGS, clock=clock)
    assert p._library is None and p.calls == 0
    assert p.describe().library_version == "1.7.0"  # from installed metadata, without importing


# ------------------------------------------------------------------ ING-17 rate limiting and backoff
def test_throttled_twice_then_success_with_increasing_backoff(fake_yf: Any, clock: FrozenClock) -> None:
    fake = fake_yf(["throttle", "throttle"])
    p = _yf(fake, clock)
    raw = p.fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    assert raw.attempts == 3 and raw.row_count == 6
    backoffs = p.last_caller.backoffs  # type: ignore[union-attr]
    assert len(backoffs) == 2 and backoffs[0] < backoffs[1]
    assert 0.5 <= backoffs[0] <= 1.0 and 1.0 <= backoffs[1] <= 2.0  # equal jitter within the exponential bound


def test_exhausted_attempts_raise_throttled_after_max_attempts(fake_yf: Any, clock: FrozenClock) -> None:
    fake = fake_yf(["throttle"] * 10)
    p = _yf(fake, clock)
    with pytest.raises(ProviderThrottled) as ei:
        p.fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    assert ei.value.attempts == 4 and len(fake.history_calls) == 4 and ei.value.retryable


def test_repeated_network_failures_are_unavailable(fake_yf: Any, clock: FrozenClock) -> None:
    with pytest.raises(ProviderUnavailable):
        _yf(fake_yf(["error"] * 10), clock).fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))


def test_retries_never_wait_past_the_function_timeout(fake_yf: Any, clock: FrozenClock) -> None:
    fake = fake_yf(["throttle"] * 10)
    p = _yf(fake, clock, settings={"max_attempts": 10, "backoff_max_seconds": 30.0}, timeout=20)
    start = clock.now()
    with pytest.raises(ProviderThrottled):
        p.fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    assert (clock.now() - start).total_seconds() <= 20
    assert len(fake.history_calls) < 10


def test_minimum_interval_between_requests(clock: FrozenClock) -> None:
    caller = ResilientCaller(RetryPolicy(max_attempts=1, min_request_interval_seconds=2.0), clock=clock, sleep=lambda s: clock.advance(seconds=s))
    caller.call(lambda: [1])
    caller.call(lambda: [1])
    assert caller.waits == [2.0]


def test_empty_after_retries_is_returned_as_data(fake_yf: Any, clock: FrozenClock) -> None:
    fake = fake_yf(["empty"] * 10)
    raw = _yf(fake, clock).fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    assert raw.empty and raw.attempts == 4


def test_empty_then_data_is_retried(fake_yf: Any, clock: FrozenClock) -> None:
    raw = _yf(fake_yf(["empty"]), clock).fetch(FetchRequest(DS, "SPY", date(2026, 1, 2), date(2026, 1, 9)))
    assert raw.row_count == 6 and raw.attempts == 2


# ------------------------------------------------------------------ ING-10 phase gate at run time
def test_registry_builds_fixture_in_phase_1_and_yfinance_only_in_phase_2(clock: FrozenClock, fake_yf: Any) -> None:
    assert provider_for_config(config_with(), clock=clock).describe().provider_id == "fixture"
    p2 = provider_for_config(config_with(**{"phase": 2, "ingest.provider": "yfinance"}), clock=clock, library=fake_yf())
    assert p2.describe().provider_id == "yfinance"
    # a configuration that slipped past the build gate still cannot reach the real provider
    import json

    from finplan_platform.core.config import EnvConfig, load_config

    doc = json.loads(json.dumps(dict(load_config("beta").data)))
    doc["ingest"]["provider"] = "yfinance"
    with pytest.raises(Exception) as ei:
        provider_for_config(EnvConfig(doc), clock=clock)
    assert getattr(ei.value, "code", None) == "PRECONDITION_FAILED" and ei.value.details["reason"] == "phase_gate"  # type: ignore[attr-defined]
