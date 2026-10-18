"""Known-outcome replay tests; fixtures are never forward performance evidence."""
import base64
import copy
import json
import re
from pathlib import Path

import pytest

from finplan_platform.reporting.reports import (
    EvidenceError, aggregate, canonical, checksum, csv_export, forecast_metrics,
    html_export, summarize, write_report,
)

DAY = "2026-10-12"
T0 = DAY + "T14:00:00+00:00"
T1 = DAY + "T15:00:00+00:00"
T2 = DAY + "T16:00:00+00:00"


def embedded_implementation(html):
    # The trusted self-contained implementation is installed after bounded gzip
    # bootstrap; this helper is only for existing source-smoke assertions.
    return base64.b64decode(re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", html).group(1)).decode()


def registry():
    return [{"approach_id": a, "display_name": a, "family": "control", "portfolio_id": "book_"+a,
             "cohort_id": "cohort", "comparison_policy_id": "common/1", "starting_capital": 100,
             "investable_universe": ["TEST"], "native_cadence": "hourly"} for a in ["cash", "rule"]]


class Reader:
    def __init__(self):
        self.runs, self.observations, self.inputs = [], [], {}
        self.calls = []
        for aid in ["cash", "rule"]:
            for i, at in enumerate([T0, T1]):
                rid, iid = f"run_{aid}_{i}", f"input_{aid}_{i}"
                payload = {"approach_id": aid, "run_id": rid, "input_id": iid,
                           "decision_time": at, "availability_cutoff": at,
                           "holdings": {"cash": 100-i, "revision": i+1},
                           "numeric": {"prepared": {"observation": [i, 7.25], "feature_order": ["hour", "test_feature"]}},
                           "news": {"consumed": False, "layer": "not_consumed", "items": []}}
                raw = canonical(payload)
                self.inputs[iid] = {"metadata": {"input_id": iid, "approach_id": aid, "run_id": rid,
                                               "checksum": checksum(raw), "captured_at": at, "kind": "model_input"},
                                    "payload": payload, "raw_bytes": raw}
                self.runs.append({"experiment_id": "test", "approach_id": aid, "portfolio_id": "book_"+aid,
                                  "run_id": rid, "input_id": iid, "scheduled_slot": at, "decision_time": at,
                                  "availability_cutoff": at, "status": "hold", "model_cost": 0,
                                  "model_cost_basis": "observed", "execution_cost": 0, "fills": []})
            for i, at in enumerate([T0, T1, T2]):
                self.observations.append({"experiment_id": "test", "approach_id": aid, "portfolio_id": "book_"+aid,
                                          "observation_id": f"obs_{aid}_{i}", "observed_at": at,
                                          "value": 100 + (i if aid == "rule" else 0), "status": "valid",
                                          "quote_status": "fresh", "cash": 100})

    def query_day(self, experiment_id, day, *, kind, page_size=100, next_token=None):
        self.calls.append((experiment_id, day, kind, next_token))
        values = self.runs if kind == "runs" else self.observations
        values = [v for v in values if v.get("decision_time", v.get("slot", v.get("observed_at")))[:10] == day]
        offset = int(next_token or 0)
        # Deliberately exercise pagination smaller than requested page size.
        return copy.deepcopy(values[offset:offset+2]), str(offset+2) if len(values)>offset+2 else None

    def read_input(self, input_id):
        return copy.deepcopy(self.inputs[input_id])


def report(reader=None, **kwargs):
    return aggregate(reader or Reader(), experiment_id="test", registry=registry(), start_day=DAY,
                     generated_at=T2, mode="controlled_replay", **kwargs)


def test_exact_per_model_per_hour_bundles_do_not_change_on_later_source_update():
    reader = Reader()
    result = report(reader)
    assert len(result["runs"]) == 4
    assert len(result["inputs"]) == 4
    assert result["evidence_failures"] == []
    assert result["inputs"]["run_rule_0"]["payload"]["numeric"]["prepared"]["observation"] == [0, 7.25]
    assert result["inputs"]["run_rule_1"]["payload"]["numeric"]["prepared"]["observation"] == [1, 7.25]
    for rid, captured in result["inputs"].items():
        raw = base64.b64decode(captured["raw_base64"])
        assert checksum(raw) == captured["checksum"]
        assert json.loads(raw)["run_id"] == rid
    reader.inputs["input_rule_0"]["payload"]["numeric"]["prepared"]["observation"] = [999]
    assert result["inputs"]["run_rule_0"]["payload"]["numeric"]["prepared"]["observation"] == [0, 7.25]
    assert any(call[-1] for call in reader.calls)


@pytest.mark.parametrize("mutation,expected", [
    (lambda x: x["metadata"].update(approach_id="cash"), "approach_id"),
    (lambda x: x["metadata"].update(kind="shared_source"), "Shared source"),
    (lambda x: x["payload"].update(run_id="run_cash_0"), "differs"),
    (lambda x: x["metadata"].update(checksum="sha256:"+"0"*64), "checksum"),
])
def test_catches_shared_input_substitution_and_mismatched_run(mutation, expected):
    reader = Reader()
    mutation(reader.inputs["input_rule_0"])
    result = report(reader)
    assert "run_rule_0" not in result["inputs"]
    assert expected.lower() in result["evidence_failures"][0]["error"].lower()
    assert next(b for b in result["leaderboard"] if b["approach_id"] == "rule")["rank"] is None


def test_catches_future_available_evidence_and_falsely_attributed_news():
    reader = Reader()
    item = reader.inputs["input_rule_0"]
    item["payload"]["news"] = {"consumed": True, "items": [{"title": "future", "available_at": T1}]}
    item["raw_bytes"] = canonical(item["payload"]); item["metadata"]["checksum"] = checksum(item["raw_bytes"])
    assert "Future-available" in report(reader)["evidence_failures"][0]["error"]
    item["payload"]["news"] = {"consumed": False, "prepared_context": "This was supplied"}
    item["raw_bytes"] = canonical(item["payload"]); item["metadata"]["checksum"] = checksum(item["raw_bytes"])
    assert "falsely labeled" in report(reader)["evidence_failures"][0]["error"]


def test_duplicate_join_does_not_multiply_recommendations_costs_or_fills():
    reader = Reader()
    reader.runs.append(copy.deepcopy(reader.runs[0]))
    reader.observations.append(copy.deepcopy(reader.observations[0]))
    result = report(reader)
    assert len(result["runs"]) == 4 and len(result["observations"]) == 6
    assert sum(b["recommendations"] for b in result["leaderboard"]) == 4
    reader.runs[-1]["run_id"] = "conflicting_new_run"
    with pytest.raises(EvidenceError, match="permanent approach/slot"):
        report(reader)


def test_known_outcome_daily_and_seven_day_rank_and_forecast_alignment(tmp_path):
    reader = Reader()
    reader.runs[2]["forecast"] = {"forecast_id": "forecast", "target_at": DAY+"T14:30:00+00:00",
                                  "expected_value": 100.5, "baseline_value": 100, "units": "USD", "forecast_kind": "model_forecast"}
    daily = report(reader)
    assert daily["forecasts"][0]["realized_value"] == 101
    assert daily["forecasts"][0]["signed_error"] == .5
    assert daily["forecasts"][0]["matched_observation_id"] == "obs_rule_1"
    assert daily["leaderboard"][0]["approach_id"] == "rule"
    assert daily["leaderboard"][0]["net_return"] == pytest.approx(.02)
    assert daily["leaderboard"][0]["mae"] == .5
    weekly = report(reader, end_day="2026-10-18", finalized=False)
    assert weekly["period"] == "weekly" and not weekly["window_complete"]
    written = write_report(weekly, tmp_path)
    assert "/replay/weekly/" in written["archive"]
    assert Path(written["archive"], "report.html").exists()
    assert "CONTROLLED REPLAY" in embedded_implementation(Path(written["archive"], "report.html").read_text())
    # Completed known-outcome replay uses the same daily/weekly renderer, never a
    # backdated forward experiment. No extra data is invented to close the week.
    complete = report(reader, end_day="2026-10-18", finalized=True)
    assert not complete["window_complete"]  # One observed day cannot mature a nominal future7-date window.
    assert len(complete["source_record_ids"]["runs"]) == 4


def test_forecast_pending_matures_without_modifying_original_and_stale_quote_skipped():
    forecasts = [{"forecast_id": "f", "portfolio_id": "book_rule", "target_at": T1, "expected_value": 101}]
    original = copy.deepcopy(forecasts)
    obs = Reader().observations
    pending = forecast_metrics(forecasts, obs, T0)
    assert pending[0]["outcome_status"] == "pending"
    next(o for o in obs if o["observation_id"] == "obs_rule_1")["quote_status"] = "stale"
    realized = forecast_metrics(forecasts, obs, T2)
    assert realized[0]["matched_observation_id"] == "obs_rule_2"
    assert realized[0]["signed_error"] == 1
    assert forecasts == original


def test_unequal_windows_missing_prices_and_unavailable_models_do_not_rank_as_zero():
    reader = Reader()
    reader.observations.pop()
    result = report(reader)
    assert all(b["rank"] is None for b in result["leaderboard"])
    reader = Reader()
    reader.runs[2]["status"] = "unavailable"
    reader.observations[3]["value"] = None
    result = report(reader)
    book = next(b for b in result["leaderboard"] if b["approach_id"] == "rule")
    assert book["rank"] is None and book["failure_rate"] == .5
    assert book["opening_value"] == 101


def test_cash_flow_adjustment_fees_not_double_counted_and_unknown_costs_not_zero():
    reader = Reader()
    reader.observations[-1].update(value=111, external_cash_flow=10, time_weighted_return=.01)
    reader.runs[2].update(execution_cost=.5, model_cost=None)
    result = report(reader)
    rule = next(b for b in result["leaderboard"] if b["approach_id"] == "rule")
    assert rule["net_pnl"] == 1 and rule["net_return"] == .01
    assert rule["execution_cost"] == .5  # NAV already net of execution costs.
    assert rule["model_cost"] is None


def test_html_escaping_offline_safe_download_and_csv_formula_protection(tmp_path):
    reader = Reader()
    item = reader.inputs["input_rule_0"]
    item["payload"]["news"] = {"consumed": True, "layer": "selected_headlines", "items": [{"title": '</script><img src=x onerror="alert(1)">'}]}
    item["raw_bytes"] = canonical(item["payload"]); item["metadata"]["checksum"] = checksum(item["raw_bytes"])
    reader.runs[2]["rationale"] = "=WEBSERVICE(\"malicious\")"
    result = report(reader)
    html = html_export(result)
    assert '</script><img src=x' not in html
    # Compressed original source content remains exact, and never becomes HTML.
    from finplan_platform.reporting.transport import decode_transport, unpack_json_bytes
    encoded = json.loads(html.split('<script id="report-data" type="application/json">')[1].split('</script>')[0])
    portable = decode_transport(encoded)
    assert b'</script><img src=x' in unpack_json_bytes(portable['inputs']['run_rule_0'], portable['artifact_chunks'],portable['artifact_lexemes'])
    assert "https://cdn" not in html and "connect-src 'none'" in html
    assert "inputRun" in html and "inputMatrix" in html and "exactInputDownload" in embedded_implementation(html)
    assert "'=WEBSERVICE" in csv_export(result)
    written = write_report(result, tmp_path)
    before = Path(written["archive"], "report.html").read_bytes()
    newer = copy.deepcopy(result); newer["generated_at"] = DAY+"T17:00:00+00:00"
    new_archive = write_report(newer, tmp_path)
    assert new_archive["revision"] != written["revision"]
    assert Path(written["archive"], "report.html").read_bytes() == before
    assert not (tmp_path/"latest.html").exists()  # replay never contaminates forward.


def test_secret_field_in_original_capture_is_blocked():
    reader = Reader()
    item = reader.inputs["input_rule_0"]
    item["payload"]["access_token"] = "must-not-render"
    item["raw_bytes"] = canonical(item["payload"]); item["metadata"]["checksum"] = checksum(item["raw_bytes"])
    result = report(reader)
    assert "Secret-bearing" in result["evidence_failures"][0]["error"]
    assert "must-not-render" not in html_export(result)


def test_storage_native_envelope_and_exact_non_reformatted_original_bytes():
    reader = Reader()
    for run in reader.runs:
        run["slot"] = run.pop("scheduled_slot"); run.pop("decision_time"); run.pop("availability_cutoff")
        run["outcome"] = {"cash_weight": 1, "target_weights": [], "rationale": "hold"}
    for observation in reader.observations:
        observation["payload"] = {"value": observation.pop("value"), "quote_status": "fresh"}
    def read_input_bytes(iid):
        # Source uses original whitespace: captures cannot be reconstructed later.
        return reader.inputs[iid]["raw_bytes"]
    reader.read_input_bytes = read_input_bytes
    for captured in reader.inputs.values():
        captured["metadata"].pop("run_id")
        captured["raw_bytes"] = json.dumps(captured["payload"], indent=2).encode()
        captured["metadata"]["checksum"] = checksum(captured["raw_bytes"])
    result = report(reader)
    assert result["evidence_failures"] == []
    assert result["runs"][0]["cash_weight"] == 1
    assert result["leaderboard"][0]["net_pnl"] == 2
    assert base64.b64decode(result["inputs"]["run_rule_0"]["raw_base64"]) == reader.inputs["input_rule_0"]["raw_bytes"]


def test_graph_sub_boundaries_download_original_provider_tool_and_receipt_inputs():
    reader = Reader()
    payload = {"schema_version": "graph-model-sub-boundaries/1", "run_id": "run_rule_0", "input_id": "input_rule_0", "approach_id": "rule",
               "captured_inputs": [{"boundary": "bedrock_generate", "GenerateRequest": {"prompt": "actual prompt"}},
                                   {"boundary": "ppo_policy", "observation": [0.1, 0.7], "news_consumed": False},
                                   {"boundary": "tool_call", "arguments": {"symbol": "TEST"}, "result": {"price": 12}}],
               "activity_receipts": [{"activity_event_id": "receipt_1", "status": "success"}]}
    raw = canonical(payload)
    reader.read_artifact_bytes = lambda ref: raw
    reader.runs[2]["output"] = {"graph_sub_boundary_ref": {"artifact_key": "modeling-evidence/v1/inputs/sub.json", "checksum": checksum(raw)}}
    result = report(reader)
    layer = result["additional_inputs"]["run_rule_0"][0]
    assert base64.b64decode(layer["raw_base64"]) == raw
    assert layer["payload"]["captured_inputs"][0]["GenerateRequest"]["prompt"] == "actual prompt"
    assert result["evidence_failures"] == []
    assert "Actual graph provider / policy / tool input boundaries" in embedded_implementation(html_export(result))
    payload["run_id"] = "another_run"
    raw = canonical(payload)
    reader.runs[2]["output"]["graph_sub_boundary_ref"]["checksum"] = checksum(raw)
    assert "sub-boundary run_id mismatch" in report(reader)["evidence_failures"][0]["error"]


def test_entire_missing_hour_is_visible_from_permanent_schedule_policy():
    reader = Reader()
    # Even when every strategy fails to write an entire slot, the expected policy
    # accounts for it; a scheduler timestamp alone cannot hide the missing hour.
    result = report(reader, comparison_policy={"first_scheduled_slot": T0, "cadence_seconds": 3600})
    assert len(result["missing_slots"]) == 2
    assert {r["scheduled_slot"] for r in result["missing_slots"]} == {T2}
    assert all(b["rank"] is None for b in result["leaderboard"])


def test_same_hour_different_timezone_spelling_cannot_duplicate_slot():
    reader = Reader()
    alias = copy.deepcopy(reader.runs[0])
    alias["run_id"] = "same_time_alias_run"
    alias["scheduled_slot"] = T0.replace("+00:00", "Z")
    reader.runs.append(alias)
    with pytest.raises(EvidenceError, match="permanent approach/slot"):
        report(reader)


def test_consolidated_actual_cohorts_keep_inputs_original_and_never_cross_rank_universes():
    from finplan_platform.reporting.reports import consolidate
    first = report()
    reader = Reader()
    for run in reader.runs:
        run["experiment_id"] = "eight"
        old_rid, old_iid = run["run_id"], run["input_id"]
        run["run_id"], run["input_id"], run["portfolio_id"] = "eight_"+old_rid, "eight_"+old_iid, "eight_"+run["portfolio_id"]
        item = reader.inputs.pop(old_iid)
        for layer in (item["metadata"], item["payload"]):
            layer.update(run_id=run["run_id"], input_id=run["input_id"])
        item["raw_bytes"] = canonical(item["payload"])
        item["metadata"]["checksum"] = checksum(item["raw_bytes"])
        reader.inputs[run["input_id"]] = item
    for obs in reader.observations:
        obs.update(experiment_id="eight", portfolio_id="eight_"+obs["portfolio_id"], observation_id="eight_"+obs["observation_id"])
    reg = registry()
    for a in reg:
        a.update(portfolio_id="eight_"+a["portfolio_id"], investable_universe=["TEST", "SECOND"], cohort_id="eight")
    second = aggregate(reader, experiment_id="eight", registry=reg, start_day=DAY, generated_at=T2, mode="controlled_replay")
    merged = consolidate([first, second])
    assert len(merged["registry"]) == 4 and len(merged["inputs"]) == 8
    assert {b["cohort_id"] for b in merged["leaderboard"]} == {"cohort", "eight"}
    assert sum(b["rank"] == 1 for b in merged["leaderboard"]) == 2
    assert merged["inputs"]["eight_run_rule_0"]["payload"]["approach_id"] == "rule"
    assert next(r for r in merged["runs"] if r["run_id"] == "eight_run_rule_0")["approach_id"] == "eight::rule"
    assert next(r for r in merged["runs"] if r["run_id"] == "eight_run_rule_0")["source_approach_id"] == "rule"


def test_actual_captured_inference_time_is_separate_from_hourly_slot_and_match_rows_not_valuations():
    reader = Reader()
    run = reader.runs[0]
    actual = DAY+"T14:05:00+00:00"
    run.pop("decision_time"); run.pop("availability_cutoff")
    run["slot"] = T0; run["started_at"] = actual
    item = reader.inputs[run["input_id"]]
    item["payload"].update(decision_time=actual, availability_cutoff=actual)
    item["payload"]["numeric"]["prepared"]["quote"] = {"available_at": actual, "price": 12}
    item["raw_bytes"] = canonical(item["payload"]); item["metadata"]["checksum"] = checksum(item["raw_bytes"])
    reader.observations.append({"experiment_id": "test", "approach_id": "cash", "portfolio_id": "book_cash", "observed_at": T1,
                                "observation_id": "execution_disposition", "kind": "execution_disposition", "payload": {"status": "superseded", "value": None}})
    result = report(reader)
    assert result["evidence_failures"] == []
    chosen = next(r for r in result["runs"] if r["run_id"] == "run_cash_0")
    assert chosen["scheduled_slot"] == T0 and chosen["decision_time"] == actual and chosen["availability_cutoff"] == actual
    assert len(result["observations"]) == 6 and len(result["ancillary_evidence"]) == 1
    assert next(b for b in result["leaderboard"] if b["approach_id"] == "cash")["valuations"] == 3


def test_forecast_errors_exclude_external_flows_and_historical_report_uses_durable_later_match():
    reader = Reader()
    reader.observations[-1].update(value=112, net_external_cash_flows_since_decision=10)
    frozen = {"forecast_id": "f", "forecast_checksum": "sha256:f", "target_at": T2, "expected_value": 101, "baseline_value": 100, "units": "USD"}
    reader.runs[2]["forecast"] = frozen
    result = report(reader)
    assert result["forecasts"][0]["signed_error"] == 1
    assert result["forecasts"][0]["flow_adjusted_realized_value"] == 102
    # A later report revision reads a durable match beyond the decision-day query,
    # preserving the original input/forecast. It never fetches replacement data.
    later = "2026-10-13T15:00:00+00:00"
    frozen["target_at"] = later
    reader.runs[2]["forecast_realization"] = {"forecast_checksum": "sha256:f", "run_id": "run_rule_0", "portfolio_id": "book_rule",
        "observation_id": "later_obs", "observed_at": later, "realized_value": 104, "signed_error": 3, "absolute_error": 3,
        "quote_provenance": {"provider": "controlled-test"}}
    revised = aggregate(reader, experiment_id="test", registry=registry(), start_day=DAY, generated_at=later, mode="controlled_replay")
    assert revised["forecasts"][0]["matched_observation_id"] == "later_obs"
    assert revised["forecasts"][0]["expected_value"] == 101


def test_decision_rows_and_csv_include_reconciled_daily_values_and_forecast_outcomes():
    import csv
    import io
    reader = Reader()
    reader.runs[2]["forecast"] = {"forecast_id": "forecast", "target_at": T1, "expected_value": 100.5, "baseline_value": 100, "units": "USD"}
    result = report(reader)
    row = next(r for r in result["runs"] if r["run_id"] == "run_rule_1")
    assert row["portfolio_value_at_decision"] == 101 and row["daily_pnl"] == 1 and row["daily_return"] == .01
    assert row["valuation_id"] == "obs_rule_1" and row["opening_valuation_id"] == "obs_rule_0"
    exported = list(csv.DictReader(io.StringIO(csv_export(result))))
    forecast_row = next(r for r in exported if r["run_id"] == "run_rule_0")
    assert forecast_row["expected_value"] == "100.5"
    assert forecast_row["realized_value"] == "101.0" and forecast_row["signed_error"] == "0.5" and forecast_row["forecast_status"] == "matched"


def test_window_turnover_uses_total_executed_notional_over_window_opening_not_point_rates():
    approaches = registry()
    observations = Reader().observations
    events = [{"approach_id": "rule", "observation_id": "buy", "transaction_cost": .5, "simulated_fills": [{"notional": 50}], "execution": {"turnover": .5}},
              {"approach_id": "rule", "observation_id": "sell", "transaction_cost": .55, "simulated_fills": [{"notional": 55}], "execution": {"turnover": 55/110}}]
    result = summarize(approaches, Reader().runs, observations, [], events)
    rule = next(b for b in result if b["approach_id"] == "rule")
    assert rule["turnover"] == 1.05 and rule["executed_notional"] == 105
    assert rule["turnover_definition"] == "absolute_executed_notional_divided_by_period_opening_value"


def test_observed_dates_are_not_market_sessions_without_persisted_proof():
    reader = Reader()
    result = report(reader)
    assert all(b["observed_dates"] == 1 and b["observed_sessions"] is None for b in result["leaderboard"])
    reader.observations[0]["quote_provenance"] = {"market_session": {"status": "open", "session_date": DAY, "source": "controlled-calendar-proof"}}
    result = report(reader)
    cash = next(b for b in result["leaderboard"] if b["approach_id"] == "cash")
    assert cash["observed_sessions"] == 1 and cash["market_session_dates"] == [DAY]
    assert cash["market_session_proof_coverage"] == pytest.approx(1/3)


def test_closed_market_marks_do_not_block_common_verified_open_session_window():
    reader = Reader()
    reg = registry()
    for a in reg:
        a["market_calendar"] = "XNYS"
    closed = {"market_session": {"status": "closed", "source": "controlled-calendar-proof"}}
    opened = {"market_session": {"status": "open", "session_date": DAY, "source": "controlled-calendar-proof"}}
    for observation in reader.observations:
        observation["quote_provenance"] = closed if observation["observed_at"] == T0 else opened
        if observation["approach_id"] == "rule" and observation["observed_at"] == T0:
            observation.update(quote_status="stale", observation_kind="carried_forward")
    result = aggregate(reader, experiment_id="test", registry=reg, start_day=DAY, generated_at=T2, mode="controlled_replay")
    assert {b["window_start"] for b in result["leaderboard"]} == {T1}
    assert {b["window_end"] for b in result["leaderboard"]} == {T2}
    assert all(b["rank"] is not None and b["expected_closed_observations"] == 1 for b in result["leaderboard"])
    assert result["leaderboard"][0]["approach_id"] == "rule"
    reader.observations[-1]["quote_status"] = "stale"
    blocked = aggregate(reader, experiment_id="test", registry=reg, start_day=DAY, generated_at=T2, mode="controlled_replay")
    assert next(b for b in blocked["leaderboard"] if b["approach_id"] == "rule")["rank"] is None


def test_closed_only_cash_returns_are_visible_without_declaring_a_market_winner():
    reader = Reader()
    reg = registry()
    for a in reg:
        a["market_calendar"] = "XNYS"
    for observation in reader.observations:
        observation["quote_provenance"] = {"market_session": {"status": "closed", "source": "controlled-calendar-proof"}}
    result = aggregate(reader, experiment_id="test", registry=reg, start_day=DAY, generated_at=T2, mode="controlled_replay")
    assert all(b["rank"] is None and b["observed_sessions"] == 0 for b in result["leaderboard"])
    assert next(b for b in result["leaderboard"] if b["approach_id"] == "cash")["net_return"] == 0


def test_earliest_execution_before_value_keeps_opening_fill_fee_in_observed_return():
    observations = Reader().observations
    for observation in observations:
        if observation["approach_id"] == "rule":
            observation["value"] -= .5
    events = [{"approach_id": "rule", "portfolio_id": "book_rule", "observation_id": "opening_execution", "observed_at": T0, "transaction_cost": .5,
               "simulated_fills": [{"notional": 50}], "execution": {"before_value": 100, "after_value": 99.5}}]
    result = summarize(registry(), Reader().runs, observations, [], events)
    rule = next(b for b in result if b["approach_id"] == "rule")
    assert rule["opening_value"] == 100 and rule["current_value"] == 101.5
    assert rule["net_return"] == .015 and rule["execution_cost"] == .5
    assert rule["opening_execution_event_id"] == "opening_execution"


def test_large_captured_input_portable_encoding_preserves_original_bytes_and_explicit_preview():
    from finplan_platform.reporting.reports import captured_input_bytes
    reader = Reader()
    original = reader.inputs['input_rule_0']
    original['payload']['numeric']['prepared']['complete_history'] = list(range(12000))
    original['payload']['numeric']['prepared']['original_text'] = 'permitted public text ' * 3000
    # Preserve original noncanonical whitespace: a later JSON serialization is not
    # an acceptable substitute for the captured download.
    raw = json.dumps(original['payload'], indent=1).encode()
    original['raw_bytes'] = raw
    original['metadata']['checksum'] = checksum(raw)
    result = report(reader)
    captured = result['inputs']['run_rule_0']
    assert 'raw_base64' not in captured
    assert captured['artifact_encoding'] == 'original-byte-gzip-chunks/1'
    assert captured['preview_is_complete'] is False
    assert captured['payload']['numeric']['prepared']['complete_history'] is None
    assert {item['kind'] for item in captured['preview_truncations']} == {'selection_summary'}
    assert captured_input_bytes(result, captured) == raw
    assert json.loads(captured_input_bytes(result, captured)) == original['payload']
    assert captured['metadata']['input_id'] == 'input_rule_0'
    damaged = copy.deepcopy(result)
    damaged['artifact_chunks'][captured['raw_chunks'][0]]['data'] = base64.b64encode(b'corrupt').decode()
    with pytest.raises(EvidenceError, match='compressed input chunk checksum'):
        captured_input_bytes(damaged, captured)


def test_actual_fresh_close_is_a_session_valuation_without_off_session_fills():
    from finplan_platform.reporting.reports import _compact_session_quotes
    reader = Reader()
    quote = {'market_open': False, 'status': 'fresh_close', 'quoted_at': T2,
             'available_at': DAY+'T16:01:00+00:00',
             'regular_session': {'start': T0, 'end': T2},
             'market_session': {'status': 'completed', 'session_date': DAY,
                                'source': 'controlled-provider-period-checksum'}}
    for observation in reader.observations:
        observation['quote_provenance'] = quote if observation['observed_at'] == T2 else {
            'market_session': {'status': 'open', 'session_date': DAY, 'source': 'controlled-provider'}}
        if observation['observed_at'] == T2:
            observation['quote_status'] = 'fresh_close'
    result = aggregate(reader, experiment_id='test', registry=[dict(a, market_calendar='XNYS') for a in registry()],
                       start_day=DAY, generated_at=DAY+'T17:00:00+00:00', mode='controlled_replay')
    assert all(b['valid_valuations'] == 3 and b['window_end'] == T2 for b in result['leaderboard'])
    assert all(b['observed_sessions'] == 1 and b['fills'] == 0 for b in result['leaderboard'])
    assert all(b['rank'] is not None for b in result['leaderboard'])
    assert _compact_session_quotes({'current_quotes': {'quotes': [quote]}})[0]['market_session']['status'] == 'completed'
    # The same after-close collection with only carried data does not become a
    # fresh closing mark or a simulated trade merely because the session ended.
    for observation in reader.observations:
        if observation['observed_at'] == T2:
            observation['quote_status'] = 'carried_closed'
            observation['quote_provenance']['market_session']['status'] = 'closed'
    stale = aggregate(reader, experiment_id='test', registry=[dict(a, market_calendar='XNYS') for a in registry()],
                      start_day=DAY, generated_at=DAY+'T17:00:00+00:00', mode='controlled_replay')
    assert all(b['valid_valuations'] == 2 and b['window_end'] == T1 for b in stale['leaderboard'])
    assert all(b['fills'] == 0 for b in stale['leaderboard'])


def test_after_close_collection_does_not_realize_a_later_than_close_forecast():
    forecasts = [{'forecast_id': 'close-horizon', 'portfolio_id': 'book_rule',
                  'target_at': DAY+'T16:30:00+00:00', 'expected_value': 102}]
    closing = {'observation_id': 'close', 'portfolio_id': 'book_rule', 'value': 101,
               'status': 'valued', 'quote_status': 'fresh_close',
               'observed_at': DAY+'T17:05:00+00:00', 'value_as_of': T2}
    cutoff = DAY+'T18:00:00+00:00'
    assert forecast_metrics(forecasts, [closing], cutoff)[0]['outcome_status'] == 'pending_market_data'
    later = dict(closing, observation_id='later', observed_at=cutoff, value_as_of=cutoff, value=103)
    matched = forecast_metrics(forecasts, [closing, later], cutoff)[0]
    assert matched['matched_observation_id'] == 'later' and matched['signed_error'] == 1
    assert matched['value_as_of'] == cutoff
    assert forecasts[0]['expected_value'] == 102


def _superseded_reader():
    reader = Reader()
    reader.execution_events_supported = True
    run = reader.runs[2]
    disposition = {'schema_version': 'modeling-execution-disposition/1', 'status': 'superseded',
                   'run_id': run['run_id'], 'input_id': run['input_id'], 'experiment_id': 'test',
                   'approach_id': 'rule', 'portfolio_id': run['portfolio_id'], 'decision_slot': T0,
                   'replacement_run_id': 'run_rule_1', 'replacement_input_id': 'input_rule_1',
                   'replacement_slot': T1, 'reason': 'Latest common-hourly policy supersedes the pending policy',
                   'recorded_at': T1, 'ledger_committed': False, 'simulated_fills': []}
    run.update(status='superseded', execution_disposition=disposition,
               execution_disposition_ref={'artifact_key': 'modeling-evidence/v1/execution-disposition/original.json',
                                          'checksum': checksum(canonical(disposition))},
               outcome={'status': 'succeeded', 'target_weights': {'TEST': .5}, 'cash_weight': .5,
                        'rationale': 'Original successful inference retained', 'usage': {'cost_usd': .17, 'cost_basis': 'observed'},
                        'execution_status': 'pending_next_hourly_regular_session_quote'})
    run.pop('model_cost')
    reader.observations.append({'experiment_id': 'test', 'approach_id': 'rule', 'portfolio_id': 'book_rule',
                                'run_id': run['run_id'], 'observation_id': 'immutable_disposition',
                                'kind': 'execution_disposition', 'observed_at': T1, 'payload': disposition})
    return reader


def test_superseded_disposition_retains_recommendation_inputs_and_cost_without_fake_execution():
    import csv
    import io
    reader = _superseded_reader()
    original = copy.deepcopy(reader.runs[2]['outcome'])
    receipt = {'run_id': 'run_rule_1', 'portfolio_id': 'book_rule', 'decision_id': 'actual_decision',
               'simulated_fills': [{'notional': 50}], 'transaction_cost': .5,
               'execution': {'execution_at': T1, 'before_value': 100, 'after_value': 99.5, 'turnover': .5}}
    event = {'experiment_id': 'test', 'approach_id': 'rule', 'portfolio_id': 'book_rule', 'run_id': 'run_rule_1',
             'decision_id': 'actual_decision', 'observation_id': 'actual_paper_execution', 'kind': 'paper_execution',
             'observed_at': T1, 'payload': receipt}
    reader.observations.extend([event, copy.deepcopy(event), copy.deepcopy(reader.observations[-1])])
    result = report(reader)
    row = next(r for r in result['runs'] if r['run_id'] == 'run_rule_0')
    assert row['fill_status'] == 'superseded_not_executed' and row['execution_status'] == 'superseded'
    assert row['fill_time'] is None and row['fills'] == [] and row['execution_cost'] == 0
    assert row['target_weights'] == {'TEST': .5} and row['model_cost'] == .17
    assert row['output'] == original and reader.runs[2]['outcome'] == original
    assert result['inputs'][row['run_id']]['checksum'] == reader.inputs[row['input_id']]['metadata']['checksum']
    assert len(result['runs']) == 4 and len(result['observations']) == 6
    assert len(result['execution_events']) == 1 and len(result['ancillary_evidence']) == 2
    book = next(b for b in result['leaderboard'] if b['approach_id'] == 'rule')
    assert book['fills'] == 1 and book['execution_cost'] == .5 and book['turnover'] == .5
    assert book['model_cost'] == .17 and book['recommendations'] == 2 and book['failure_rate'] == 0
    assert not result['evidence_failures']
    exported = next(r for r in csv.DictReader(io.StringIO(csv_export(result))) if r['run_id'] == row['run_id'])
    assert exported['fill_status'] == 'superseded_not_executed'
    assert json.loads(exported['execution_disposition'])['replacement_run_id'] == 'run_rule_1'
    assert 'Not executed:' in embedded_implementation(html_export(result)) and 'Replacement run' in embedded_implementation(html_export(result))


@pytest.mark.parametrize('mutation,message', [
    (lambda r: r['execution_disposition'].update(portfolio_id='another_book'), 'portfolio_id mismatch'),
    (lambda r: r['execution_disposition'].update(ledger_committed=True), 'explicit superseded nonexecution'),
    (lambda r: r.update(execution_ref={'checksum': 'actual_ledger'}), 'conflicts with committed'),
    (lambda r: r.update(execution_disposition=None), 'disposition unavailable'),
])
def test_disposition_mismatch_or_claimed_ledger_action_remains_visible_coverage_error(mutation,message):
    reader = _superseded_reader()
    mutation(reader.runs[2])
    result = report(reader)
    assert any(message in e['error'] for e in result['evidence_failures'])
    assert next(b for b in result['leaderboard'] if b['approach_id'] == 'rule')['rank'] is None


def test_later_supersession_revises_cutoff_without_backdating_or_filling_original_decision():
    reader = _superseded_reader()
    later = '2026-10-13T14:00:00Z'
    reader.runs[2]['execution_disposition']['recorded_at'] = later
    result = aggregate(reader, experiment_id='test', registry=registry(), start_day=DAY,
                       generated_at=later, mode='controlled_replay')
    row = next(r for r in result['runs'] if r['run_id'] == 'run_rule_0')
    assert result['data_cutoff'] == later and row['decision_time'] == T0
    assert row['fill_time'] is None and row['execution_disposition_at'] == later


def _excluded_reader(reason_code):
    reader = _superseded_reader()
    run = reader.runs[2]
    disposition = run['execution_disposition']
    for field in ('replacement_run_id', 'replacement_input_id', 'replacement_slot'):
        disposition.pop(field)
    disposition.update(status='excluded_nontrading', reason_code=reason_code,
                       reason='Manual/late decision is not eligible for declared hourly comparison execution',
                       eligible_from_slot=T1, latest_started_at_for_slot=T1,
                       registry_input_ref={'input_id': 'actual_registry', 'checksum': 'sha256:registry'})
    run.update(status='execution_excluded')
    run['execution_disposition_ref']['checksum'] = checksum(canonical(disposition))
    reader.observations[3]['run_id'] = run['run_id']
    return reader


def test_manual_before_horizon_preserves_rows_but_cannot_create_weekly_coverage_or_forecast_match():
    reader = _excluded_reader('before_declared_comparison_horizon')
    run = reader.runs[2]
    forecast = {'forecast_id': 'manual-forecast', 'forecast_checksum': 'sha256:manual', 'target_at': T1,
                'expected_value': 100.5, 'baseline_value': 100, 'units': 'USD'}
    run['outcome']['forecast'] = forecast
    run['forecast_realization'] = {'forecast_checksum': 'sha256:manual', 'run_id': run['run_id'],
        'portfolio_id': run['portfolio_id'], 'observation_id': 'obs_rule_1', 'observed_at': T1,
        'realized_value': 101, 'signed_error': .5, 'absolute_error': .5}
    original_input = reader.inputs[run['input_id']]['raw_bytes']
    result = report(reader, comparison_policy={'first_scheduled_slot': T1, 'cadence_seconds': 3600})
    row = next(r for r in result['runs'] if r['run_id'] == run['run_id'])
    assert row['fill_status'] == 'excluded_nontrading' and row['comparison_eligible'] is False
    assert row['fills'] == [] and row['fill_time'] is None and row['model_cost'] == .17
    assert row['output']['forecast'] == forecast
    assert base64.b64decode(result['inputs'][run['run_id']]['raw_base64']) == original_input
    assert result['forecasts'][0]['outcome_status'] == 'nontrading_not_applied'
    assert result['forecasts'][0]['realized_value'] is None
    assert result['forecasts'][0]['stored_noncomparable_realization'] == run['forecast_realization']
    book = next(b for b in result['leaderboard'] if b['approach_id'] == 'rule')
    assert book['recommendations'] == 2 and book['comparison_recommendations'] == 1 and book['nontrading_recommendations'] == 1
    assert book['valuations'] == 3 and book['valid_valuations'] == 2 and book['nontrading_valuations'] == 1
    assert book['window_start'] == T1 and book['model_cost'] == .17 and book['comparison_model_cost'] == 0
    assert book['forecast_matches'] == 0 and book['failure_rate'] == 0
    assert all(m['scheduled_slot'] != T0 for m in result['missing_slots'])
    assert result['missing_slots'] and not result['window_complete']
    assert len(result['runs']) == 4 and result['execution_events'] == []


def test_late_created_exclusion_is_no_trade_but_remains_failed_hourly_coverage():
    reader = _excluded_reader('created_after_decision_slot_closed')
    result = report(reader)
    row = next(r for r in result['runs'] if r['run_id'] == 'run_rule_0')
    assert row['fill_status'] == 'excluded_nontrading' and row['fills'] == []
    assert 'replacement_run_id' not in row['execution_disposition']
    book = next(b for b in result['leaderboard'] if b['approach_id'] == 'rule')
    assert book['failure_rate'] == .5 and book['rank'] is None
    assert book['fills'] == 0 and book['execution_cost'] == 0 and book['model_cost'] == .17
    assert result['evidence_failures'] == []


def test_private_exports_repair_existing_permissive_files_and_preserve_unrelated_demo(tmp_path):
    import os
    import stat
    output = tmp_path/'owned-reports'
    output.mkdir(mode=0o775)
    demo = output/'demo.html'; demo.write_text('Approved unrelated reference'); demo.chmod(0o664)
    previous = os.umask(0o002)
    try:
        data = report()
        first = write_report(data, output)
        archive = Path(first['archive'])
        assert stat.S_IMODE(archive.stat().st_mode) == 0o700
        assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
        paths = list(archive.iterdir()) + [Path(first['directory'])/f'latest.{ext}' for ext in ('html','json','csv')]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
        for path in paths: path.chmod(0o664)
        second = write_report(data, output)
        assert first['revision'] == second['revision']
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
        assert demo.read_text() == 'Approved unrelated reference' and stat.S_IMODE(demo.stat().st_mode) == 0o664
        current = os.umask(0o002)
        assert current == 0o002  # Library writer restores its caller's umask.
    finally:
        os.umask(previous)
