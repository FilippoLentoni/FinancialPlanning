"""Consolidated costs retain permanent job ownership and actual terminal day."""
from copy import deepcopy

import pytest

from finplan_platform.reporting.costs import apply_managed_costs, collect_managed_cost_events
from finplan_platform.reporting.reports import EvidenceError, consolidate, summarize


def cost(experiment, *, job=None, at="2026-01-13T05:00:05Z", **changes):
    job = job or "job-" + experiment
    return {"protocol": "finplan-managed-model-cost/1", "event_id": "managed:" + job,
            "managed_run_id": job, "experiment_id": experiment, "approach_id": "jev",
            "portfolio_id": "book-" + experiment, "occurred_at": at,
            "compute_observed_usd": 1.2, "vendor_observed_usd": .3,
            "compute_estimated_usd": 4.5, "vendor_estimated_usd": .8,
            "compute_cost_basis": "observed_billing", "vendor_cost_basis": "observed_vendor_usage",
            "source_evidence_refs": [{"checksum": "controlled-public-receipt"}], **changes}


def report(experiment, *, event=None, day="2026-01-13", observed_start="2026-01-13T05:00:00Z",
           horizon="2026-01-13T05:00:00Z", comparison_peer=False):
    event = event or cost(experiment)
    registry = [{"approach_id": "jev", "portfolio_id": "book-" + experiment,
                 "family": "language_model", "comparison_policy_id": "policy",
                 "cohort_id": experiment, "investable_universe": ["VOO"],
                 "starting_capital": 1000, "comparison_horizon_start": horizon}]
    runs = [{"run_id": experiment + "-run-" + str(index), "experiment_id": experiment,
             "approach_id": "jev", "portfolio_id": "book-" + experiment,
             "decision_time": at, "status": "succeeded", "output": {"status": "succeeded"},
             "model_cost": .1, "model_cost_basis": "controlled_observed",
             "inference_invocation_state": "invoking", "managed_cost_event": deepcopy(event)}
            for index, at in enumerate((observed_start, "2026-01-13T06:00:00Z"))]
    observations = [{"observation_id": experiment + "-valuation-" + str(index),
                     "experiment_id": experiment, "approach_id": "jev", "portfolio_id": "book-" + experiment,
                     "observed_at": at, "value": value, "observation_kind": "valid",
                     "quote_status": "fresh"}
                    for index, (at, value) in enumerate(((observed_start, 1000), ("2026-01-13T06:00:00Z", 1010)))]
    receipt = {"kind": "managed_model_cost:" + event["managed_run_id"],
               "experiment_id": experiment, "approach_id": "jev", "portfolio_id": "book-" + experiment,
               "observation_id": experiment + "-cost", "slot": event["occurred_at"],
               "checksum": "controlled-cost-artifact", "payload": deepcopy(event)}
    events = collect_managed_cost_events(runs, [receipt], experiment_id=experiment, registry=registry,
                                        start_day=day, end_day=day, generated_at="2026-01-14T12:00:00Z")
    # Real execution costs already affect the authoritative book. Managed costs
    # are separate and must not subtract another amount from cash/P&L or fees.
    executions = [{"observation_id": experiment + "-execution", "approach_id": "jev",
                   "observed_at": "2026-01-13T05:30:00Z", "transaction_cost": 2,
                   "simulated_fills": [{"notional": 100}], "execution": {"turnover": .1}}]
    if comparison_peer:
        registry.append({**registry[0], "approach_id": "cash", "portfolio_id": "cash-" + experiment, "family": "control"})
        runs.extend({**r, "run_id": r["run_id"] + "-cash", "approach_id": "cash",
                     "portfolio_id": "cash-" + experiment, "model_cost": 0, "managed_cost_event": None}
                    for r in list(runs))
        observations.extend({**o, "observation_id": o["observation_id"] + "-cash", "approach_id": "cash",
                             "portfolio_id": "cash-" + experiment, "value": 1000}
                            for o in list(observations))
    books = summarize(registry, runs, observations, [], executions)
    apply_managed_costs(books, events, registry)
    return {"experiment_id": experiment, "start_day": day, "end_day": day, "mode": "forward",
            "generated_at": "2026-01-14T12:00:00Z", "period": "daily", "timezone": "America/New_York",
            "comparison_policy": {"initial_capital": 1000}, "expected_slot_manifest_available": True,
            "registry": registry, "runs": runs, "observations": observations, "forecasts": [],
            "missing_slots": [], "evidence_failures": [], "inputs": {}, "execution_events": executions,
            "managed_cost_events": events, "leaderboard": books, "ancillary_evidence": [receipt],
            "source_record_ids": {"runs": [r["run_id"] for r in runs],
                                  "observations": [o["observation_id"] for o in observations]},
            "data_cutoff": "2026-01-13T06:00:00Z", "window_complete": False, "finalized": False}


