"""Ingestion test fixtures (INGEST-owned; builds on the FOUNDATION fixtures in ``tests/conftest.py``).

* ``make_deps(provider=..., calendar=..., cfg=..., budget_state=...)``: an
  :class:`IngestionDeps` over the in-memory DynamoDB fake, moto S3 (the six platform
  buckets) and the shared frozen clock; the default provider is the fixture provider with
  the synthetic fixture calendar;
* ``at(ts)``: move the shared frozen clock;
* ``octx`` / ``sctx``: on-demand and scheduled operation contexts;
* ``fake_yf``: a mocked ``yfinance`` library interface backed by the synthetic shape fixture
  (``tests/fixtures/market_data/yfinance_history_shape.json``), with programmable failures.

The real ``yfinance`` library is never called here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from finplan_platform.core.artifacts import ArtifactStore
from finplan_platform.core.calendar import SessionCalendar, calendar_for_provider, fixture_calendar, xnys_calendar
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.config import EnvConfig, load_config, validate_config
from finplan_platform.core.context import OperationContext
from finplan_platform.core.ingestion import IngestionDeps
from finplan_platform.core.ingestion_budget import StaticBudgetGate
from finplan_platform.core.repository import MetadataRepository, create_tables
from finplan_platform.providers.fixture import FixtureProvider
from finplan_platform.providers.mock import MockProvider

from tests.fakes import FakeDynamoDB

SHAPE_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "market_data" / "yfinance_history_shape.json"
OPERATOR = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-operator"


def config_with(env: str = "beta", **changes: Any) -> EnvConfig:
    """A validated configuration with dotted-path overrides, e.g. ``config_with(**{"ingest.provider": "yfinance", "phase": 2})``."""
    doc = json.loads(json.dumps(dict(load_config(env).data)))
    for path, value in changes.items():
        cur = doc
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = value
    problems = validate_config(doc, env=env)
    assert not problems, problems
    return EnvConfig(doc)


@pytest.fixture
def xnys() -> SessionCalendar:
    return xnys_calendar()


@pytest.fixture
def fixcal() -> SessionCalendar:
    return fixture_calendar()


@pytest.fixture
def at(clock: FrozenClock) -> Callable[[str], FrozenClock]:
    def move(ts: str) -> FrozenClock:
        clock.set(ts)
        return clock

    return move


@pytest.fixture
def make_deps(s3: Any, buckets: dict[str, str], clock: FrozenClock) -> Callable[..., IngestionDeps]:
    def make(*, provider: Any = None, calendar: SessionCalendar | None = None, cfg: EnvConfig | None = None, budget_state: Any = None, ddb: Any = None, store: ArtifactStore | None = None) -> IngestionDeps:
        cfg = cfg or load_config("beta")
        if ddb is None:
            ddb = FakeDynamoDB()
            create_tables(ddb, cfg.env)
        repo = MetadataRepository(ddb, cfg.env)
        store = store or ArtifactStore(s3, buckets, clock=clock)
        provider = provider or FixtureProvider(dataset_id=cfg.dataset_id, instrument_id=str(cfg.dataset["instrument"]), clock=clock)
        calendar = calendar or calendar_for_provider(provider.describe().provider_id)
        return IngestionDeps(config=cfg, repo=repo, store=store, provider=provider, calendar=calendar, budget_gate=StaticBudgetGate(budget_state))

    return make


@pytest.fixture
def mock_provider(clock: FrozenClock) -> Callable[..., MockProvider]:
    def make(**kwargs: Any) -> MockProvider:
        kwargs.setdefault("clock", clock)
        return MockProvider(**kwargs)

    return make


@pytest.fixture
def octx(ctx_factory: Callable[..., OperationContext]) -> Callable[..., OperationContext]:
    def make(principal: str = OPERATOR, **kw: Any) -> OperationContext:
        return ctx_factory(principal, trigger="on_demand", **kw)

    return make


@pytest.fixture
def sctx(ctx_factory: Callable[..., OperationContext]) -> Callable[..., OperationContext]:
    def make(**kw: Any) -> OperationContext:
        return ctx_factory("finplan-beta-financialplanning-daily-ingest-schedule-role", trigger="scheduled", channel="scheduler", **kw)

    return make


def on_demand(start: str, end: str, key: str = "k-1", **extra: Any) -> dict[str, Any]:
    return {"dataset_id": "finance/etf-daily/SPY", "start_date": start, "end_date": end, "granularity": "daily", "idempotency_key": key, **extra}


# ------------------------------------------------------------------ mocked yfinance library
class FakeYF:
    """Mocked ``yfinance`` interface: ``Ticker(t).history(**kw)`` -> pandas DataFrame from the shape fixture."""

    FINPLAN_SYNTHETIC = True

    def __init__(self, script: list[str] | None = None, *, rows: list[dict[str, Any]] | None = None, drop_columns: tuple[str, ...] = ()) -> None:
        doc = json.loads(SHAPE_FIXTURE.read_text())
        assert doc["synthetic"] is True
        self.__version__ = doc["library_version"]
        self._doc = doc
        self.rows = rows if rows is not None else doc["rows"]
        self.script = list(script or [])
        self.drop_columns = drop_columns
        self.history_calls: list[dict[str, Any]] = []
        lib = self

        class YFRateLimitError(Exception):
            pass

        class YFPricesMissingError(Exception):
            pass

        self.YFRateLimitError = YFRateLimitError
        self.YFPricesMissingError = YFPricesMissingError

        class Ticker:
            def __init__(self, ticker: str) -> None:
                self.ticker = ticker

            def history(self, **kwargs: Any) -> Any:
                return lib._history(self.ticker, kwargs)

        self.Ticker = Ticker

    def _history(self, ticker: str, kwargs: dict[str, Any]) -> Any:
        import pandas as pd

        self.history_calls.append({"ticker": ticker, **kwargs})
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "throttle":
            raise self.YFRateLimitError("Too Many Requests. Rate limited. Try after a while.")
        if outcome == "error":
            raise ConnectionError("synthetic transient network failure")
        cols = [c for c in self._doc["columns"] if c not in self.drop_columns]
        if outcome == "empty":
            return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="America/New_York", name="Date"))
        start, end = kwargs["start"], kwargs["end"]
        rows = [r for r in self.rows if start <= r["Date"] < end]
        index = pd.DatetimeIndex([pd.Timestamp(r["Date"]).tz_localize("America/New_York") for r in rows], name="Date")
        return pd.DataFrame([[r.get(c) for c in cols] for r in rows], columns=cols, index=index)


@pytest.fixture
def fake_yf() -> Callable[..., FakeYF]:
    return FakeYF
