"""Deployed tests of change add-research-universe-and-daily-loop (tasks 4.1-4.4): DLY-07 (beta and gamma),
UNI-05 (beta, phase 2) and DLY-08 (beta, one real ``buy_and_hold`` job).

They run IN THE PIPELINE only (beta ``integration-beta`` and gamma ``gamma`` suites, started by
``scripts/stage_runner.py`` with the stage role) and are skipped everywhere else; the stage fails when
no test executes. Nothing here runs on a developer machine and the real provider is called only by
the deployed ingestion function.

* **DLY-07**: with the production-strategy key absent, an asynchronous scheduled universe ingestion
  (the same event the 09:00 schedule sends) ends in the trigger outcome ``skipped_no_strategy`` read
  through the deployed API; the record names no ``run_id`` (no job API request) and the research
  plan's version list is unchanged.
* **UNI-05** (phase 2 environments): a synchronous scheduled ingestion of both datasets; the universe
  snapshot has ``yfinance`` lineage, 5 tickers from 2010-10-01, ``approved`` under
  ``approval-v2-universe`` and both disclosures; the SPY ``etf-daily`` snapshot is still produced.
  The test prints the promotion evidence for ``config/phase2-evidence.json`` (UNI-06).
* **DLY-08**: needs FinanceModel's strategy selection (contracts 1.1.0 consumer); it runs when
  FinanceModel's release manifest in this environment declares contract 1.1.0 or later, and otherwise
  skips with that reason (the other tests still execute).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.smoke.transport import SigV4Transport, endpoint_parameter

ENV = os.environ.get("FINPLAN_TARGET_ENV")
SUITE = os.environ.get("FINPLAN_SUITE")
SUITES = {"beta": "integration-beta", "gamma": "gamma"}
pytestmark = pytest.mark.skipif(not ENV or SUITES.get(ENV) != SUITE, reason="deployed daily-loop tests run only in the pipeline's beta and gamma stages")

UNIVERSE_TICKERS = {"VOO", "GOOGL", "NFLX", "AAPL", "NVDA"}
OUTCOME_WAIT_SECONDS = 900


@pytest.fixture(scope="module")
def dep() -> dict[str, Any]:  # pragma: no cover - needs a deployment
    import boto3
    from botocore.config import Config
    from finplan_platform.core.config import load_config

    assert ENV is not None
    cfg = load_config(ENV)
    session = boto3.session.Session(region_name=cfg.region)
    ssm = session.client("ssm")
    endpoint = ssm.get_parameter(Name=endpoint_parameter(ENV))["Parameter"]["Value"]
    creds = session.get_credentials()
    assert creds is not None
    return {
        "cfg": cfg,
        "ssm": ssm,
        "lambda": session.client("lambda", config=Config(read_timeout=360, retries={"max_attempts": 0})),
        "sfn": session.client("stepfunctions"),
        "t": SigV4Transport(endpoint, cfg.region, creds.get_frozen_credentials(), correlation_prefix=f"cor_dl{ENV}"),
    }


def _ingestion_function(env: str) -> str:
    return f"finplan-{env}-financialplanning-ingestion-handler"


def _scheduler_event(dataset_id: str) -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"source": "finplan.scheduler", "trigger": "scheduled", "dataset_id": dataset_id, "scheduled_time": now, "execution_id": f"it-{int(time.time())}-{dataset_id.rsplit('/', 1)[-1]}"}


def _strategy_param(env: str) -> str:
    from finplan_contracts import ssm as contract_ssm

    return contract_ssm.production_strategy_parameter(env)


def _strategy_absent(ssm: Any, env: str) -> bool:
    try:
        value = ssm.get_parameter(Name=_strategy_param(env))["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return True
    return not value.strip() or not json.loads(value).get("strategy_id")


def _version_ids(t: Any, plan_id: str) -> list[str]:
    code, body, _ = t.call("GET", f"/v1/plans/{plan_id}/versions?page_size=100")
    assert code == 200, (code, body.get("code"))
    return [v["plan_version_id"] for v in body.get("versions", [])]


def _research_plan(ssm: Any, env: str) -> str:
    from finplan_platform.core.daily_trigger import research_plan_ref_parameter

    return ssm.get_parameter(Name=research_plan_ref_parameter(env))["Parameter"]["Value"].strip()


def _wait_outcome(t: Any, session_date: str, after: str, *, run_tag: str | None = None) -> dict[str, Any]:
    deadline = time.time() + OUTCOME_WAIT_SECONDS
    while time.time() < deadline:
        code, body, _ = t.call("GET", f"/v1/daily-trigger/outcomes/{session_date}")
        assert code == 200, (code, body.get("code"), body.get("message"))
        for rec in reversed(body["outcomes"]):
            if rec["recorded_at"] > after and (run_tag is None or rec.get("run_tag") == run_tag):
                return rec
        time.sleep(20)
    raise AssertionError(f"no trigger outcome for {session_date} after {after} within {OUTCOME_WAIT_SECONDS}s")


def _target_session(cfg: Any) -> tuple[str, bool]:
    from finplan_platform.core.calendar import calendar_for_provider

    cal = calendar_for_provider(cfg.provider)
    now = datetime.now(UTC)
    fire = cal.local_date(now)
    if not cal.is_session(fire):
        return fire.isoformat(), False
    return cal.latest_closed_session(now).day.isoformat(), True


# ------------------------------------------------------------------ DLY-07 (beta and gamma)
def test_dly07_no_strategy_means_nothing_runs(dep: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    cfg, ssm, t = dep["cfg"], dep["ssm"], dep["t"]
    assert cfg.universe is not None
    assert _strategy_absent(ssm, ENV), "DLY-07 needs the production-strategy key absent (phase 1 state; DLY-08 clears it)"
    plan_id = _research_plan(ssm, ENV)
    before = _version_ids(t, plan_id)
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = dep["lambda"].invoke(FunctionName=_ingestion_function(ENV), InvocationType="Event", Payload=json.dumps(_scheduler_event(cfg.universe.dataset_id)).encode())
    assert resp["StatusCode"] == 202
    session, is_session = _target_session(cfg)
    rec = _wait_outcome(t, session, started)
    if not is_session:
        assert rec["outcome"] == "no_session"
    else:
        assert rec["outcome"] == "skipped_no_strategy", rec
    assert "run_id" not in rec and rec["published"] is False  # no FinanceModel job request
    assert _version_ids(t, plan_id) == before


# ------------------------------------------------------------------ UNI-05 (beta, phase 2)
def test_uni05_universe_snapshot_from_yfinance(dep: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    cfg, t = dep["cfg"], dep["t"]
    if cfg.phase != 2:
        pytest.skip(f"{ENV} is phase 1: the universe is served by the fixture provider (UNI-05 runs once the environment declares phase 2)")
    out = {}
    for ds in (cfg.dataset_id, cfg.universe.dataset_id):
        r = dep["lambda"].invoke(FunctionName=_ingestion_function(ENV), InvocationType="RequestResponse", Payload=json.dumps(_scheduler_event(ds)).encode())
        body = json.loads(r["Payload"].read() or b"{}")
        assert "FunctionError" not in r, body
        out[ds] = body
    spy = out[cfg.dataset_id]
    if spy.get("quality_flags") == ["no_session"] and not spy.get("snapshot"):
        pytest.skip("not a trading day")
    assert spy["snapshot"]["dataset"]["dataset_id"] == "finance/etf-daily/SPY"
    u = out[cfg.universe.dataset_id]
    sid = u["input_snapshot_id"]
    code, body, _ = t.call("GET", f"/v1/snapshots/{sid}")
    assert code == 200, (code, body.get("code"))
    snap = body["snapshot"]
    assert snap["lineage"]["provider"] == "yfinance"
    assert snap["coverage"]["start"] == "2010-10-01"
    assert snap["status"] == "approved" and snap["approval_rule_version"] == "approval-v2-universe", (snap["status"], snap["quality_flags"], snap.get("quality_details", {}).get("partial_response"))
    assert {d["kind"] for d in snap["bias_disclosures"]} == {"hindsight_selection", "survivorship"}
    assert snap["observation_summary"]["instruments_expected"] == 5 == snap["observation_summary"]["instruments_complete"]
    code, obs, _ = t.call("GET", f"/v1/snapshots/{sid}/observations?page_size=1&start_date=2010-10-01&end_date=2010-10-01")
    assert code == 200 and {i["instrument_id"] for i in obs["instruments"]} == UNIVERSE_TICKERS
    evidence = {"input_snapshot_id": sid, "dataset_id": cfg.universe.dataset_id, "status": snap["status"], "approval_rule_version": snap["approval_rule_version"], "provider": "yfinance", "trigger": u.get("trigger")}
    print("PHASE2-EVIDENCE " + json.dumps({ENV: evidence}, sort_keys=True))


# ------------------------------------------------------------------ DLY-08 (beta, real job)
def _financemodel_contract(ssm: Any, env: str) -> str:
    from finplan_contracts import ssm as contract_ssm

    try:
        doc = json.loads(ssm.get_parameter(Name=contract_ssm.build(env, "financemodel", "release", "manifest"))["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return "0.0.0"
    return str(doc.get("contract_version", "0.0.0"))


def test_dly08_real_daily_job_is_unpublished_until_the_user_publishes(dep: dict[str, Any]) -> None:  # pragma: no cover - needs a deployment
    cfg, ssm, t = dep["cfg"], dep["ssm"], dep["t"]
    if ENV != "beta" and cfg.phase != 2:
        pytest.skip("DLY-08 runs in beta, and in gamma once gamma declares phase 2")
    fm = tuple(int(x) for x in _financemodel_contract(ssm, ENV).split(".")[:2])
    if fm < (1, 1):
        pytest.skip(f"FinanceModel in {ENV} serves contracts {'.'.join(map(str, fm))}: no strategy selection yet (needs 1.1.0)")
    import boto3
    from finplan_platform.handlers.daily_trigger import SigV4Json

    session = boto3.session.Session(region_name=cfg.region)
    from finplan_contracts import ssm as contract_ssm

    job = SigV4Json(ssm.get_parameter(Name=contract_ssm.build(ENV, "financemodel", "api", "job-endpoint"))["Parameter"]["Value"], cfg.region, session.get_credentials())
    plan_id = _research_plan(ssm, ENV)
    # 1. select buy_and_hold through FinanceModel's selection operation
    code, sel = job.call("PUT", "/v1/production-strategy", {"action": "set", "strategy_id": "buy_and_hold", "confirmed_by_user": True, "idempotency_key": f"it-select-{int(time.time())}"})
    if code == 400 and ((sel.get("details") or {}).get("rule") == "no_evaluation_evidence" or "evidence" in str(sel)):
        # Selecting a strategy needs a succeeded universe benchmark in this environment; those run only
        # when the user starts an experiment (never on a schedule), so a fresh environment has none yet.
        pytest.skip("no universe benchmark has been run in this environment yet (user-started experiments only)")
    assert code in (200, 201), (code, sel)
    try:
        # 2. start the trigger with a run tag (one job per suite)
        build = (os.environ.get("CODEBUILD_BUILD_ID") or "manual").rsplit(":", 1)[-1][:8].lower()
        run_tag = f"it-{build}"
        session_date, is_session = _target_session(cfg)
        if not is_session:
            pytest.skip("not a trading day")
        code, latest, _ = t.call("GET", f"/v1/daily-trigger/outcomes/{session_date}")
        sid = next((r.get("input_snapshot_id") for r in reversed(latest.get("outcomes", [])) if r.get("input_snapshot_id")), None)
        assert sid, "no universe snapshot recorded for this session (run UNI-05 / DLY-07 first)"
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        arn = f"arn:aws:states:{cfg.region}:{session.client('sts').get_caller_identity()['Account']}:stateMachine:finplan-{ENV}-financialplanning-daily-trigger"
        dep["sfn"].start_execution(stateMachineArn=arn, name=f"daily-{ENV}-{session_date}-{run_tag}", input=json.dumps({"input_snapshot_id": sid, "dataset_id": cfg.universe.dataset_id, "trigger": "scheduled", "session_date": session_date, "run_tag": run_tag}))
        rec = _wait_outcome(t, session_date, started, run_tag=run_tag)
        # 3. one run, a validated model_run version with disclosures, no publication
        assert rec["outcome"] == "pending_approval", rec
        assert rec["run_id"].startswith("run_") and rec["plan_version_status"] == "validated"
        code, pv, _ = t.call("GET", f"/v1/plan-versions/{rec['plan_version_id']}")
        assert code == 200 and pv["plan_version"]["origin"] == "model_run"
        code, plan, _ = t.call("GET", f"/v1/plans/{plan_id}")
        assert plan["plan"].get("current_publication_id") != rec["plan_version_id"]
        # 4. the user's approval path: the operator publishes through the existing route
        code, pub, _ = t.call("POST", f"/v1/plans/{plan_id}/publications", {"plan_version_id": rec["plan_version_id"], "expected_publication_revision": plan["plan"].get("publication_revision", 0), "idempotency_key": f"it-publish-{run_tag}"})
        assert code in (200, 201), (code, pub.get("code"), pub.get("message"))
        assert pub["plan_version_id"] == rec["plan_version_id"]
    finally:
        # 5. clear the key; the trigger is a no-op again
        job.call("PUT", "/v1/production-strategy", {"action": "clear", "confirmed_by_user": True, "idempotency_key": f"it-clear-{int(time.time())}"})
    assert _strategy_absent(ssm, ENV)
