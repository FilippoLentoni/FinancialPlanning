"""Post-ingestion daily recommendation trigger (daily-recommendation-trigger; tasks 3.1-3.3; DLY-01 to DLY-06;
design T1-T3).

The scheduled ingestion's success event for the research universe starts a Step Functions Standard
state machine (``infra/stacks/daily_trigger.py``) whose states each call :func:`run_step` through the
step Lambda::

    CheckSnapshot -> ReadStrategy -> CheckBudget -> SubmitJob -> (Wait 5 min -> Poll) x <= 9 -> Accept -> WriteOutcome

Every state passes one JSON ``state`` object along; a state that decides the run is over sets
``outcome`` and the machine jumps to ``WriteOutcome``. Outcomes (:data:`OUTCOMES`, documented in
``docs/daily-trigger.md``):

``no_session``          the scheduled ingestion found no session (holiday or weekend); no FinanceModel call
``skipped_snapshot``    the universe snapshot is not ``approved`` (or not a scheduled universe snapshot); no FinanceModel call
``skipped_no_strategy`` ``/finplan/<env>/financemodel/config/production-strategy`` is absent, empty or has no
                        ``strategy_id``; no job, no benchmark, no plan version (decision 22)
``skipped_budget``      the budget deny action is active (pre-check), or FinanceModel answered ``BUDGET_EXCEEDED``
``pending_approval``    the run succeeded and the staged output was accepted as a ``validated`` (or
                        ``invalid``) plan version with origin ``model_run``; it is **unpublished** and only the
                        user publishes it through the existing publish route
``no_version``          the run finished without a committable solution (acceptance recorded ``no_version``)
``run_failed``          the run failed, was cancelled, or its output was rejected
``timed_out``           no terminal job state within ``max_polls`` x ``poll_interval_seconds`` (45 minutes)

Idempotency (DLY-04): the execution name and the job idempotency key are ``daily-<env>-<session_date>``
(a test start adds ``-<run_tag>`` to both), so a duplicate start observes the same ``run_id`` and one
SageMaker job runs; the acceptance key is ``accept-<run_id>``.

The trigger never publishes: it has no publish client, and its role is denied the publish route both
by IAM (explicit deny) and by the API router (role class ``automation``) (DLY-06).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from finplan_contracts import ssm as contract_ssm
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate

from .clock import Clock, SystemClock, to_timestamp
from .config import EnvConfig

__all__ = [
    "OUTCOMES",
    "OUTCOMES_RESPONSE",
    "TERMINAL_JOB_STATES",
    "JobApi",
    "PlanApi",
    "TriggerDeps",
    "execution_name",
    "get_outcomes",
    "idempotency_key",
    "outcome_key",
    "research_plan_ref_parameter",
    "run_step",
    "start_input",
]

OUTCOMES = ("no_session", "skipped_snapshot", "skipped_no_strategy", "skipped_budget", "pending_approval", "no_version", "run_failed", "timed_out")
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled", "timed_out")
JOB_TYPE = "daily_recommendation"
PURPOSE = "production_candidate"
_RUN_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class JobApi(Protocol):
    """FinanceModel job API (IAM auth): ``submit_job`` and ``get_job_status``. Errors return the contract envelope."""

    def submit_job(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]: ...
    def get_job_status(self, run_id: str) -> tuple[int, dict[str, Any]]: ...


class PlanApi(Protocol):
    """Platform plan API (IAM auth) as the trigger role: snapshot and plan reads and staged-output acceptance only."""

    def get_snapshot(self, input_snapshot_id: str) -> tuple[int, dict[str, Any]]: ...
    def get_plan(self, plan_id: str) -> tuple[int, dict[str, Any]]: ...
    def accept(self, plan_id: str, run_id: str, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]: ...


@dataclass
class TriggerDeps:
    config: EnvConfig
    ssm: Any  # boto3 SSM client (GetParameter only)
    job_api: JobApi
    plan_api: PlanApi
    write_outcome: Callable[[str, bytes], None]  # (key, body) -> write-once object in the reports bucket
    budget_denied: Callable[[], bool]
    clock: Clock = field(default_factory=SystemClock)


def research_plan_ref_parameter(env: str) -> str:
    return contract_ssm.build(env, "financialplanning", "config", "research-plan-ref")


def idempotency_key(env: str, session_date: str, run_tag: str | None = None) -> str:
    return f"daily-{env}-{session_date}" + (f"-{run_tag}" if run_tag else "")


execution_name = idempotency_key


def outcome_key(session_date: str, name: str) -> str:
    return f"daily-trigger/outcomes/{session_date}/{name}.json"


def start_input(event: Mapping[str, Any], env: str) -> dict[str, Any]:
    """Initial state from the ingestion function's success destination event (or a manual/test start)."""
    detail = event.get("detail") if isinstance(event.get("detail"), Mapping) else None
    if detail is not None:
        resp = detail.get("responsePayload") or {}
        req = detail.get("requestPayload") or {}
        snap = resp.get("snapshot") or {}
        state = {
            "input_snapshot_id": resp.get("input_snapshot_id"),
            "dataset_id": (snap.get("dataset") or {}).get("dataset_id") or req.get("dataset_id"),
            "trigger": resp.get("trigger") or req.get("trigger"),
            "session_date": (resp.get("requested_range") or {}).get("end"),
            "ingestion_flags": list(resp.get("quality_flags") or snap.get("quality_flags") or []),
            "new_snapshot": bool(resp.get("new_snapshot")),
        }
    else:
        state = {k: event.get(k) for k in ("input_snapshot_id", "dataset_id", "trigger", "session_date", "run_tag")}
        state["ingestion_flags"] = list(event.get("ingestion_flags") or [])
        state["new_snapshot"] = bool(event.get("input_snapshot_id"))
    tag = state.get("run_tag") or event.get("run_tag")
    if tag is not None and not (isinstance(tag, str) and _RUN_TAG.match(tag)):
        raise ValueError("run_tag must be lowercase letters, digits and '-' (at most 32)")
    state["run_tag"] = tag
    state["env"] = env
    state["polls"] = 0
    return state


