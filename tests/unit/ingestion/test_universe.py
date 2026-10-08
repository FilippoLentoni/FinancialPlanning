"""Research-universe dataset ``equity-etf-daily`` (change add-research-universe-and-daily-loop):
UNI-01 (registry and configuration), UNI-02 (full-history fetch, split continuity, revisions),
UNI-03 (all-or-nothing approval), UNI-04 (contract shape and disclosures), UNI-06 (promotion gate).

Every provider response is synthetic: the fixture provider or the mocked ``yfinance`` interface
(``FakeYF``). The real library is never called."""

from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any

import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.artifacts import (
    SNAPSHOT_STATUS_TAG,
    key_snapshot_manifest,
    key_snapshot_payload,
)
from finplan_platform.core.calendar import xnys_calendar
from finplan_platform.core.config import (
    EnvConfig,
    load_all,
    load_config,
    phase2_promotion_problems,
    validate_config,
)
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.ingestion import run_ingestion
from finplan_platform.core.ingestion_universe import effective_history_start
from finplan_platform.core.snapshots import get_snapshot
from finplan_platform.providers.fixture import FixtureProvider
from finplan_platform.providers.yfinance_provider import YFinanceProvider

from .conftest import FakeYF, config_with

UNIVERSE = "finance/equity-etf-daily/research-universe"
TICKERS = ("VOO", "GOOGL", "NFLX", "AAPL", "NVDA")
SCHED = "2026-01-12T14:00:00Z"  # Monday 09:00 America/New_York -> target session Friday 2026-01-09


def _doc(env: str = "beta") -> dict[str, Any]:
    return json.loads(json.dumps(dict(load_config(env).data)))


# ------------------------------------------------------------------ UNI-01
def test_both_datasets_configured_in_every_environment() -> None:
    for env, cfg in load_all().items():
        assert cfg.dataset_id == "finance/etf-daily/SPY", env  # etf-daily unchanged
        u = cfg.universe
        assert u is not None and u.dataset_id == UNIVERSE
        assert u.tickers == TICKERS
        cash = [i for i in u.instruments if i["kind"] == "cash"]
        assert cash == [{"instrument_id": "USD_CASH", "kind": "cash", "return_assumption": "zero_nominal"}]
        assert {i["instrument_id"]: i["kind"] for i in u.instruments if i["kind"] != "cash"} == {"VOO": "etf", "GOOGL": "equity", "NFLX": "equity", "AAPL": "equity", "NVDA": "equity"}
        assert u.history_start == "2010-10-01" and u.return_basis == "adj_close"
        assert {d["kind"] for d in u.disclosures()} == {"hindsight_selection", "survivorship"}
        assert cfg.dataset_ids == ("finance/etf-daily/SPY", UNIVERSE)


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda u: u["instruments"][1].pop("kind"), "kind"),
        (lambda u: u.__setitem__("bias_disclosures", u["bias_disclosures"][:1]), "survivorship"),
        (lambda u: u.__setitem__("bias_disclosures", []), "hindsight_selection"),
        (lambda u: u["instruments"][-1].pop("return_assumption"), "zero_nominal"),
        (lambda u: u.__setitem__("adjustment_basis", "split_and_dividend_adjusted"), "unadjusted"),
    ],
)
def test_missing_kind_or_disclosure_fails_the_build(mutate: Any, needle: str) -> None:
    doc = _doc()
    mutate(doc["ingest"]["universe"])
    problems = validate_config(doc, env="beta")
    assert problems and any(needle in str(p) for p in problems), problems


def test_beta_and_gamma_run_yfinance_phase_2_decision_26() -> None:
    cfgs = load_all()
    for env in ("beta", "gamma"):
        assert (cfgs[env].phase, cfgs[env].provider) == (2, "yfinance")
    # prod follows once gamma's UNI-05 evidence is recorded (UNI-06); until then it is phase 1
    assert (cfgs["prod"].phase, cfgs["prod"].provider) in ((1, "fixture"), (2, "yfinance"))


# Data settings that must match across stages (decision 26). Names, retention, limits and
# consumer principals stay environment-specific; the provider follows the phase.
_PARITY_KEYS = ("validation", "daily_trigger", "metadata")


def _data_view(env: str) -> dict[str, Any]:
    doc = _doc(env)
    ingest = copy.deepcopy(doc["ingest"])
    ingest.pop("provider")
    return {"ingest": ingest, **{k: doc[k] for k in _PARITY_KEYS}}


