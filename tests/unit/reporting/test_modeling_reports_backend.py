"""Controlled replay through actual ModelingEvidence + Dynamo/S3 repository adapters."""
import base64
import json

import pytest

from finplan_platform.core.artifacts import sha256_checksum
from finplan_platform.core.modeling_evidence import ModelingEvidence, _id
from finplan_platform.reporting.reports import aggregate, write_report

DAY = "2026-01-12"
OPEN = DAY + "T14:00:00+00:00"
SLOTS = [DAY + "T15:00:00+00:00", DAY + "T16:00:00+00:00"]


def test_real_backend_daily_report_roundtrip_exact_input_fills_and_book_values(repo_any, artifacts, tmp_path):
    e = ModelingEvidence(repo_any, artifacts)
    approaches = []
    for aid in ["cash", "rule"]:
        book = e.ensure_portfolio("report-replay", aid, 1000, ["SPY"], OPEN)
        approaches.append({"approach_id": aid, "display_name": aid, "family": "control", "portfolio_id": book["portfolio_id"],
                           "cohort_id": "replay", "comparison_policy_id": "common-test", "starting_capital": 1000,
                           "investable_universe": ["SPY"], "native_cadence": "hourly"})
        e.record_observation("report-replay", aid, OPEN, book["portfolio_id"], {"value": 1000, "cash": 1000, "holdings": [], "quote_status": "fresh", "status": "valid"}, OPEN)
        for i, at in enumerate(SLOTS):
            api_at = at.replace("+00:00", "Z")
            book = e.get_portfolio(book["portfolio_id"])
            run_id = _id("mr", "report-replay", aid, at)
            input_id = _id("mi", "report-replay", aid, at, "model_input")
            actual = {"schema_version": "finplan-model-input/1", "run_id": run_id, "input_id": input_id, "approach_id": aid,
                      "decision_time": at, "availability_cutoff": at, "holdings": book, "input_snapshot_id": "snap_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                      "news": {"consumed": False, "layer": "not_consumed", "items": []},
                      "numeric": {"frequency": "controlled_replay", "prepared": {"price": 100+i*10, "mean": [0.01], "covariance": [[0.0004]], "feature_order": ["SPY"]}}}
            supplied = e.capture_input("report-replay", aid, at, actual, None, at)
            run = e.start_run("report-replay", aid, at, supplied["input_id"], book["portfolio_id"], api_at, {"family": "control"})
            output = {"status": "succeeded", "cash_weight": .5 if aid == "rule" else 1, "target_weights": {"SPY": .5 if aid == "rule" else 0},
                      "model_cost": 0, "model_cost_basis": "observed", "forecast": None}
            e.finish_run(run["run_id"], output, api_at)
            if aid == "rule":
                cash, quantity = (499.5, 5) if i == 0 else (719.28, 3)
                fills = [{"symbol": "SPY", "quantity": 5 if i == 0 else 2, "price": 100+i*10, "cost": .5 if i == 0 else .22, "side": "buy" if i == 0 else "sell"}]
                state = {**book["paper_state"], "cash_balance": cash, "positions": [{"instrument_id": "SPY", "quantity": quantity}], "source": "paper_rebalance"}
                fee = .5 if i == 0 else .22
                before, after = (1000, 999.5) if i == 0 else (1049.5, 1049.28)
            else:
                state, fills, fee, before, after = book["paper_state"], [], 0, 1000, 1000
            execution = {"market_session_valid": True, "evaluator_version": "controlled-replay/1", "quote_provenance": {"source": "controlled-test", "price": 100+i*10},
                         "execution_at": at, "transaction_cost": fee, "before_value": before, "after_value": after, "reference_prices": {"SPY": 100+i*10},
                         "transaction_cost_bps": 10, "reference_date": DAY, "turnover": (500 if i == 0 else 220)/1000 if aid == "rule" else 0}
            e.commit_paper_step(run["run_id"], book["revision"], state, fills, execution, api_at)
            e.record_observation("report-replay", aid, at, book["portfolio_id"], {"value": after, "cash": state["cash_balance"], "holdings": state.get("positions", []), "status": "valid", "quote_status": "fresh"}, at)
    e.register_approaches("report-replay", approaches, {"starting_capital": 1000}, OPEN)
    result = aggregate(e, experiment_id="report-replay", registry=e.read_registry("report-replay")["approaches"], start_day=DAY,
                       generated_at=SLOTS[-1], mode="controlled_replay")
    assert result["evidence_failures"] == []
    assert len(result["runs"]) == 4 and len(result["inputs"]) == 4
    rule = next(b for b in result["leaderboard"] if b["approach_id"] == "rule")
    assert rule["current_value"] == pytest.approx(1049.28)
    assert rule["net_pnl"] == pytest.approx(49.28)
    assert rule["execution_cost"] == pytest.approx(.72)
    assert rule["fills"] == 2 and rule["rank"] == 1
    for rid, captured in result["inputs"].items():
        raw = base64.b64decode(captured["raw_base64"])
        assert raw == e.read_input_bytes(captured["metadata"]["input_id"])
        assert sha256_checksum(raw) == captured["checksum"]
        assert json.loads(raw)["run_id"] == rid
    written = write_report(result, tmp_path, artifact_store=artifacts)
    assert written["manifest"]["source_record_ids"]["runs"] == result["source_record_ids"]["runs"]
    assert (tmp_path / "replay" / "latest.html").exists()