def books(result):
    return {b["approach_id"]: b for b in result["leaderboard"]}


def test_two_cohort_reused_jobs_keep_observed_costs_once_and_do_not_change_book_returns():
    first, second = report("five", comparison_peer=True), report("eight", comparison_peer=True)
    original = deepcopy((first, second))
    combined = consolidate([first, second])
    assert (first, second) == original
    assert len(combined["runs"]) == 8 and len(combined["managed_cost_events"]) == 2
    for book in (b for b in books(combined).values() if b["family"] == "language_model"):
        assert book["managed_costs"]["event_count"] == 1
        assert book["managed_costs"]["total_observed_usd"] == pytest.approx(1.5)
        assert book["managed_costs"]["compute_estimated_usd"] == 4.5
        assert book["model_cost"] == pytest.approx(1.7)  # Two .1 invocations + one actual job.
        assert book["comparison_model_cost"] == pytest.approx(1.7)
        assert book["observed_model_cost"] == pytest.approx(1.7)
        assert book["net_pnl"] == 10 and book["net_return"] == pytest.approx(.01)
        assert book["execution_cost"] == 2 and book["fills"] == 1


def test_same_immutable_job_reference_in_two_cohort_views_is_charged_only_to_original_book():
    first, second = report("five"), report("eight")
    shared = deepcopy(first["managed_cost_events"][0])
    shared["evidence_sources"] = [{"run_id": "eight-retained-reference", "source": "original-job-reference"}]
    second["managed_cost_events"].append(shared)
    combined = consolidate([first, second])
    assert len(combined["managed_cost_events"]) == 2
    original_event = next(e for e in combined["managed_cost_events"] if e["managed_run_id"] == "job-five")
    assert original_event["approach_id"] == "five::jev"
    assert len(original_event["evidence_sources"]) == 4  # Two reuses, original receipt, cross-view reference.
    for book in books(combined).values():
        assert book["managed_costs"]["event_count"] == 1
        assert book["model_cost"] == pytest.approx(1.7)


def test_unknown_billing_and_independent_estimates_remain_unknown_after_consolidation():
    unknown = cost("eight", compute_observed_usd=None, compute_estimated_usd=7,
                   compute_cost_basis="estimated_instance_seconds_rate")
    combined = consolidate([report("five"), report("eight", event=unknown)])
    book = books(combined)["eight::jev"]
    assert book["model_cost"] is None and book["managed_costs"]["total_observed_usd"] is None
    assert book["managed_costs"]["compute_estimated_usd"] == 7
    assert book["managed_costs"]["known_vendor_observed_usd"] == .3
    assert book["managed_costs"]["unknown_observed_event_count"] == 1
    assert book["net_pnl"] == 10 and book["execution_cost"] == 2


def test_actual_terminal_new_york_day_and_observed_window_are_not_retimed_by_reuse():
    # Job terminal is Jan13 00:00:05NY; its later reuse cannot charge Jan12.
    first, second = report("five", day="2026-01-12"), report("eight", day="2026-01-12")
    before = consolidate([first, second])
    assert all(b["managed_costs"]["event_count"] == 0 and b["model_cost"] == pytest.approx(.2)
               for b in before["leaderboard"])
    # On actual day the job is included in the total, but predates both the
    # declared comparison horizon and observed valuation window.
    later = consolidate([report("five", observed_start="2026-01-13T05:30:00Z", horizon="2026-01-13T05:30:00Z"),
                         report("eight", observed_start="2026-01-13T05:30:00Z", horizon="2026-01-13T05:30:00Z")])
    for book in later["leaderboard"]:
        assert book["model_cost"] == pytest.approx(1.7)
        assert book["comparison_model_cost"] == pytest.approx(.2)
        assert book["comparison_managed_costs"]["event_count"] == 0
        assert book["observed_window_managed_costs"]["event_count"] == 0
        assert book["managed_costs"]["event_ids"] == ["managed:job-" + book["cohort_id"]]


@pytest.mark.parametrize("change", [{"vendor_observed_usd": .4}, {"portfolio_id": "book-eight", "experiment_id": "eight"}])
def test_same_permanent_job_with_conflicting_payload_or_book_is_rejected(change):
    first, second = report("five"), report("eight")
    second["managed_cost_events"].append({**deepcopy(first["managed_cost_events"][0]), **change})
    with pytest.raises(EvidenceError, match="Conflicting immutable"):
        consolidate([first, second])


def test_cost_reference_without_its_original_book_cannot_be_reassigned_to_another_cohort():
    first, second = report("five"), report("eight")
    second["managed_cost_events"].append({**cost("unknown"), "counted_in_reporting_window": True,
                                           "evidence_sources": []})
    with pytest.raises(EvidenceError, match="another consolidated book"):
        consolidate([first, second])