@pytest.mark.parametrize("env", ["gamma", "prod"])
def test_data_parity_with_beta_decision_26(env: str) -> None:
    assert _data_view(env) == _data_view("beta")


# ------------------------------------------------------------------ UNI-06
def _evidence(**over: Any) -> dict[str, Any]:
    ev = {"input_snapshot_id": "snap_X", "dataset_id": UNIVERSE, "status": "approved", "approval_rule_version": "approval-v2-universe", "provider": "yfinance", "trigger": "scheduled"}
    ev.update(over)
    return ev


def test_promotion_gate_refuses_gamma_phase_2_without_beta_evidence() -> None:
    cfgs = load_all()
    g = _doc("gamma")
    g["phase"], g["ingest"]["provider"] = 2, "yfinance"
    cfgs = {**cfgs, "gamma": EnvConfig(g)}
    problems = phase2_promotion_problems(cfgs, {})
    gamma = [x for x in problems if x.pointer == "/gamma/phase"]
    assert len(gamma) == 1 and gamma[0].test_id == "UNI-06" and "missing beta evidence" in gamma[0].message
    assert phase2_promotion_problems(cfgs, {"beta": _evidence(provider="fixture")})
    assert phase2_promotion_problems(cfgs, {"beta": _evidence(status="committed")})
    # prod declares phase 2 too, so the gate also needs gamma's evidence
    assert phase2_promotion_problems(cfgs, {"beta": _evidence(), "gamma": _evidence()}) == []


def test_promotion_gate_prod_needs_gamma_evidence_and_repository_config_passes() -> None:
    cfgs = load_all()
    p = _doc("prod")
    p["phase"], p["ingest"]["provider"] = 2, "yfinance"
    problems = phase2_promotion_problems({**cfgs, "prod": EnvConfig(p)}, {"beta": _evidence()})
    assert problems and "gamma" in problems[0].message
    assert phase2_promotion_problems({**cfgs, "prod": EnvConfig(p)}, {"beta": _evidence(), "gamma": _evidence()}) == []
    from finplan_platform.core.config import load_phase2_evidence

    assert phase2_promotion_problems(cfgs, load_phase2_evidence()) == []


# ------------------------------------------------------------------ helpers
def _fixture_factory(clock: Any):
    return lambda ticker: FixtureProvider(dataset_id=UNIVERSE, instrument_id=ticker, clock=clock)


def _rows(start: date, end: date, *, split_on: str | None = None, adj_shift: float = 0.0) -> list[dict[str, Any]]:
    """Synthetic yfinance rows for every XNYS session in range (invented round numbers)."""
    out = []
    for i, sd in enumerate(xnys_calendar().sessions_between(start, end)):
        d = sd.day.isoformat()
        close = 100.0 + i
        split = 0.0
        if split_on and d >= split_on:
            close = close / 2.0
            split = 2.0 if d == split_on else 0.0
        out.append({"Date": d, "Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Adj Close": round(100.0 + i - adj_shift, 4), "Volume": 1000, "Dividends": 0.0, "Stock Splits": split, "Capital Gains": 0.0})
    return out


class UniverseYF(FakeYF):
    """FakeYF serving the same synthetic rows for every ticker, except tickers listed in ``missing`` (empty)."""

    def __init__(self, rows: list[dict[str, Any]], missing: tuple[str, ...] = ()) -> None:
        super().__init__(rows=rows)
        self.missing = missing

    def _history(self, ticker: str, kwargs: dict[str, Any]) -> Any:
        if ticker in self.missing:
            self.script = ["empty"]
        return super()._history(ticker, kwargs)


def _yf_factory(lib: FakeYF, clock: Any, cfg: EnvConfig):
    return lambda ticker: YFinanceProvider(dataset_id=UNIVERSE, ticker=ticker, settings=cfg.ingest["provider_settings"], clock=clock, sleep=lambda s: None, library=lib)


def _yf_deps(make_deps: Any, clock: Any, cfg: EnvConfig, lib: FakeYF, **kw: Any):
    from finplan_platform.providers.mock import MockProvider

    deps = make_deps(cfg=cfg, provider=MockProvider(clock=clock), calendar=xnys_calendar(), **kw)
    deps.universe_providers = _yf_factory(lib, clock, cfg)
    return deps


def _small_cfg() -> EnvConfig:
    return config_with(**{"ingest.universe.history_start": "2025-12-01"})


