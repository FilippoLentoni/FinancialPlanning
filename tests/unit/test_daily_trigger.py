"""Daily recommendation trigger (daily-recommendation-trigger): DLY-01 to DLY-05 with stubs.

The FinanceModel job API, the plan API and SSM are in-memory stubs; nothing calls AWS, SageMaker
or FinanceModel. The control flow is :func:`run_inline`, the in-process twin of the state machine."""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts import ssm as contract_ssm
from finplan_contracts.validate import validate
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.config import load_config
from finplan_platform.core.daily_trigger import (
    OUTCOMES,
    TriggerDeps,
    idempotency_key,
    research_plan_ref_parameter,
    run_inline,
    start_input,
    submission,
)

UNIVERSE = "finance/equity-etf-daily/research-universe"
SNAP = "snap_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K"
PLAN = "pl_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K"
RUN = "run_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K"
PV = "pv_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K"
DISCLOSURES = load_config("beta").universe.disclosures()


class ParameterNotFound(Exception):
    pass


class StubSsm:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []

    def get_parameter(self, Name: str) -> dict[str, Any]:
        self.calls.append(Name)
        if Name not in self.values:
            raise ParameterNotFound(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Name": Name, "Value": self.values[Name]}}


class StubJobApi:
    """Idempotent by key like FinanceModel's job API: a duplicate submission returns the same run_id."""

    def __init__(self, states: list[str] | None = None, submit_error: str | None = None) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.runs: dict[str, str] = {}
        self.states = list(states or ["running", "succeeded"])
        self.submit_error = submit_error
        self.status_calls = 0

    @property
    def calls(self) -> int:
        return len(self.submissions) + self.status_calls

    def submit_job(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.submissions.append(body)
        if self.submit_error:
            return 402, {"error": {"code": self.submit_error, "retryable": False}}
        run = self.runs.setdefault(body["idempotency_key"], RUN[:-2] + f"{len(self.runs):02d}".replace("0", "A").replace("1", "B"))
        return 202, {"run_id": run, "state": "queued"}

    def get_job_status(self, run_id: str) -> tuple[int, dict[str, Any]]:
        self.status_calls += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        out = {"run_id": run_id, "state": state}
        if state in ("succeeded", "failed"):
            out["completion_status"] = state
        return 200, out


class StubPlanApi:
    def __init__(self, snapshot_status: str = "approved", accept_outcome: str = "accepted") -> None:
        self.snapshot_status = snapshot_status
        self.accept_outcome = accept_outcome
        self.accepts: list[dict[str, Any]] = []
        self.publishes = 0
        self.versions: list[dict[str, Any]] = []

    def get_snapshot(self, sid: str) -> tuple[int, dict[str, Any]]:
        return 200, {"snapshot": {"input_snapshot_id": sid, "status": self.snapshot_status, "coverage": {"start": "2010-10-01", "end": "2026-01-09"}, "bias_disclosures": DISCLOSURES}}

    def get_plan(self, plan_id: str) -> tuple[int, dict[str, Any]]:
        return 200, {"plan": {"plan_id": plan_id, "head": {"current_version_id": None, "revision": 3 + len(self.versions)}}}  # contract shape

    def accept(self, plan_id: str, run_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.accepts.append(body)
        if self.accept_outcome == "accepted":
            self.versions.append({"plan_version_id": PV, "origin": "model_run", "status": "validated", "bias_disclosures": DISCLOSURES})
            return 200, {"outcome": "accepted", "plan_version_id": PV, "status": "validated", "origin": "model_run"}
        return 200, {"outcome": self.accept_outcome}


def _deps(*, strategy: str | None = '{"strategy_id": "buy_and_hold"}', denied: bool = False, jobs: StubJobApi | None = None, plans: StubPlanApi | None = None) -> tuple[TriggerDeps, dict[str, bytes]]:
    values = {research_plan_ref_parameter("beta"): PLAN}
    if strategy is not None:
        values[contract_ssm.production_strategy_parameter("beta")] = strategy
    written: dict[str, bytes] = {}
    deps = TriggerDeps(
        config=load_config("beta"),
        ssm=StubSsm(values),
        job_api=jobs or StubJobApi(),
        plan_api=plans or StubPlanApi(),
        write_outcome=lambda k, b: written.__setitem__(k, b),
        budget_denied=lambda: denied,
        clock=FrozenClock("2026-01-12T14:40:00Z"),
    )
    return deps, written


def _event(**over: Any) -> dict[str, Any]:
    resp = {"input_snapshot_id": SNAP, "trigger": "scheduled", "new_snapshot": True, "requested_range": {"start": "2010-10-01", "end": "2026-01-09"}, "snapshot": {"dataset": {"dataset_id": UNIVERSE}, "status": "approved", "quality_flags": []}}
    resp.update(over)
    return {"source": "lambda", "detail": {"requestPayload": {"source": "finplan.scheduler", "trigger": "scheduled", "dataset_id": UNIVERSE}, "responsePayload": resp}}


def _run(deps: TriggerDeps, event: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_inline(start_input(event or _event(), "beta"), deps)


# ------------------------------------------------------------------ DLY-01
def test_snapshot_not_approved_records_skipped_snapshot_without_job_api_calls() -> None:
    jobs = StubJobApi()
    deps, written = _deps(jobs=jobs, plans=StubPlanApi(snapshot_status="committed"))
    out = _run(deps)
    assert out["outcome"] == "skipped_snapshot" and out["snapshot_status"] == "committed"
    assert jobs.calls == 0 and len(written) == 1


def test_no_session_and_non_universe_events_make_no_call() -> None:
    jobs = StubJobApi()
    deps, _ = _deps(jobs=jobs)
    assert _run(deps, _event(new_snapshot=False, input_snapshot_id=None, quality_flags=["no_session"]))["outcome"] == "no_session"
    ev = _event()
    ev["detail"]["responsePayload"]["snapshot"]["dataset"]["dataset_id"] = "finance/etf-daily/SPY"
    assert _run(deps, ev)["outcome"] == "skipped_snapshot"
    assert jobs.calls == 0


# ------------------------------------------------------------------ DLY-02
@pytest.mark.parametrize("strategy", [None, "", "{}", '{"strategy_id": ""}', "not json"])
def test_no_production_strategy_makes_zero_job_api_calls(strategy: str | None) -> None:
    jobs, plans = StubJobApi(), StubPlanApi()
    deps, written = _deps(strategy=strategy, jobs=jobs, plans=plans)
    out = _run(deps)
    assert out["outcome"] == "skipped_no_strategy"
    assert jobs.calls == 0 and plans.accepts == [] and plans.versions == []
    rec = json.loads(next(iter(written.values())))
    assert rec["outcome"] == "skipped_no_strategy" and rec["published"] is False


# ------------------------------------------------------------------ DLY-03
def test_budget_deny_active_records_skipped_budget_without_submit() -> None:
    jobs = StubJobApi()
    deps, _ = _deps(denied=True, jobs=jobs)
    assert _run(deps)["outcome"] == "skipped_budget" and jobs.calls == 0


def test_budget_exceeded_from_financemodel_is_skipped_budget() -> None:
    jobs = StubJobApi(submit_error="BUDGET_EXCEEDED")
    deps, _ = _deps(jobs=jobs)
    out = _run(deps)
    assert out["outcome"] == "skipped_budget" and len(jobs.submissions) == 1 and jobs.status_calls == 0


# ------------------------------------------------------------------ DLY-04
def test_submission_is_a_contract_valid_daily_recommendation() -> None:
    deps, _ = _deps()
    state = {"input_snapshot_id": SNAP, "session_date": "2026-01-09", "strategy_id": "buy_and_hold", "run_tag": None}
    body = submission(state, deps, PLAN)
    assert validate(body, "job-submission").valid
    assert (body["job_type"], body["purpose"], body["plan_id"]) == ("daily_recommendation", "production_candidate", PLAN)
    assert body["idempotency_key"] == "daily-beta-2026-01-09" == idempotency_key("beta", "2026-01-09")
    assert body["configuration"]["payload"]["strategy"] == "buy_and_hold"


def test_duplicate_start_yields_the_same_run_id() -> None:
    jobs = StubJobApi(states=["succeeded"])
    deps, _ = _deps(jobs=jobs)
    a, b = _run(deps), _run(deps)
    assert a["run_id"] == b["run_id"]
    assert len({s["idempotency_key"] for s in jobs.submissions}) == 1 and len(jobs.runs) == 1


def test_run_tag_suffixes_key_and_execution_name() -> None:
    jobs = StubJobApi(states=["succeeded"])
    deps, _ = _deps(jobs=jobs)
    out = run_inline(start_input({**{k: v for k, v in {"input_snapshot_id": SNAP, "dataset_id": UNIVERSE, "trigger": "scheduled", "session_date": "2026-01-09"}.items()}, "run_tag": "it-42"}, "beta"), deps)
    assert jobs.submissions[0]["idempotency_key"] == "daily-beta-2026-01-09-it-42"
    assert out["execution_name"] == "daily-beta-2026-01-09-it-42"
    with pytest.raises(ValueError):
        start_input({"run_tag": "Bad Tag"}, "beta")


def test_polling_is_bounded_to_45_minutes() -> None:
    jobs = StubJobApi(states=["running"])
    deps, _ = _deps(jobs=jobs)
    out = _run(deps)
    assert out["outcome"] == "timed_out" and jobs.status_calls == 9
    cfg = deps.config.daily_trigger
    assert cfg["poll_interval_seconds"] * cfg["max_polls"] <= 45 * 60


def test_failed_run_is_run_failed_without_acceptance() -> None:
    plans = StubPlanApi()
    deps, _ = _deps(jobs=StubJobApi(states=["running", "failed"]), plans=plans)
    assert _run(deps)["outcome"] == "run_failed" and plans.accepts == []


# ------------------------------------------------------------------ DLY-05
def test_successful_run_is_an_unpublished_model_run_version_with_disclosures() -> None:
    plans = StubPlanApi()
    deps, written = _deps(plans=plans)
    out = _run(deps)
    assert out["outcome"] == "pending_approval" and out["plan_version_id"] == PV and out["plan_version_status"] == "validated"
    assert plans.versions == [{"plan_version_id": PV, "origin": "model_run", "status": "validated", "bias_disclosures": DISCLOSURES}]
    assert plans.publishes == 0 and out["published"] is False
    assert plans.accepts[0]["idempotency_key"] == f"accept-{out['run_id']}" and plans.accepts[0]["expected_revision"] == 3
    rec = json.loads(next(iter(written.values())))
    assert rec["bias_disclosures"] == DISCLOSURES and rec["plan_version_id"] == PV


def test_no_version_outcome() -> None:
    deps, _ = _deps(plans=StubPlanApi(accept_outcome="no_version"))
    assert _run(deps)["outcome"] == "no_version"


def test_outcome_enum_matches_docs() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "docs" / "daily-trigger.md").read_text()
    for o in OUTCOMES:
        assert f"`{o}`" in text, o


def test_fake_plan_api_matches_the_contract_get_plan_response():
    """Regression: the fake returned plan.revision, the real API plan.head.revision, so DLY-05 passed
    offline while the first beta daily loop failed at Accept."""
    fake = StubPlanApi()
    status, doc = fake.get_plan("pl_01KDVDNAZ83BAMMYCEGWF33DPM")
    assert status == 200 and "revision" in doc["plan"]["head"]
