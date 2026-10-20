"""Competition insights: attribution, head-to-head explanations and recommendation changes."""
import copy

import pytest

from finplan_platform.reporting.insights import build_insights
from finplan_platform.reporting.reports import _valid, _value, aggregate
from finplan_platform.reporting.transport import decode_transport, encode_transport

from test_modeling_reports import DAY, Reader, registry

S0, S1, S2 = "2026-10-19T13:45:00Z", "2026-10-19T19:45:00Z", "2026-10-20T13:45:00Z"
APPROACHES = [
    {"approach_id": "cash", "display_name": "Cash", "family": "control", "adapter": "baseline", "starting_capital": 1000.0},
    {"approach_id": "classical_min_variance", "display_name": "Min variance", "family": "optimization", "adapter": "classical", "starting_capital": 1000.0},
    {"approach_id": "agent_min_variance", "display_name": "Agent min variance", "family": "agentic", "adapter": "agent_tool",
     "strategy_name": "min_variance", "starting_capital": 1000.0},
    {"approach_id": "ppo", "display_name": "PPO", "family": "reinforcement_learning", "adapter": "actor", "starting_capital": 1000.0},
    {"approach_id": "direct_claude_news", "display_name": "Claude", "family": "language_model", "adapter": "claude_offline", "starting_capital": 1000.0},
]


def _valuation(aid, at, value, quantities, prices, cash):
    return {"approach_id": aid, "observation_id": f"obs_{aid}_{at}", "observed_at": at, "kind": "valuation", "value": value, "cash": cash,
            "holdings": [{"instrument_id": i, "quantity": q} for i, q in quantities.items()],
            "quote_provenance": {i: {"price": p} for i, p in prices.items()}, "observation_kind": "valid"}


def _run(aid, at, weights, cash, *, status="applied", price_as_of="2026-10-16", **extra):
    return {"approach_id": aid, "run_id": f"mr_{aid}_{at}", "decision_time": at, "status": status, "price_as_of": price_as_of,
            "target_weights": [{"instrument_id": i, "weight": w} for i, w in weights.items()], "cash_weight": cash, **extra}


def _classical_input(volatility, weights):
    return {"holdings": {"weights": [{"instrument_id": i, "weight": w} for i, w in weights.items()]},
            "numeric": {"prepared": {"algorithm": "min_variance", "instruments": ["AAA", "BBB"], "expected_returns": [0.001, 0.002],
                                     "covariance": [[volatility[0] ** 2, 0.0], [0.0, volatility[1] ** 2]]}}}


def _fixture():
    p0, p1, p2 = {"AAA": 10.0, "BBB": 20.0}, {"AAA": 11.0, "BBB": 19.0}, {"AAA": 12.0, "BBB": 18.0}
    observations = [
        _valuation("cash", S0, 1000.0, {}, p0, 1000.0), _valuation("cash", S1, 1000.0, {}, p1, 1000.0), _valuation("cash", S2, 1000.0, {}, p2, 1000.0),
        # 50 AAA + 25 BBB bought at S0 for 999 (1 of costs); S1: AAA +50, BBB -25; S2: AAA +50, BBB -25, 2 more costs.
        _valuation("classical_min_variance", S0, 999.0, {"AAA": 50, "BBB": 25}, p0, -1.0),
        _valuation("classical_min_variance", S1, 1024.0, {"AAA": 50, "BBB": 25}, p1, -1.0),
        _valuation("classical_min_variance", S2, 1047.0, {"AAA": 50, "BBB": 25}, p2, -3.0),
        _valuation("agent_min_variance", S1, 1000.0, {}, p1, 1000.0),
        _valuation("agent_min_variance", S2, 990.0, {"BBB": 50}, p2, 90.0),
    ]
    runs = [
        _run("classical_min_variance", S0, {"AAA": .5, "BBB": .5}, 0.0),
        _run("classical_min_variance", S1, {"AAA": .5, "BBB": .5}, 0.0),
        _run("classical_min_variance", S2, {"AAA": .6, "BBB": .4}, 0.0, price_as_of="2026-10-19"),
        _run("agent_min_variance", S0, {}, 1.0, status="failed", outcome={"reason": "agent_allocated_outside_the_experiment_universe"}),
        _run("agent_min_variance", S1, {"BBB": 1.0}, 0.0),
        _run("agent_min_variance", S2, {"AAA": .6, "BBB": .4}, 0.0, price_as_of="2026-10-19"),
        _run("ppo", S0, {"AAA": .1}, .9),
        _run("ppo", S1, {"AAA": .2}, .8),
        _run("direct_claude_news", S0, {"AAA": .3}, .7),
        _run("direct_claude_news", S1, {"AAA": .2}, .8, rationale="Trim AAA after the rally."),
    ]
    inputs = {
        "mr_classical_min_variance_" + S1: {"payload": _classical_input((0.02, 0.03), {"AAA": .5, "BBB": .5})},
        "mr_classical_min_variance_" + S2: {"payload": _classical_input((0.02, 0.05), {"AAA": .53, "BBB": .48})},
        "mr_ppo_" + S0: {"payload": {"numeric": {"prepared": {"observation_names": ["mean.AAA", "current_weight.cash"], "observation": [0.1, 1.0]}}}},
        "mr_ppo_" + S1: {"payload": {"numeric": {"prepared": {"observation_names": ["mean.AAA", "current_weight.cash"], "observation": [0.1, 0.9],
                                                              "environment": {"features": ["market_summary", "current_weights"], "rebalance_fraction": 0.25}}}}},
        "mr_direct_claude_news_" + S0: {"payload": {"news": {"consumed": True, "items": [{"headline": "old"}]}}},
        "mr_direct_claude_news_" + S1: {"payload": {"news": {"consumed": True, "items": [{"headline": "old"}, {"headline": "AAA rallies"}]}}},
    }
    return observations, runs, inputs


