"""CS-07: completion_status separate from solution_status."""

from finplan_contracts.validate import validate

from conftest import fixture


def test_infeasible_is_succeeded_plus_infeasible():
    r = fixture("job-result/valid/succeeded-infeasible.json")
    assert r["completion_status"] == "succeeded" and r["solution_status"] == "infeasible" and "error" not in r
    assert validate(r, "job-result").valid
    assert not validate(fixture("job-result/invalid/infeasible-reported-as-failure.json"), "job-result").valid


def test_crash_is_failed_plus_error_without_solution_status():
    r = fixture("job-result/valid/failed-crash.json")
    assert r["completion_status"] == "failed" and "solution_status" not in r and validate(r["error"], "error").valid
    assert validate(r, "job-result").valid
    assert not validate(fixture("job-result/invalid/failed-without-error.json"), "job-result").valid


def test_no_effect_is_a_success():
    r = fixture("job-result/valid/succeeded-no-effect.json")
    assert (r["completion_status"], r["solution_status"]) == ("succeeded", "no_effect") and validate(r, "job-result").valid


def test_enums(store):
    defs = store.get("job-status").schema["$defs"]
    assert defs["completion_status"]["enum"] == ["succeeded", "failed", "cancelled", "timed_out"]
    assert defs["solution_status"]["enum"] == ["optimal", "feasible", "infeasible", "unbounded", "no_effect", "not_applicable"]


def test_partial_artifacts_flagged_on_timeout_or_cancel():
    assert validate(fixture("job-result/valid/timed-out-partial-artifacts.json"), "job-result").valid
    assert not validate(fixture("job-result/invalid/timed-out-artifacts-complete.json"), "job-result").valid


def test_staged_output_manifest_follows_the_same_rule():
    assert validate(fixture("staged-output-manifest/valid/succeeded-infeasible-no-files.json"), "staged-output-manifest").valid
    assert not validate(fixture("staged-output-manifest/invalid/failed-with-solution-status.json"), "staged-output-manifest").valid
