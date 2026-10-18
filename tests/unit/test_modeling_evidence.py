"""Exact inference evidence and permanent isolated-ledger guards; fake and real Dynamo syntax."""
import copy
import json
from dataclasses import replace

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
    if e.read_registry("experiment-test") is None:
        e.register_approaches("experiment-test", [{"approach_id": approach, "family": "optimization"}], {"first_scheduled_slot": AT, "cadence_seconds": 3600}, AT)
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
    assert e.get_portfolio(book["portfolio_id"])["created_at"] == book["created_at"]
    with pytest.raises(PlatformError, match="revision changed"):
        e.commit_paper_step(competing["run_id"], 1, state, fills, execution, LATER)
    assert e.repo.get("portfolio_history", f"{book['portfolio_id']}:00000000000000000002")
    assert e.repo.get("portfolio_decision", result["decision_id"])


def test_book_creation_metadata_read_is_authoritative_without_writes_or_extra_reads(evidence, monkeypatch):
    book = evidence.ensure_portfolio("creation-bound", "cash", 1000, ["SPY"], AT)
    record = evidence.repo.require("portfolio", book["portfolio_id"])
    original_doc, original_attrs = copy.deepcopy(record.doc), copy.deepcopy(record.attrs)
    require = evidence.repo.require
    calls = []
    def read(logical, record_id):
        calls.append((logical, record_id))
        return require(logical, record_id)
    def no_write(*args, **kwargs):
        raise AssertionError("Portfolio metadata read must not publish a mutation")
    monkeypatch.setattr(evidence.repo, "require", read)
    monkeypatch.setattr(evidence.repo, "commit", no_write)
    loaded = evidence.get_portfolio(book["portfolio_id"])
    assert loaded["created_at"] == original_doc["created_at"]
    assert calls == [("portfolio", book["portfolio_id"])]
    assert record.doc == original_doc and record.attrs == original_attrs
    loaded["paper_state"]["as_of"] = "2026-02-01"
    assert evidence.get_portfolio(book["portfolio_id"])["created_at"] == original_doc["created_at"]


def test_book_settings_cannot_override_original_creation_metadata(evidence, monkeypatch):
    book = evidence.ensure_portfolio("creation-setting", "cash", 1000, ["SPY"], AT)
    record = evidence.repo.require("portfolio", book["portfolio_id"])
    doc = copy.deepcopy(record.doc)
    doc["settings"]["created_at"] = LATER
    shadow = replace(record, doc=doc)
    monkeypatch.setattr(evidence.repo, "require", lambda *args: shadow)
    assert evidence.get_portfolio(book["portfolio_id"])["created_at"] == record.doc["created_at"]


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


def test_due_forecasts_survive_long_horizon_and_missing_slots_stay_pending(evidence):
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
    # A month-later mark alone cannot prove all earlier scheduled opportunities
    # were accounted for. Preserve this due forecast until strong coverage exists.
    with pytest.raises(PlatformError, match="first-valid strong"):
        e.mark_forecast_realized(run["run_id"],match["observation_id"],target)
    assert [r["run_id"] for r in e.query_pending_forecasts("experiment-test","2027-01-01T00:00:00Z")[0]]==[run["run_id"]]
    assert e.read_run(run["run_id"])["output_ref"]["checksum"]==output_before
    assert e.read_run(run["run_id"])["forecast_state"]=="pending"
    historical,_=e.query_observations_after("experiment-test","optimizer",target,"2026-03-01T00:00:00Z")
    assert len(historical)==2 and all(r["observed_at"]==target for r in historical)