def test_friday_recommendation_monday_execution_cost_is_counted_only_on_execution_day(repo_any, artifacts):
    e = ModelingEvidence(repo_any, artifacts)
    friday, monday = "2026-01-09T19:00:00Z", "2026-01-12T15:00:00Z"
    book = e.ensure_portfolio("cross-day-report", "rule", 1000, ["SPY"], friday)
    registry = [{"approach_id": "rule", "display_name": "rule", "family": "control", "portfolio_id": book["portfolio_id"],
                 "cohort_id": "cross-day", "comparison_policy_id": "controlled", "starting_capital": 1000, "investable_universe": ["SPY"]}]
    iid = _id("mi", "cross-day-report", "rule", friday, "model_input")
    rid = _id("mr", "cross-day-report", "rule", friday)
    supplied = e.capture_input("cross-day-report", "rule", friday,
        {"run_id": rid, "input_id": iid, "approach_id": "rule", "decision_time": friday, "availability_cutoff": friday,
         "input_snapshot_id": "snap_01ARZ3NDEKTSV4RRFFQ69G5FAV", "news": {"consumed": False}, "numeric": {"prepared": {"price": 100}}}, None, friday)
    e.start_run("cross-day-report", "rule", friday, supplied["input_id"], book["portfolio_id"], friday, {"family": "control"})
    e.finish_run(rid, {"status": "succeeded", "target_weights": {"SPY": .5}, "cash_weight": .5, "model_cost": 0, "model_cost_basis": "observed"}, friday)
    e.record_observation("cross-day-report", "rule", friday, book["portfolio_id"], {"value": 1000, "status": "valid", "quote_status": "fresh"}, friday)
    e.record_observation("cross-day-report", "rule", "2026-01-12T14:00:00Z", book["portfolio_id"], {"value": 1000, "status": "valid", "quote_status": "fresh"}, "2026-01-12T14:00:00Z")
    state = {**book["paper_state"], "positions": [{"instrument_id": "SPY", "quantity": 5}], "cash_balance": 499.5, "source": "paper_rebalance", "as_of": "2026-01-12"}
    execution = {"execution_at": monday, "market_session_valid": True, "evaluator_version": "controlled-replay", "quote_provenance": {"provider": "controlled-test"},
                 "reference_prices": {"SPY": 100}, "reference_date": "2026-01-12", "transaction_cost_bps": 10, "transaction_cost": .5,
                 "before_value": 1000, "after_value": 999.5, "turnover": .5}
    fills = [{"symbol": "SPY", "quantity": 5, "side": "buy", "price": 100, "cost": .5}]
    committed = e.commit_paper_step(rid, 1, state, fills, execution, monday)
    assert e.commit_paper_step(rid, 1, state, fills, execution, monday) == committed
    e.record_observation("cross-day-report", "rule", monday, book["portfolio_id"], {"value": 999.5, "status": "valid", "quote_status": "fresh"}, monday)
    def build(start, end=None):
        return aggregate(e, experiment_id="cross-day-report", registry=registry, start_day=start, end_day=end,
                         generated_at="2026-01-12T16:00:00Z", mode="controlled_replay")
    friday_report, monday_report = build("2026-01-09"), build("2026-01-12")
    assert len(friday_report["runs"]) == 1 and len(monday_report["runs"]) == 0
    assert friday_report["leaderboard"][0]["fills"] == 0 and friday_report["leaderboard"][0]["execution_cost"] == 0
    assert friday_report["runs"][0]["execution_cost"] == .5  # Eventual recommendation lifecycle evidence.
    assert friday_report["data_cutoff"] == monday  # Late outcome is an explicit revised cutoff.
    assert monday_report["leaderboard"][0]["fills"] == 1 and monday_report["leaderboard"][0]["execution_cost"] == .5
    assert monday_report["leaderboard"][0]["turnover"] == .5 and monday_report["leaderboard"][0]["net_pnl"] == -.5
    assert len(monday_report["execution_events"]) == 1
    combined_window = build("2026-01-09", "2026-01-12")
    assert len(combined_window["runs"]) == 1 and len(combined_window["execution_events"]) == 1
    assert combined_window["leaderboard"][0]["fills"] == 1 and combined_window["leaderboard"][0]["execution_cost"] == .5