def _build():
    observations, runs, inputs = _fixture()
    return build_insights(registry=APPROACHES, runs=runs, observations=observations, inputs=inputs, value_of=_value, valid=_valid, window={})


def test_attribution_splits_value_change_into_instruments_and_costs():
    books = {b["approach_id"]: b for b in _build()["books"]}
    mv = books["classical_min_variance"]
    # 50 x (+1 +1) = +100 on AAA; 25 x (-1 -1) = -50 on BBB; the remaining -3 of 47 are costs.
    assert mv["contributions"] == {"AAA": 100.0, "BBB": -50.0}
    assert mv["costs_and_timing"] == -3.0 and mv["pnl"] == 47.0 and mv["return"] == pytest.approx(0.047)
    assert mv["pnl"] == pytest.approx(sum(mv["contributions"].values()) + mv["costs_and_timing"])
    assert mv["best_instrument"] == "AAA" and mv["worst_instrument"] == "BBB" and mv["average_invested_fraction"] == pytest.approx(1.0, abs=0.01)
    assert "best AAA +$100" in mv["summary"] and "worst BBB -$50" in mv["summary"]
    assert books["cash"]["pnl"] == 0.0 and books["cash"]["contributions"] == {}
    # Agent book started late (all cash until S1), then lost 50 x -1 on BBB; 10 - ... costs.
    agent = books["agent_min_variance"]
    assert agent["contributions"] == {} and agent["costs_and_timing"] == -10.0 and agent["pnl"] == -10.0
    assert [b["approach_id"] for b in _build()["books"]][:3] == ["classical_min_variance", "cash", "agent_min_variance"]


def test_head_to_head_explains_gap_by_instrument_and_pairs_agents_with_their_direct_book():
    insights = _build()
    kinds = [(c["kind"], c["a"], c["b"]) for c in insights["comparisons"]]
    assert ("next_ranked", "classical_min_variance", "cash") in kinds
    assert ("agent_vs_direct", "agent_min_variance", "classical_min_variance") in kinds
    # The agent/direct pair is listed once, as agent_vs_direct, never also as next-ranked.
    assert sum({a, b} == {"agent_min_variance", "classical_min_variance"} for _, a, b in kinds) == 1
    lead = next(c for c in insights["comparisons"] if c["a"] == "classical_min_variance" and c["b"] == "cash")
    assert lead["pnl_gap"] == 47.0 and lead["instruments"][0] == {"instrument_id": "AAA", "a": 100.0, "b": 0.0, "difference": 100.0}
    assert lead["summary"].startswith("Min variance is $47 ahead of Cash.") and "AAA (Min variance +$100 vs Cash +$0)" in lead["summary"]
    assert {m["instrument_id"]: m["change"] for m in insights["market"]} == {"AAA": 0.2, "BBB": -0.1}
    assert insights["headline"].startswith("Min variance leads with +$47")