def test_execution_events_account_actual_day_and_retry_never_duplicates(evidence):
    """A prior-day policy's costs belong to execution day, never the issue day."""
    e=evidence
    book,_,run=ready(e)
    e.finish_run(run["run_id"],{"status":"succeeded","target_weights":{"SPY":.5},"cash_weight":.5},AT)
    execution_at="2026-01-13T15:00:00Z"
    recorded="2026-01-13T15:00:01Z"
    state={**book["paper_state"],"positions":[{"instrument_id":"SPY","quantity":5}],"cash_balance":499.5,"as_of":"2026-01-13","source":"paper_rebalance"}
    execution={"market_session_valid":True,"evaluator_version":"CONTROLLED_REPLAY","quote_provenance":{"source":"CONTROLLED_REPLAY"},"execution_at":execution_at,"reference_prices":{"SPY":100},"reference_date":"2026-01-13","transaction_cost_bps":10,"before_value":1000,"after_value":999.5,"transaction_cost":.5,"turnover":.5}
    fills=[{"instrument_id":"SPY","quantity":5,"price":100,"fee":.5,"spread_cost":0,"slippage_cost":0}]
    receipt=e.commit_paper_step(run["run_id"],1,state,fills,execution,recorded)
    assert e.commit_paper_step(run["run_id"],1,state,fills,execution,recorded)==receipt
    issue_runs,_=e.query_day("experiment-test","2026-01-12")
    issue_events,_=e.query_day("experiment-test","2026-01-12",kind="observations")
    execution_runs,_=e.query_day("experiment-test","2026-01-13")
    execution_events,_=e.query_day("experiment-test","2026-01-13",kind="observations")
    assert len(issue_runs)==1 and not issue_events and not execution_runs
    assert len(execution_events)==1
    event=execution_events[0]
    assert event["kind"]=="paper_execution" and event["decision_slot"]==AT
    assert event["run_id"]==run["run_id"] and event["decision_id"]==receipt["decision_id"]
    assert event["execution_ref"]["checksum"]==issue_runs[0]["execution_ref"]["checksum"]
    assert event["payload"]["transaction_cost"]==.5
    assert event["payload"]["simulated_fills"][0]["side"]=="buy"
    assert event["payload"]["execution"]["turnover"]==.5
    assert event["observed_at"]==execution_at and event["recorded_at"]==recorded
    assert event["portfolio_revision"]==2


def native_ready(e, *, mutate=None, trigger="controlled_replay"):
    """Controlled native-hourly source, never a claimed live market snapshot."""
    from datetime import datetime, timedelta, timezone
    end=datetime(2026,1,17,12,tzinfo=timezone.utc)
    stamp=lambda dt:dt.isoformat().replace('+00:00','Z')
    universe=["BTC-USD","ETH-USD"]
    native={"dataset_id":"coinbase-crypto-hourly-v1","dataset_checksum":"sha256:"+"abcd"*16,
            "as_of":stamp(end),"available_at":stamp(end+timedelta(minutes=5)),"granularity_seconds":3600,
            "products":universe,"market_data_synthetic":True,"sources":[],"contract_version":"1.7.0",
            "bars":{p:[{"start":stamp(end-timedelta(hours=24-i)),"open":100.,"close":100.,"low":99.,"high":101.,"volume":100000.} for i in range(24)] for p in universe}}
    source={"native_crypto_dataset":native,"investable_universe":universe,"checksum":native["dataset_checksum"],
            "availability_cutoff":"2026-01-17T12:05:00Z","trigger":trigger}
    if mutate:mutate(source)
    at=source["availability_cutoff"]
    book=e.ensure_portfolio("native-test","crypto_rule",1000.,universe,at)
    e.register_approaches("native-test", [{"approach_id": "crypto_rule", "family": "optimization"}], {"first_scheduled_slot": stamp(end), "cadence_seconds": 3600}, at)
    shared=e.capture_shared_source("native-test",stamp(end),source,at)
    supplied=e.capture_input("native-test","crypto_rule",stamp(end),{"availability_cutoff":at,"holdings":book,"numeric":{"prepared":{"native_source":native}},"news":{"consumed":False}},shared,at)
    run=e.start_run("native-test","crypto_rule",stamp(end),supplied["input_id"],book["portfolio_id"],at,{"family":"optimization"})
    e.finish_run(run["run_id"],{"status":"succeeded","target_weights":{"BTC-USD":.5,"ETH-USD":0.},"cash_weight":.5},at)
    state={**book["paper_state"],"positions":[{"instrument_id":"BTC-USD","quantity":5.}],"cash_balance":499.5,"source":"paper_rebalance"}
    execution={"market_session_valid":True,"evaluator_version":"controlled-native-common-kernel","quote_provenance":{"BTC-USD":{"frequency":"1h","source":"controlled_fixture"}},"execution_at":"2026-01-17T13:05:00Z","transaction_cost":.5,"before_value":1000.,"after_value":999.5,"reference_prices":{"BTC-USD":100.,"ETH-USD":100.},"reference_date":"2026-01-17","transaction_cost_bps":10.}
    return book,shared,supplied,run,state,execution