def test_interim_recommendations_with_no_registry_are_included_but_cannot_trade_or_rank(repo_any, artifacts):
    from finplan_platform.reporting.reports import aggregate_interim, consolidate
    e = ModelingEvidence(repo_any, artifacts)
    experiment = "interim-report-test"
    primary = e.ensure_portfolio(experiment, "direct_codex_news", 1000, ["SPY"], OPEN)
    interim = e.ensure_portfolio(experiment+"-interim", "direct_codex_news", 1000, ["SPY"], OPEN)
    approaches = [{"approach_id": "direct_codex_news", "display_name": "Direct Codex", "family": "language_model", "portfolio_id": primary["portfolio_id"],
                   "comparison_policy_id": "controlled", "cohort_id": "primary", "starting_capital": 1000, "investable_universe": ["SPY"]}]
    primary_registry = {"approaches": approaches, "comparison_policy": {}}
    for at in ["2026-01-12T14:30:00Z", "2026-01-12T15:00:00Z"]:
        iid = _id("mi", experiment+"-interim", "direct_codex_news", at, "model_input")
        rid = _id("mr", experiment+"-interim", "direct_codex_news", at)
        payload = {"approach_id": "direct_codex_news", "run_id": rid, "input_id": iid, "decision_time": at, "availability_cutoff": at,
                   "holdings": {"portfolio_id": primary["portfolio_id"], "cash": 1000, "revision": 1},
                   "numeric": {"prepared": {"actual_source": {"market_descriptors": {"trend_bucket": "flat"}}}},
                   "news": {"consumed": True, "layer": "selected_headlines", "items": [{"title": "Controlled permitted headline", "available_at": at}]}}
        captured = e.capture_input(experiment+"-interim", "direct_codex_news", at, payload, None, at)
        e.start_run(experiment+"-interim", "direct_codex_news", at, captured["input_id"], interim["portfolio_id"], at, {"family": "language_model"})
        e.finish_run(rid, {"status": "succeeded", "target_weights": {"SPY": .5}, "cash_weight": .5, "purpose": "interim_research_no_primary_execution", "execution_status": "not_applied_interim"}, at)
    assert e.read_registry(experiment+"-interim") is None
    interim_report = aggregate_interim(e, primary_experiment=experiment, primary_registry=primary_registry, start_day=DAY, end_day=DAY,
                                     generated_at=SLOTS[-1], mode="controlled_replay")
    assert len(interim_report["runs"]) == 2 and len(interim_report["inputs"]) == 2
    assert interim_report["leaderboard"] == [] and interim_report["execution_events"] == []
    assert all(r["non_trading"] and r["reviewed_portfolio_id"] == primary["portfolio_id"] for r in interim_report["runs"])
    assert e.get_portfolio(primary["portfolio_id"])["revision"] == 1 and e.get_portfolio(interim["portfolio_id"])["revision"] == 1
    primary_report = aggregate(e, experiment_id=experiment, registry=approaches, start_day=DAY, generated_at=SLOTS[-1], mode="controlled_replay")
    combined = consolidate([primary_report, interim_report])
    assert len(combined["runs"]) == 2 and len(combined["leaderboard"]) == 1
    assert all("-interim::" in r["approach_id"] for r in combined["runs"])
    assert e.read_registry(experiment+"-interim") is None  # read model never writes a fictional registry.


def test_session_proof_from_shared_quotes_never_becomes_cash_model_input(repo_any, artifacts):
    e = ModelingEvidence(repo_any, artifacts)
    at = "2026-10-17T14:00:00Z"  # Saturday; explicit stored provider closure is the proof.
    book = e.ensure_portfolio("session-report-test", "cash", 1000, ["SPY"], at)
    shared = e.capture_shared_source("session-report-test", at, {"current_quotes": {"quotes": [{"instrument_id": "SPY", "price": 999,
        "provider": "controlled-test", "market_open": False, "quoted_at": "2026-10-16T19:59:00Z", "available_at": at,
        "regular_session": {"start": "2026-10-16T13:30:00Z", "end": "2026-10-16T20:00:00Z"}}]}}, at)
    iid = _id("mi", "session-report-test", "cash", at, "model_input")
    rid = _id("mr", "session-report-test", "cash", at)
    payload = {"input_id": iid, "run_id": rid, "approach_id": "cash", "decision_time": at, "availability_cutoff": at,
               "news": {"consumed": False}, "numeric": {"prepared": {"rule": "hold cash"}}, "holdings": {"cash": 1000}}
    captured = e.capture_input("session-report-test", "cash", at, payload, shared, at)
    e.start_run("session-report-test", "cash", at, captured["input_id"], book["portfolio_id"], at, {"family": "control"})
    e.finish_run(rid, {"status": "hold"}, at)
    e.record_observation("session-report-test", "cash", at, book["portfolio_id"], {"run_id": rid, "value": 1000, "quote_status": "fresh", "quote_provenance": {"basis": "authoritative_cash"}}, at)
    registry = [{"approach_id": "cash", "portfolio_id": book["portfolio_id"], "family": "control", "cohort_id": "test", "comparison_policy_id": "test", "starting_capital": 1000}]
    report = aggregate(e, experiment_id="session-report-test", registry=registry, start_day="2026-10-17", generated_at=at, mode="controlled_replay")
    assert report["leaderboard"][0]["observed_dates"] == 1 and report["leaderboard"][0]["observed_sessions"] == 0
    assert report["inputs"][rid]["payload"] == payload
    assert "price" not in json.dumps(report["inputs"][rid]["payload"])
    assert report["runs"][0]["market_session_evidence"]["role"].startswith("valuation/session context only")
    assert "price" not in report["runs"][0]["market_session_evidence"]["quotes"][0]
