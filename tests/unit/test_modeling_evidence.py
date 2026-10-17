"""Exact inference evidence and permanent isolated-ledger guards; fake and real Dynamo syntax."""
import json

import pytest

from finplan_platform.core.artifacts import sha256_checksum
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.modeling_evidence import ModelingEvidence
from finplan_platform.core.repository import PutNew

AT = "2026-01-12T15:00:00Z"
LATER = "2026-01-12T16:00:00Z"


@pytest.fixture
def evidence(repo_any, artifacts):
    return ModelingEvidence(repo_any, artifacts)


def ready(e, *, approach="optimizer", slot=AT):
    book = e.ensure_portfolio("experiment-test", approach, 1000, ["SPY"], AT)
    payload = {"availability_cutoff": slot, "decision_time": slot, "numeric": {"feature_order": ["return", "risk"], "observation": [0.1, 0.2]}, "news": {"consumed": False, "items": []}, "holdings": book}
    source = e.capture_shared_source("experiment-test", slot, {"input_snapshot_id": "snap_01ARZ3NDEKTSV4RRFFQ69G5FAV", "prices": [{"symbol": "SPY", "price": 100, "available_at": slot}]}, slot)
    supplied = e.capture_input("experiment-test", approach, slot, payload, source, slot)
    run = e.start_run("experiment-test", approach, slot, supplied["input_id"], book["portfolio_id"], slot, {"family": "optimization"})
    return book, supplied, run


def test_exact_capture_is_permanent_and_not_shared_replacement(evidence):
    e = evidence
    book, supplied, run = ready(e)
    raw = e.read_input_bytes(supplied["input_id"])
    assert sha256_checksum(raw) == supplied["checksum"]
    assert json.loads(raw)["numeric"]["observation"] == [0.1, 0.2]
    assert json.loads(raw)["news"]["consumed"] is False
    changed = e.read_input(supplied["input_id"])["payload"]
    changed["numeric"]["observation"] = [99, 99]
    with pytest.raises(PlatformError, match="permanent experiment record"):
        e.capture_input("experiment-test", "optimizer", AT, changed, supplied["shared_source_ref"], AT)
    assert e.start_run("experiment-test", "optimizer", AT, supplied["input_id"], book["portfolio_id"], LATER)["run_id"] == run["run_id"]
    assert e.read_input_bytes(supplied["input_id"]) == raw


def test_future_news_and_credentials_rejected_before_persistence(evidence):
    with pytest.raises(PlatformError, match="future-available"):
        evidence.capture_input("test", "codex", AT, {"news": {"items": [{"headline": "Future", "platform_available_at": LATER}]}}, None, AT)
    with pytest.raises(PlatformError, match="credential"):
        evidence.capture_input("test", "codex", AT, {"prompt": {"api_key": "secret"}}, None, AT)


