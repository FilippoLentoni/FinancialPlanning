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