def test_native_input_identity_public_ledger_roundtrip_without_fabricated_snapshot(evidence):
    from finplan_platform.core.config import load_config
    from finplan_platform.core.services import Services
    from finplan_platform.core.portfolio_lifecycle import get_decision,get_history
    from finplan_platform.core.contract_io import require_valid
    e=evidence;book,shared,supplied,run,state,execution=native_ready(e)
    fills=[{"instrument_id":"BTC-USD","quantity":5.,"price":100.,"cost":.5}]
    receipt=e.commit_paper_step(run["run_id"],1,state,fills,execution,"2026-01-17T13:05:00Z")
    assert receipt["input_snapshot_id"] is None
    expected={"input_id":shared["input_id"],"checksum":shared["checksum"],"source_type":"platform_crypto_hourly",
              "dataset_id":"coinbase-crypto-hourly-v1","dataset_checksum":"sha256:"+"abcd"*16,
              "as_of":"2026-01-17T12:00:00Z","available_at":"2026-01-17T12:05:00Z","granularity_seconds":3600,
              "universe":["BTC-USD","ETH-USD"],"market_data_synthetic":True}
    assert receipt["native_input_ref"]==expected
    assert e.commit_paper_step(run["run_id"],1,state,fills,execution,"2026-01-17T14:05:00Z")==receipt
    assert e.get_portfolio(book["portfolio_id"])["revision"]==2
    svc=Services(cfg=load_config("beta"),repo=e.repo,artifacts=e.store);ctx=e._ctx("2026-01-17T13:05:00Z")
    decision=get_decision(ctx,svc,receipt["decision_id"]);history=get_history(ctx,svc,book["portfolio_id"],2)
    require_valid(decision,"api/portfolio-decision-response");require_valid(history,"api/portfolio-history-entry-response")
    assert decision["decision"]["native_input_ref"]==history["history"]["native_input_ref"]==expected
    assert decision["decision"]["input_snapshot_id"] is history["history"]["input_snapshot_id"] is None
    assert e.read_input(shared["input_id"])["metadata"]["checksum"]==expected["checksum"]
    assert e.read_run(run["run_id"])["execution"]["native_input_ref"]==expected


@pytest.mark.parametrize("defect",["universe","frequency","gap","ohlc","checksum","stale","synthetic_forward","provider"])
def test_native_input_identity_rejects_invalid_provenance_without_changing_book(evidence,defect):
    def mutate(source):
        native=source["native_crypto_dataset"]
        if defect=="universe":source["investable_universe"]=["SPY"]
        if defect=="frequency":native["granularity_seconds"]=60
        if defect=="gap":native["bars"]["BTC-USD"].pop(1)
        if defect=="ohlc":native["bars"]["BTC-USD"][0]["high"]=1.
        if defect=="checksum":source["checksum"]="sha256:"+"bcde"*16
        if defect=="stale":source["availability_cutoff"]="2026-01-17T12:26:00Z"
        if defect in ("synthetic_forward","provider"):source["trigger"]="scheduled_hourly"
        if defect=="provider":native["market_data_synthetic"]=False
    book,shared,supplied,run,state,execution=native_ready(evidence,mutate=mutate)
    with pytest.raises(PlatformError):evidence.commit_paper_step(run["run_id"],1,state,[{"instrument_id":"BTC-USD","quantity":5.,"price":100.,"cost":.5}],execution,"2026-01-17T13:05:00Z")
    assert evidence.get_portfolio(book["portfolio_id"])["revision"]==1
    assert not evidence.read_run(run["run_id"]).get("execution_ref")


def test_native_input_identity_accepts_verified_provider_form_without_relabeling_it_a_snapshot(evidence):
    def actual_provider_form(source):
        source["native_crypto_dataset"]["market_data_synthetic"]=False
        source["native_crypto_dataset"]["sources"]=[{"product":p,"provider":"coinbase_exchange_public","response_checksum":"sha256:"+"bcde"*16,"frozen_source_checksum":"sha256:"+"cdef"*16} for p in ("BTC-USD","ETH-USD")]
    book,shared,supplied,run,state,execution=native_ready(evidence,mutate=actual_provider_form,trigger="scheduled_hourly")
    result=evidence.commit_paper_step(run["run_id"],1,book["paper_state"],[],{**execution,"transaction_cost":0.,"after_value":1000.},"2026-01-17T13:05:00Z")
    assert result["native_input_ref"]["market_data_synthetic"] is False
    assert result["input_snapshot_id"] is None