def test_failure_keeps_original_inputs_and_output_immutable(evidence):
    _, supplied, run = ready(evidence)
    outcome = {"status": "failed", "error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "policy artifact missing"}}
    finished = evidence.finish_run(run["run_id"], outcome, LATER)
    assert evidence.read_run(run["run_id"])["outcome"] == outcome
    assert evidence.finish_run(run["run_id"], outcome, LATER) == finished
    with pytest.raises(PlatformError, match="cannot change"):
        evidence.finish_run(run["run_id"], {"status": "succeeded"}, LATER)
    assert evidence.read_input(supplied["input_id"])["payload"]["numeric"]["observation"] == [0.1, 0.2]


def test_paper_commit_permanent_retry_and_competing_revision(evidence):
    e = evidence
    book, _, run = ready(e)
    _, _, competing = ready(e, slot="2026-01-12T15:01:00Z")
    e.finish_run(run["run_id"], {"status": "succeeded", "target_weights": {"SPY": 0.5}, "forecast": None}, AT)
    e.finish_run(competing["run_id"], {"status": "succeeded", "target_weights": {"SPY": 0.5}}, AT)
    state = {**book["paper_state"], "positions": [{"instrument_id": "SPY", "quantity": 5}], "cash_balance": 499.5, "source": "paper_rebalance"}
    execution = {"market_session_valid": True, "evaluator_version": "controlled-test", "quote_provenance": {"source": "controlled-test", "price": 100}, "execution_at": AT, "transaction_cost": 0.5, "before_value": 1000, "after_value": 999.5, "reference_prices": {"SPY": 100}, "transaction_cost_bps": 10}
    fills = [{"symbol": "SPY", "quantity": 5, "price": 100, "cost": 0.5, "side": "buy"}]
    result = e.commit_paper_step(run["run_id"], 1, state, fills, execution, AT)
    assert e.commit_paper_step(run["run_id"], 1, state, fills, execution, AT) == result
    assert e.get_portfolio(book["portfolio_id"])["revision"] == 2
    with pytest.raises(PlatformError, match="revision changed"):
        e.commit_paper_step(competing["run_id"], 1, state, fills, execution, LATER)
    assert e.repo.get("portfolio_history", f"{book['portfolio_id']}:00000000000000000002")
    assert e.repo.get("portfolio_decision", result["decision_id"])


def test_no_automatic_user_book_and_no_off_session_fills(evidence):
    e = evidence
    e.repo.commit([PutNew("portfolio", doc={"portfolio_id": "user-book", "synthetic": True, "settings": {}, "revision": 1})])
    with pytest.raises(PlatformError, match="isolated synthetic"):
        e.get_portfolio("user-book")
    book, _, run = ready(e)
    e.finish_run(run["run_id"], {"status": "succeeded", "target_weights": {"SPY": 1}}, AT)
    with pytest.raises(PlatformError, match="validated session"):
        e.commit_paper_step(run["run_id"], 1, book["paper_state"], [{"side": "buy"}], {}, AT)


def test_bounded_day_queries_token_scope_and_unique_rows(evidence):
    e = evidence
    book, _, run = ready(e)
    e.finish_run(run["run_id"], {"status": "hold", "reason": "market closed"}, AT)
    e.record_observation("experiment-test", "optimizer", AT, book["portfolio_id"], {"status": "stale", "equity": None}, AT)
    rows, token = e.query_day("experiment-test", "2026-01-12", kind="inputs", page_size=1)
    assert len(rows) == 1 and token
    rows2, _ = e.query_day("experiment-test", "2026-01-12", kind="inputs", page_size=1, next_token=token)
    assert rows[0]["input_id"] != rows2[0]["input_id"]
    with pytest.raises(PlatformError, match="scope"):
        e.query_day("experiment-test", "2026-01-13", kind="inputs", next_token=token)
    runs, _ = e.query_day("experiment-test", "2026-01-12")
    assert len(runs) == 1 and runs[0]["outcome"]["status"] == "hold"
    observations, _ = e.query_day("experiment-test", "2026-01-12", kind="observations")
    assert observations[0]["payload"]["equity"] is None


def test_unavailable_forecast_is_honest_and_forecast_target_frozen(evidence):
    _, _, run = ready(evidence)
    with pytest.raises(PlatformError, match="target timestamp"):
        evidence.finish_run(run["run_id"], {"status": "succeeded", "forecast": {"expected_value": 1005, "baseline_value": 1000, "units": "USD"}}, AT)
    evidence.finish_run(run["run_id"], {"status": "hold", "forecast": {"available": False, "reason": "allocation only"}}, AT)


def test_registry_preserves_distinct_approaches(evidence):
    e = evidence
    approaches = [{"approach_id": "ppo", "family": "reinforcement_learning"}, {"approach_id": "sac", "family": "reinforcement_learning"}, {"approach_id": "codex", "family": "language_model"}]
    e.register_approaches("experiment-test", approaches, {"starting_capital": 1000}, AT)
    assert e.read_registry("experiment-test")["approaches"] == approaches


def test_lease_nonoverlap_and_crash_recovery_reuses_exact_input(evidence):
    e = evidence
    _, supplied, run = ready(e)
    raw = e.read_input_bytes(supplied["input_id"])
    assert e.claim_run(run["run_id"], "worker-one", AT, lease_seconds=60)
    assert not e.claim_run(run["run_id"], "worker-two", AT, lease_seconds=60)
    assert e.claim_run(run["run_id"], "worker-two", LATER, lease_seconds=60)
    assert e.read_input_bytes(supplied["input_id"]) == raw
    e.finish_run(run["run_id"], {"status": "failed", "reason": "controlled crash recovery"}, LATER)
    assert not e.claim_run(run["run_id"], "worker-three", LATER)
    attempts = [a for a in e.repo.audit_events(run["run_id"]) if a.operation == "claim_model_invocation"]
    assert len(attempts) == 2


def test_automatic_receipts_roundtrip_existing_decision_and_history_contracts(evidence, ctx, env_config):
    from finplan_platform.core.contract_io import require_valid
    from finplan_platform.core.portfolio_lifecycle import get_decision, get_history
    from finplan_platform.core.services import Services
    e=evidence
    book, _, run=ready(e)
    e.finish_run(run["run_id"], {"status":"succeeded","target_weights":{"SPY":0.5},"cash_weight":0.5}, AT)
    execution={"reference_prices":{"SPY":100},"reference_date":"2026-01-12","transaction_cost_bps":1}
    result=e.commit_paper_step(run["run_id"],1,book["paper_state"],[],execution,AT)
    svc=Services(cfg=env_config,repo=e.repo,artifacts=e.store)
    decision=get_decision(ctx,svc,result["decision_id"])
    require_valid(decision,"api/portfolio-decision-response")
    require_valid(get_history(ctx,svc,book["portfolio_id"],1),"api/portfolio-history-entry-response")
    require_valid(get_history(ctx,svc,book["portfolio_id"],2),"api/portfolio-history-entry-response")
    assert decision["decision"]["resolution"]["confirmed_by_user"] is False
    assert decision["decision"]["resolution"]["automatic_experiment_policy"] is True
    # An ordinary user resolution cannot claim unconfirmed acceptance.
    ordinary=dict(decision["decision"]["resolution"])
    ordinary.pop("automatic_experiment_policy")
    with pytest.raises(PlatformError):require_valid(ordinary,"api/resolve-portfolio-decision-response")


def test_equivalent_timezone_slot_cannot_create_second_run(evidence):
    from finplan_platform.core.modeling_evidence import _id
    e=evidence
    book, supplied, run=ready(e)
    alias="2026-01-12T10:00:00-05:00"
    assert _id("mr","experiment-test","optimizer",alias)==run["run_id"]
    assert e.start_run("experiment-test","optimizer",alias,supplied["input_id"],book["portfolio_id"],LATER)["run_id"]==run["run_id"]


def test_unavailable_artifact_keeps_day_row_with_integrity_blocker(evidence, monkeypatch):
    e=evidence
    _,_,run=ready(e)
    e.finish_run(run["run_id"],{"status":"failed","reason":"test"},AT)
    original=e.store.get
    def missing(role,key,**kwargs):
        if "/outputs/" in key:raise PlatformError.not_found("controlled missing artifact")
        return original(role,key,**kwargs)
    monkeypatch.setattr(e.store,"get",missing)
    rows,_=e.query_day("experiment-test","2026-01-12")
    assert len(rows)==1 and rows[0]["run_id"]==run["run_id"]
    assert rows[0]["outcome"] is None and rows[0]["evidence_error"]["code"]=="NOT_FOUND"


def test_due_forecasts_survive_long_horizon_and_realization_is_permanent(evidence):
    e=evidence
    book,_,run=ready(e)
    target="2026-02-12T15:00:00Z"
    forecast={"status":"pending","available":True,"expected_value":1100,"baseline_value":1000,"units":"USD","target_at":target,"forecast_checksum":"sha256:controlled-forecast"}
    e.finish_run(run["run_id"],{"status":"hold","forecast":forecast},AT)
    assert e.query_pending_forecasts("experiment-test","2026-02-12T14:59:59Z")[0]==[]
    due,_=e.query_pending_forecasts("experiment-test","2026-02-12T15:00:00.001Z")
    assert [r["run_id"] for r in due]==[run["run_id"]]
    output_before=e.read_run(run["run_id"])["output_ref"]["checksum"]
    valuation=e.record_observation("experiment-test","optimizer",target,book["portfolio_id"],{"value":1080,"observed_at":target,"observation_kind":"valid","quote_status":"fresh","quote_provenance":{"source":"controlled_replay"}},target)
    payload={"run_id":run["run_id"],"forecast_checksum":"sha256:controlled-forecast","observation_id":valuation["observation_id"],"observed_at":target,"realized_value":1080,"signed_error":-20,"absolute_error":20,"squared_error":400}
    match=e.record_observation("experiment-test","optimizer",AT,book["portfolio_id"],payload,target,kind="forecast_match")
    ref=e.mark_forecast_realized(run["run_id"],match["observation_id"],target)
    assert e.mark_forecast_realized(run["run_id"],match["observation_id"],target)==ref
    assert e.query_pending_forecasts("experiment-test","2027-01-01T00:00:00Z")[0]==[]
    assert e.read_run(run["run_id"])["output_ref"]["checksum"]==output_before
    assert e.read_run(run["run_id"])["forecast_state"]=="realized"
    assert e.read_run(run["run_id"])["forecast_realization"]["realized_value"]==1080
    historical,_=e.query_observations_after("experiment-test","optimizer",target,"2026-03-01T00:00:00Z")
    assert len(historical)==2 and all(r["observed_at"]==target for r in historical)