def test_recommendation_changes_name_the_inputs_that_moved():
    evolution = _build()["evolution"]
    mv = evolution["classical_min_variance"]
    assert mv[0]["reasons"] == ["First recommendation in this history."] and mv[0]["drivers"] == []
    assert mv[1]["turnover"] == 0.0 and any("Same daily price snapshot" in r for r in mv[1]["reasons"])
    assert "The recommendation is unchanged." in mv[1]["reasons"]
    assert mv[2]["change"] == {"AAA": 0.1, "BBB": -0.1} and mv[2]["turnover"] == 0.1
    assert any("New daily price snapshot: 2026-10-16 → 2026-10-19" in r for r in mv[2]["reasons"])
    assert any("Holdings drifted" in r and "AAA 50.0% → 53.0%" in r for r in mv[2]["reasons"])
    # Min-variance ignores expected returns: only the volatility that moved is a driver.
    assert mv[2]["drivers"] == [{"input": "daily volatility BBB", "before": 0.03, "after": 0.05, "change": 0.02}]
    ppo = evolution["ppo"][1]
    assert ppo["drivers"] == [{"input": "current_weight.cash", "before": 1.0, "after": 0.9, "change": -0.1}]
    assert any("moves only 25% of the way to its target" in r for r in ppo["reasons"])
    claude = evolution["direct_claude_news"][1]
    assert claude["drivers"] == [{"input": "new headlines", "count": 1, "examples": ["AAA rallies"]}]
    assert claude["rationale"] == "Trim AAA after the rally." and claude["change"] == {"AAA": -0.1}
    agent = evolution["agent_min_variance"]
    assert agent[0]["note"] == "agent_allocated_outside_the_experiment_universe" and "target" not in agent[0]
    assert any("classical_min_variance book's inputs" in r for r in agent[2]["reasons"])


def test_aggregate_reads_history_before_the_window_and_the_report_transport_keeps_insights():
    reader = Reader()
    previous = "2026-10-11T15:00:00+00:00"
    history_run = copy.deepcopy(reader.runs[2])
    history_run.update(run_id="run_rule_prev", input_id="input_rule_0", scheduled_slot=previous, decision_time=previous, availability_cutoff=previous)
    reader.runs.append(history_run)
    reader.observations.append({**copy.deepcopy(reader.observations[3]), "observation_id": "obs_rule_prev", "observed_at": previous, "value": 99})
    report = aggregate(reader, experiment_id="test", registry=registry(), start_day=DAY, generated_at=DAY + "T16:00:00+00:00",
                       mode="controlled_replay", comparison_policy={"first_scheduled_slot": previous})
    insights = report["insights"]
    assert insights["window"]["start_day"] == "2026-10-11" and insights["window"]["report_start_day"] == DAY
    assert ("test", "2026-10-11", "runs", None) in reader.calls
    rule = next(b for b in insights["books"] if b["approach_id"] == "rule")
    assert rule["valuations"] == 4 and rule["end_value"] == 102
    assert [r["run_id"] for r in insights["evolution"]["rule"]][0] == "run_rule_prev"
    # The history run is not a report row; only its insight is added.
    assert "run_rule_prev" not in report["source_record_ids"]["runs"]
    assert decode_transport(encode_transport(report))["insights"] == insights


def test_insight_failure_never_blocks_the_report():
    class Broken(Reader):
        def query_day(self, experiment_id, day, *, kind, page_size=100, next_token=None):
            if day < DAY:
                raise ValueError("history unavailable")
            return super().query_day(experiment_id, day, kind=kind, page_size=page_size, next_token=next_token)
    report = aggregate(Broken(), experiment_id="test", registry=registry(), start_day=DAY, generated_at=DAY + "T16:00:00+00:00",
                       mode="controlled_replay", comparison_policy={"first_scheduled_slot": "2026-10-10T15:00:00+00:00"})
    assert report["insights"] == {"version": "modeling-insights/1", "status": "unavailable", "reason": "ValueError: history unavailable"}
    assert report["leaderboard"]


def test_large_captured_inputs_still_yield_solver_drivers():
    # Inputs over 2 KiB are kept as compressed original bytes plus a summary payload; the
    # insights read the solver inputs from the full payload while it is loaded.
    from finplan_platform.reporting.reports import canonical, checksum
    reader = Reader()
    for i, run in enumerate(r for r in reader.runs if r["approach_id"] == "rule"):
        capture = reader.inputs[run["input_id"]]
        payload = capture["payload"]
        payload["numeric"]["prepared"] = {"algorithm": "min_variance", "instruments": ["TEST", "OTHER"],
                                          "covariance": [[(0.02 + 0.01 * i) ** 2, 0.0], [0.0, 0.0009]],
                                          "scenarios": [[0.001 * k, -0.001 * k] for k in range(400)]}
        capture["raw_bytes"] = canonical(payload)
        capture["metadata"]["checksum"] = checksum(capture["raw_bytes"])
        run.update(status="applied", target_weights=[{"instrument_id": "TEST", "weight": 0.5 + 0.1 * i}], cash_weight=0.5 - 0.1 * i)
    report = aggregate(reader, experiment_id="test", registry=registry(), start_day=DAY, generated_at=DAY + "T16:00:00+00:00", mode="controlled_replay")
    assert all(not report["inputs"][r["run_id"]].get("preview_is_complete", True) for r in reader.runs if r["approach_id"] == "rule")
    latest = report["insights"]["evolution"]["rule"][-1]
    assert latest["drivers"] == [{"input": "daily volatility TEST", "before": 0.02, "after": 0.03, "change": 0.01}]