# ===================================================================== steps
def _done(state: dict[str, Any], outcome: str, **extra: Any) -> dict[str, Any]:
    assert outcome in OUTCOMES, outcome
    return {**state, **extra, "outcome": outcome}


def check_snapshot(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """DLY-01: only an approved scheduled universe snapshot continues; otherwise no FinanceModel call."""
    u = deps.config.universe
    if "no_session" in state.get("ingestion_flags", []) and not state.get("new_snapshot"):
        return _done(state, "no_session")
    sid = state.get("input_snapshot_id")
    if u is None or state.get("dataset_id") != u.dataset_id or not sid:
        return _done(state, "skipped_snapshot", reason="not_a_universe_snapshot")
    if state.get("trigger") not in ("scheduled", None) and not state.get("run_tag"):
        return _done(state, "skipped_snapshot", reason="not_scheduled")
    status, body = deps.plan_api.get_snapshot(sid)
    snap = body.get("snapshot") if status == 200 else None
    if not snap or snap.get("status") != "approved":
        return _done(state, "skipped_snapshot", reason="snapshot_not_approved", snapshot_status=(snap or {}).get("status"))
    session = state.get("session_date") or snap["coverage"]["end"]
    return {**state, "session_date": session, "bias_disclosures": snap.get("bias_disclosures") or []}


def read_strategy(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """DLY-02: absent, empty or strategy-less key -> ``skipped_no_strategy`` (before any FinanceModel call)."""
    name = contract_ssm.production_strategy_parameter(deps.config.env)
    try:
        value = deps.ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        if type(exc).__name__ in ("ParameterNotFound",) or "ParameterNotFound" in str(exc):
            return _done(state, "skipped_no_strategy")
        raise
    try:
        doc = json.loads(value) if value and value.strip() else {}
    except json.JSONDecodeError:
        doc = {}
    sid = doc.get("strategy_id") if isinstance(doc, Mapping) else None
    if not isinstance(sid, str) or not sid:
        return _done(state, "skipped_no_strategy")
    return {**state, "strategy_id": sid}


def check_budget(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """DLY-03: the budget deny action active -> ``skipped_budget`` without calling FinanceModel."""
    if deps.budget_denied():
        return _done(state, "skipped_budget", reason="budget_deny_active")
    return state


def submission(state: Mapping[str, Any], deps: TriggerDeps, plan_id: str) -> dict[str, Any]:
    u = deps.config.universe
    assert u is not None
    payload = {"strategy": state["strategy_id"], "objective": "daily_recommendation", "universe": [str(i["instrument_id"]) for i in u.instruments]}
    body = {
        "domain": "finance",
        "domain_schema_version": "1.0",
        "job_type": JOB_TYPE,
        "purpose": PURPOSE,
        "dry_run": False,
        "input_snapshot_id": state["input_snapshot_id"],
        "plan_id": plan_id,
        "configuration": {"domain": "finance", "domain_schema_version": "1.0", "payload": payload},
        "compute_class": str((deps.config.daily_trigger or {}).get("compute_class", "cpu")),
        "max_runtime_seconds": int((deps.config.daily_trigger or {}).get("max_runtime_seconds", 1800)),
        "idempotency_key": idempotency_key(deps.config.env, state["session_date"], state.get("run_tag")),
        "contract_version": load_store().version,
    }
    res = validate(body, "job-submission")
    if not res.valid:  # pragma: no cover - a platform bug
        raise ValueError(f"daily job submission failed contract validation: {[i.message for i in res.issues][:3]}")
    return body


def submit_job(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """DLY-04: one ``daily_recommendation`` job per session (idempotency key ``daily-<env>-<session_date>``)."""
    plan_id = deps.ssm.get_parameter(Name=research_plan_ref_parameter(deps.config.env))["Parameter"]["Value"].strip()
    body = submission(state, deps, plan_id)
    status, resp = deps.job_api.submit_job(body)
    if status >= 400:
        code = (resp.get("error") or resp).get("code") if isinstance(resp, Mapping) else None
        if code == "BUDGET_EXCEEDED":
            return _done(state, "skipped_budget", reason="budget_exceeded")
        raise RuntimeError(f"FinanceModel submit_job failed with HTTP {status} ({code})")
    return {**state, "plan_id": plan_id, "run_id": resp["run_id"], "job_state": resp.get("state"), "idempotency_key": body["idempotency_key"]}


def poll_job(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    status, resp = deps.job_api.get_job_status(state["run_id"])
    if status >= 400:
        raise RuntimeError(f"FinanceModel get_job_status failed with HTTP {status}")
    polls = int(state.get("polls", 0)) + 1
    job_state = resp.get("state")
    out = {**state, "polls": polls, "job_state": job_state, "completion_status": resp.get("completion_status"), "terminal": job_state in TERMINAL_JOB_STATES}
    if not out["terminal"] and polls >= int((deps.config.daily_trigger or {}).get("max_polls", 9)):
        return _done(out, "timed_out")
    if out["terminal"] and job_state != "succeeded":
        return _done(out, "run_failed", reason=f"job_{job_state}")
    return out


def accept(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """DLY-05: the existing staged-output acceptance -> an unpublished ``model_run`` version."""
    status, plan = deps.plan_api.get_plan(state["plan_id"])
    if status != 200:
        raise RuntimeError(f"plan read failed with HTTP {status}")
    revision = int(plan["plan"]["revision"])
    body = {"plan_id": state["plan_id"], "run_id": state["run_id"], "expected_revision": revision, "idempotency_key": f"accept-{state['run_id']}"}
    status, resp = deps.plan_api.accept(state["plan_id"], state["run_id"], body)
    if status >= 400:
        return _done(state, "run_failed", reason="acceptance_rejected", error_code=(resp.get("error") or resp).get("code"))
    kind = resp.get("outcome")
    if kind == "accepted":
        return _done(state, "pending_approval", plan_version_id=resp["plan_version_id"], plan_version_status=resp.get("status"))
    if kind == "no_version":
        return _done(state, "no_version")
    return _done(state, "run_failed", reason="run_outcome_rejected")


def write_outcome(state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    """The outcome record (``reports`` bucket, write-once per execution), read through ``GET /v1/daily-trigger/outcomes``."""
    outcome = state.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    session = state.get("session_date") or deps.clock.now().date().isoformat()
    name = execution_name(deps.config.env, session, state.get("run_tag"))
    keep = ("outcome", "reason", "session_date", "input_snapshot_id", "dataset_id", "strategy_id", "run_id", "plan_id", "plan_version_id", "plan_version_status", "job_state", "completion_status", "polls", "run_tag", "bias_disclosures", "snapshot_status", "error_code")
    record = {k: state[k] for k in keep if state.get(k) is not None}
    record.update({"environment": deps.config.env, "execution_name": name, "published": False, "recorded_at": to_timestamp(deps.clock.now())})
    deps.write_outcome(outcome_key(session, f"{name}-{deps.clock.now().strftime('%Y%m%dT%H%M%SZ')}"), json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return record


STEPS: dict[str, Callable[[dict[str, Any], TriggerDeps], dict[str, Any]]] = {
    "check_snapshot": check_snapshot,
    "read_strategy": read_strategy,
    "check_budget": check_budget,
    "submit_job": submit_job,
    "poll_job": poll_job,
    "accept": accept,
    "write_outcome": write_outcome,
}


def run_step(step: str, state: dict[str, Any], deps: TriggerDeps) -> dict[str, Any]:
    if step not in STEPS:
        raise ValueError(f"unknown step {step!r}")
    return STEPS[step](dict(state), deps)


def run_inline(state: dict[str, Any], deps: TriggerDeps, *, sleep: Callable[[float], None] = lambda s: None) -> dict[str, Any]:
    """The state machine's control flow in-process (unit tests and documentation of the ASL)."""
    for step in ("check_snapshot", "read_strategy", "check_budget", "submit_job"):
        state = run_step(step, state, deps)
        if "outcome" in state:
            return run_step("write_outcome", state, deps)
    interval = int((deps.config.daily_trigger or {}).get("poll_interval_seconds", 300))
    while True:
        sleep(interval)
        state = run_step("poll_job", state, deps)
        if "outcome" in state:
            return run_step("write_outcome", state, deps)
        if state.get("terminal"):
            break
    state = run_step("accept", state, deps)
    return run_step("write_outcome", state, deps)


# ===================================================================== reads (GET /v1/daily-trigger/outcomes/{session_date})
OUTCOMES_RESPONSE: dict[str, Any] = {
    "x-platform-name": "daily-trigger-outcomes-response",
    "type": "object",
    "properties": {
        "session_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"outcome": {"enum": list(OUTCOMES)}, "execution_name": {"type": "string"}, "published": {"const": False}},
                "required": ["outcome", "execution_name", "published"],
            },
        },
        "latest": {"type": ["object", "null"]},
    },
    "required": ["session_date", "outcomes", "latest"],
    "additionalProperties": False,
}
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def get_outcomes(ctx: Any, svc: Any, session_date: str) -> dict[str, Any]:
    """Every outcome record of one session date (oldest first) and the latest one."""
    from .errors import PlatformError

    if not isinstance(session_date, str) or not _DATE.match(session_date):
        raise PlatformError.validation("session_date must be YYYY-MM-DD", pointer="/session_date")
    keys = sorted(k for k, _ in svc.store.list_objects("reports", f"daily-trigger/outcomes/{session_date}/"))
    records = [json.loads(svc.store.get("reports", k)[0]) for k in keys]
    records.sort(key=lambda r: r.get("recorded_at", ""))
    return {"session_date": session_date, "outcomes": records, "latest": records[-1] if records else None}