# ------------------------------------------------------------------ UNI-02
def test_full_history_fetch_from_history_start_for_every_ticker(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    cfg = _small_cfg()
    lib = UniverseYF(_rows(date(2025, 12, 1), date(2026, 1, 9)))
    deps = _yf_deps(make_deps, clock, cfg, lib)
    resp = run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    assert sorted(c["ticker"] for c in lib.history_calls) == sorted(TICKERS)
    assert {c["start"] for c in lib.history_calls} == {"2025-12-01"} and {c["end"] for c in lib.history_calls} == {"2026-01-10"}
    snap = resp["snapshot"]
    assert snap["dataset"]["dataset_id"] == UNIVERSE and snap["lineage"]["provider"] == "yfinance"
    assert snap["coverage"] == {"start": "2025-12-01", "end": "2026-01-09"}
    assert snap["status"] == "approved" and snap["approval_rule_version"] == "approval-v2-universe"


def test_effective_history_start_is_2010_10_01_on_xnys() -> None:
    assert effective_history_start(load_config("beta").universe, xnys_calendar()) == date(2010, 10, 1)


def test_split_continuity_keeps_unadjusted_ohlc_and_adj_close_basis(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    cfg = _small_cfg()
    lib = UniverseYF(_rows(date(2025, 12, 1), date(2026, 1, 9), split_on="2025-12-15"))
    deps = _yf_deps(make_deps, clock, cfg, lib)
    resp = run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    sid = resp["input_snapshot_id"]
    body, _ = deps.store.get("snapshots", key_snapshot_payload(sid, "observations.json"))
    obs = [o for o in json.loads(body)["observations"] if o["instrument_id"] == "NVDA"]
    by = {o["session_date"]: o for o in obs}
    assert by["2025-12-15"]["split_ratio"] == 2.0 and by["2025-12-12"]["split_ratio"] == 1.0
    assert by["2025-12-15"]["close"] < by["2025-12-12"]["close"]  # unadjusted price halves
    assert by["2025-12-15"]["adj_close"] - by["2025-12-12"]["adj_close"] == pytest.approx(1.0)  # return basis continuous
    assert resp["snapshot"]["status"] == "approved"


def test_rewritten_history_is_flagged_source_revised_and_previous_checksum_unchanged(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    cfg = _small_cfg()
    lib = UniverseYF(_rows(date(2025, 12, 1), date(2026, 1, 9)))
    deps = _yf_deps(make_deps, clock, cfg, lib)
    first = run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    ref1 = next(a for a in first["snapshot"]["artifacts"] if a["kind"] == "snapshot_payload")
    # next day: a dividend makes the provider rewrite every past adj_close
    at("2026-01-13T14:00:00Z")
    lib.rows = _rows(date(2025, 12, 1), date(2026, 1, 12), adj_shift=0.5)
    second = run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": "2026-01-13T14:00:00Z"}, deps=deps)
    snap = second["snapshot"]
    assert "source_revised" in snap["quality_flags"]
    revised = snap["quality_details"]["source_revised"]
    assert revised["previous_payload_checksum"] == ref1["checksum"]
    assert set(revised["instruments"]) == set(TICKERS) and revised["instruments"]["VOO"]["fields"] == ["adj_close"]
    assert snap["observation_summary"]["source_revised_count"] > 0
    # the previous snapshot is write-once: same bytes, same checksum
    body, _ = deps.store.get("snapshots", key_snapshot_payload(first["input_snapshot_id"], "observations.json"), expected_checksum=ref1["checksum"])
    assert json.loads(body)["observations"][0]["adj_close"] == 100.0
    assert snap["status"] == "approved"  # a revision is informational, not blocking


# ------------------------------------------------------------------ UNI-03
def test_one_missing_ticker_stays_committed_and_financemodel_cannot_read(make_deps: Any, sctx: Any, at: Any, clock: Any, ctx_factory: Any) -> None:
    at(SCHED)
    cfg = _small_cfg()
    lib = UniverseYF(_rows(date(2025, 12, 1), date(2026, 1, 9)), missing=("NFLX",))
    deps = _yf_deps(make_deps, clock, cfg, lib)
    resp = run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    snap = resp["snapshot"]
    assert snap["status"] == "committed" and "approval_rule_version" not in snap
    assert "partial_response" in snap["quality_flags"]
    assert any(p["instrument_id"] == "NFLX" for p in snap["quality_details"]["partial_response"])
    assert snap["observation_summary"]["instruments_complete"] == 4
    sid = resp["input_snapshot_id"]
    for key in (key_snapshot_manifest(sid), key_snapshot_payload(sid, "observations.json")):
        assert deps.store.get_tags("snapshots", key)[SNAPSHOT_STATUS_TAG] == "committed"  # the job-role bucket grant needs 'approved'
    fm = ctx_factory("arn:aws:iam::<account-id>:role/finplan-beta-financemodel-job-execution-role", role_class="financemodel-job")
    with pytest.raises(PlatformError) as ei:
        get_snapshot(fm, sid, deps=deps)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "snapshot_not_approved"


def test_every_ticker_failing_raises_without_a_snapshot(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    cfg = _small_cfg()
    lib = UniverseYF(_rows(date(2025, 12, 1), date(2026, 1, 9)))
    lib.script = ["error"] * 100
    deps = _yf_deps(make_deps, clock, cfg, lib)
    with pytest.raises(PlatformError) as ei:
        run_ingestion(sctx(), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    assert ei.value.code == "DEPENDENCY_UNAVAILABLE"


def test_duplicate_scheduled_delivery_returns_the_same_snapshot(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    deps = make_deps(cfg=load_config("gamma"))
    deps.universe_providers = _fixture_factory(clock)
    a = run_ingestion(sctx(env="gamma"), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    b = run_ingestion(sctx(env="gamma"), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    assert a["input_snapshot_id"] == b["input_snapshot_id"]


# ------------------------------------------------------------------ UNI-04 (+ 2.4 synthetic phase 1 fixtures)
def test_phase_1_fixture_universe_snapshot_is_contract_valid_with_disclosures(make_deps: Any, sctx: Any, at: Any, clock: Any) -> None:
    at(SCHED)
    cfg = load_config("gamma")
    deps = make_deps(cfg=cfg)
    deps.universe_providers = _fixture_factory(clock)
    resp = run_ingestion(sctx(env="gamma"), {"dataset_id": UNIVERSE, "scheduled_time": SCHED}, deps=deps)
    snap = resp["snapshot"]
    assert validate(snap, "input-snapshot").valid
    assert snap["synthetic"] is True and snap["lineage"]["provider"] == "fixture"
    assert [d["kind"] for d in snap["bias_disclosures"]] == ["hindsight_selection", "survivorship"]
    assert snap["status"] == "approved" and snap["approval_rule_version"] == "approval-v2-universe"
    body, _ = deps.store.get("snapshots", key_snapshot_payload(resp["input_snapshot_id"], "observations.json"))
    payload = json.loads(body)
    assert validate(payload, "snapshot-payload").valid
    assert payload["bias_disclosures"] == snap["bias_disclosures"]
    assert payload["universe"]["return_basis"] == "adj_close"
    assert not [o for o in payload["observations"] if o["instrument_id"] == "USD_CASH"]
    cash = next(i for i in payload["instruments"] if i["instrument_id"] == "USD_CASH")
    assert cash["kind"] == "cash" and cash["return_assumption"] == "zero_nominal"
    assert {o["instrument_id"] for o in payload["observations"]} == set(TICKERS)


def test_missing_disclosures_block_approval() -> None:
    from finplan_platform.core.snapshots import evaluate_approval

    cfg = load_config("gamma")
    doc = {"dataset": {"dataset_id": UNIVERSE}, "quality_flags": [], "observation_summary": {"instruments_expected": 5, "instruments_complete": 5}, "bias_disclosures": cfg.universe.disclosures()}
    assert evaluate_approval(doc, cfg)[0] is True
    bad = copy.deepcopy(doc)
    bad.pop("bias_disclosures")
    ok, blocking, version = evaluate_approval(bad, cfg)
    assert not ok and "missing_bias_disclosures" in blocking and version == "approval-v2-universe"


def test_etf_daily_snapshot_still_produced_alongside(make_deps: Any, sctx: Any, at: Any) -> None:
    at(SCHED)
    deps = make_deps(cfg=load_config("gamma"))
    resp = run_ingestion(sctx(env="gamma"), {"dataset_id": "finance/etf-daily/SPY", "scheduled_time": SCHED}, deps=deps)
    assert resp["snapshot"]["dataset"]["dataset_id"] == "finance/etf-daily/SPY" and resp["snapshot"]["approval_rule_version"] == "approval-v1"
    assert "bias_disclosures" not in resp["snapshot"]