def test_user_facing_decision_request_does_not_accept_native_identity_or_null_snapshot():
    from pathlib import Path
    import copy
    from finplan_platform.core.contract_io import require_valid
    original=json.loads((Path(__file__).parents[2]/"contracts/fixtures/api/create-portfolio-decision-request/valid/synthetic-lifecycle.json").read_text())
    require_valid(original,"api/create-portfolio-decision-request")
    altered=copy.deepcopy(original);altered["input_snapshot_id"]=None
    with pytest.raises(PlatformError):require_valid(altered,"api/create-portfolio-decision-request")
    altered=copy.deepcopy(original);altered["native_input_ref"]={"source_type":"platform_crypto_hourly"}
    with pytest.raises(PlatformError):require_valid(altered,"api/create-portfolio-decision-request")


def test_noop_cannot_mint_cash_or_positions(evidence):
    book,supplied,run=ready(evidence)
    evidence.finish_run(run["run_id"],{"status":"hold","target_weights":{"SPY":0.},"cash_weight":1.},AT)
    for state in ({**book["paper_state"],"cash_balance":1001.},
                  {**book["paper_state"],"positions":[{"instrument_id":"SPY","quantity":1.}]}):
        with pytest.raises(PlatformError,match="without fills"):
            evidence.commit_paper_step(run["run_id"],1,state,[],{},LATER)
    assert evidence.get_portfolio(book["portfolio_id"])["revision"]==1


@pytest.mark.parametrize("value_as_of,eligible",[("2026-01-12T21:00:00Z",False),("2026-01-12T21:30:00Z",True),("2026-01-12T22:00:00Z",True),("2026-01-12T22:06:00Z",False)])
def test_forecast_horizon_uses_actual_valuation_time_not_late_eod_report_time(evidence,value_as_of,eligible):
    e=evidence;book,_,run=ready(e)
    target="2026-01-12T21:30:00Z";observed="2026-01-12T22:05:00Z"
    forecast={"status":"pending","available":True,"expected_value":1100.,"baseline_value":1000.,"units":"USD","target_at":target,"forecast_checksum":"sha256:controlled-close-forecast"}
    e.finish_run(run["run_id"],{"status":"hold","forecast":forecast},AT)
    original=e.read_run(run["run_id"])["output_ref"]["checksum"]
    # Every preceding declared slot has an immutable explicit missing mark;
    # the horizon can align with the actual22:05 observation, never report time.
    for hour in range(15,23):
        sid=f"2026-01-12T{hour}:00:00Z"
        if hour>15:
            _,_,other=ready(e,slot=sid)
            e.finish_run(other["run_id"],{"status":"hold","forecast":{"available":False}},sid)
        if hour<22:
            e.record_observation("experiment-test","optimizer",sid,book["portfolio_id"],{"observed_at":sid,"value":None,"observation_kind":"missing","quote_status":"missing"},sid)
    valuation=e.record_observation("experiment-test","optimizer","2026-01-12T22:00:00Z",book["portfolio_id"],{"value":1080.,"observed_at":observed,"value_as_of":value_as_of,"observation_kind":"valid","quote_status":"fresh","quote_provenance":{"source":"controlled_replay","quote_status":"fresh_close"}},observed)
    matched={"run_id":run["run_id"],"forecast_checksum":forecast["forecast_checksum"],"observation_id":valuation["observation_id"],"observed_at":observed,"realized_value":1080.,"signed_error":-20.,"absolute_error":20.,"squared_error":400.}
    candidate=e.record_observation("experiment-test","optimizer",AT,book["portfolio_id"],matched,observed,kind="forecast_match")
    if eligible:
        e.mark_forecast_realized(run["run_id"],candidate["observation_id"],observed)
        assert e.read_run(run["run_id"])["forecast_state"]=="realized"
    else:
        with pytest.raises(PlatformError,match="post-horizon"):
            e.mark_forecast_realized(run["run_id"],candidate["observation_id"],observed)
        assert e.read_run(run["run_id"])["forecast_state"]=="pending"
        assert e.query_pending_forecasts("experiment-test",observed)[0]
    assert e.read_run(run["run_id"])["output_ref"]["checksum"]==original
