"""The shared ingestion operation (tasks 6.3-6.9, 6.13-6.15, 9.4).

ING-01 (one operation, two triggers), ING-03 (holiday), ING-04 (observation kinds, frozen
clock), ING-05 (capabilities, throttling), ING-06 (normalization and validation), ING-07
(dedupe), ING-08 (immutable snapshot results), ING-09 (idempotent triggers), ING-10/ING-13
(fixture ETF series), ING-11 (separate datasets), ING-14 (yfinance normalization), ING-16
(lineage), ING-17 (throttled then success / exhausted), ING-18 (empty and partial responses),
COST-05 (budget pre-check). Fixture provider, programmable mock and a mocked yfinance library
only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_contracts.validate import validate
from finplan_platform.core.artifacts import ArtifactExists, key_snapshot_manifest, key_snapshot_payload
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.errors import PlatformError
from finplan_platform.core.ingestion import ingest, run_ingestion, scheduled_idempotency_key
from finplan_platform.providers.yfinance_provider import YFinanceProvider

from .conftest import config_with, on_demand

SCHED = {"dataset_id": "finance/etf-daily/SPY", "scheduled_time": "2026-01-12T14:00:00Z"}  # Monday 09:00 ET


def _snapshots(deps: Any) -> list[Any]:
    return list(deps.repo.scan("snapshot_catalog"))


def _curated(deps: Any, prefix: str = "") -> list[str]:
    return [k for k, _ in deps.store.list_objects("curated", prefix)]


def _manifest(deps: Any, sid: str) -> dict[str, Any]:
    body, _ = deps.store.get("snapshots", key_snapshot_manifest(sid))
    return json.loads(body)


def _payload(deps: Any, sid: str) -> dict[str, Any]:
    body, _ = deps.store.get("snapshots", key_snapshot_payload(sid, "observations.json"))
    return json.loads(body)


# ------------------------------------------------------------------ ING-01
def test_scheduled_and_on_demand_share_one_operation_and_normalized_content(make_deps: Any, sctx: Any, octx: Any, at: Any) -> None:
    deps = make_deps()
    at("2026-01-12T14:00:00Z")
    sched = run_ingestion(sctx(), SCHED, deps=deps)
    demand = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09"), deps=deps)
    for res in (sched, demand):
        assert validate(res, "tools/refresh-market-data-response").valid, validate(res, "tools/refresh-market-data-response").issues
    assert sched["input_snapshot_id"] != demand["input_snapshot_id"]
    ms, md = _manifest(deps, sched["input_snapshot_id"]), _manifest(deps, demand["input_snapshot_id"])
    assert [o["curated_checksum"] for o in ms["observations"]] == [o["curated_checksum"] for o in md["observations"]]
    assert sched["content_checksum"] == demand["content_checksum"]
    assert len(_curated(deps)) == 1  # the second trigger found the identical curated observation
    rows = {r.id: r.attrs for r in _snapshots(deps)}
    assert rows[sched["input_snapshot_id"]]["trigger"] == "scheduled" and rows[demand["input_snapshot_id"]]["trigger"] == "on_demand"
    assert rows[demand["input_snapshot_id"]]["caller_principal"].endswith("financialplanning-operator")


# ------------------------------------------------------------------ ING-04 / ING-13
def test_morning_scheduled_run_ingests_previous_completed_session_only(make_deps: Any, sctx: Any, at: Any) -> None:
    deps = make_deps()
    at("2026-01-12T14:00:00Z")  # 09:00 ET on a trading Monday
    res = run_ingestion(sctx(), SCHED, deps=deps)
    obs = _payload(deps, res["input_snapshot_id"])["observations"]
    assert [(o["session_date"], o["kind"]) for o in obs] == [("2026-01-09", "completed_daily")]
    assert all(o["synthetic"] is True and o["instrument_id"] == "SPY" for o in obs)
    snap = res["snapshot"]
    assert snap["dataset"]["dataset_id"] == "finance/etf-daily/SPY" and snap["synthetic"] is True
    assert snap["lineage"]["provider"] == "fixture" and snap["status"] == "approved"


def test_early_close_session_completes_at_the_early_close(make_deps: Any, octx: Any, at: Any, fixcal: Any) -> None:
    deps = make_deps()
    # synthetic fixture calendar: 2026-11-27 (day after the 4th Thursday) closes at 13:00 ET
    assert fixcal.status(__import__("datetime").date(2026, 11, 27)) == "early_close"
    at("2026-11-27T17:30:00Z")  # 12:30 ET, before the early close
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-11-27", "2026-11-27", "early-1"), deps=deps)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "session_not_completed"
    at("2026-11-27T18:30:00Z")  # 13:30 ET, after the early close (16:00 has not passed)
    res = run_ingestion(octx(), on_demand("2026-11-27", "2026-11-27", "early-2"), deps=deps)
    obs = _payload(deps, res["input_snapshot_id"])["observations"]
    assert obs[0]["kind"] == "completed_daily" and obs[0]["session_status"] == "early_close"


def test_intraday_capable_mock_marks_current_session_partial(make_deps: Any, mock_provider: Any, octx: Any, at: Any) -> None:
    at("2026-01-12T16:00:00Z")  # 11:00 ET during regular hours
    deps = make_deps(provider=mock_provider(intraday=True))
    res = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-12", "intra-1", granularity="intraday"), deps=deps)
    obs = {o["session_date"]: o["kind"] for o in _payload(deps, res["input_snapshot_id"])["observations"]}
    assert obs == {"2026-01-09": "completed_daily", "2026-01-12": "intraday_partial"}
    assert "contains_intraday_partial" in res["snapshot"]["quality_flags"]


def test_settle_delay_finality_inferred_for_a_provider_without_finality(make_deps: Any, mock_provider: Any, octx: Any, at: Any) -> None:
    deps = make_deps(provider=mock_provider(final=None))  # beta settle delay: 60 min
    at("2026-01-09T21:30:00Z")  # 16:30 ET Friday: closed, but inside the settle delay
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "settle-1"), deps=deps)
    assert ei.value.details["reason"] == "session_not_completed" and ei.value.details["completed_after"] == "2026-01-09T22:00:00Z"
    res = run_ingestion(octx(), on_demand("2026-01-08", "2026-01-09", "settle-2"), deps=deps)
    obs = _payload(deps, res["input_snapshot_id"])["observations"]
    assert [o["session_date"] for o in obs] == ["2026-01-08"]  # 01-09 excluded: not marked completed_daily
    assert "finality_inferred" in res["snapshot"]["quality_flags"]
    assert res["snapshot"]["quality_details"]["excluded"] == [{"session_date": "2026-01-09", "reason": "awaiting_settle_delay"}]


# ------------------------------------------------------------------ ING-03
def test_holiday_schedule_makes_no_provider_call_and_references_latest(make_deps: Any, sctx: Any, at: Any) -> None:
    deps = make_deps()
    at("2026-01-12T14:00:00Z")
    first = run_ingestion(sctx(), SCHED, deps=deps)
    calls = len(deps.provider.calls)
    at("2026-01-01T14:00:00Z")  # synthetic fixture calendar holiday (Thursday)
    res = run_ingestion(sctx(), {"scheduled_time": "2026-01-01T14:00:00Z"}, deps=deps)
    assert len(deps.provider.calls) == calls
    assert res["quality_flags"] == ["no_session"] and res["new_snapshot"] is False
    assert res["input_snapshot_id"] == first["input_snapshot_id"] and len(_snapshots(deps)) == 1


def test_holiday_with_no_prior_snapshot_returns_null_id(make_deps: Any, sctx: Any, at: Any) -> None:
    deps = make_deps()
    at("2026-12-25T14:00:00Z")
    res = run_ingestion(sctx(), {"scheduled_time": "2026-12-25T14:00:00Z"}, deps=deps)
    assert res["input_snapshot_id"] is None and res["quality_flags"] == ["no_session"] and "snapshot" not in res
    assert deps.provider.calls == []


def test_out_of_coverage_request_is_precondition_failed(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2024-06-03", "2024-06-07"), deps=deps)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "calendar_coverage"
    assert ei.value.details["calendar_coverage"]["start"] == "2025-01-01"
    assert deps.provider.calls == [] and _snapshots(deps) == []


# ------------------------------------------------------------------ ING-05
def test_intraday_request_to_daily_only_provider_is_refused(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", granularity="intraday"), deps=deps)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "provider_capability_unsupported"
    assert deps.provider.calls == [] and _snapshots(deps) == []


def test_throttled_provider_is_rate_limited_retryable_without_snapshot(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(script=["throttle"]))
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    assert ei.value.code == "RATE_LIMITED" and ei.value.retryable is True
    assert _snapshots(deps) == [] and list(deps.store.list_objects("snapshots")) == []
    # nothing was recorded under the key: the retry with the same key succeeds
    assert run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)["new_snapshot"] is True


# ------------------------------------------------------------------ ING-06
def test_missing_session_is_flagged_and_excluded_from_coverage(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(drop_dates=["2026-01-09"]))
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    snap = res["snapshot"]
    assert {"missing_sessions", "partial_response"} <= set(snap["quality_flags"])
    assert snap["quality_details"]["missing_sessions"] == ["2026-01-09"]
    assert snap["coverage"] == {"start": "2026-01-05", "end": "2026-01-08"}
    assert res["coverage_complete"] is False


def test_gap_inside_the_range_is_listed(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(drop_dates=["2026-01-07"]))
    snap = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)["snapshot"]
    assert snap["quality_details"]["missing_sessions"] == ["2026-01-07"]
    assert "stale_source" not in snap["quality_flags"]


def test_inconsistent_bar_rejected_counted_and_retained_in_raw(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(overrides={"2026-01-07": {"high": 50.0, "low": 60.0}}))
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    snap = res["snapshot"]
    assert "rejected_records" in snap["quality_flags"]
    assert snap["quality_details"]["rejected_records"]["count"] == 1
    assert "2026-01-07" not in {o["session_date"] for o in _payload(deps, res["input_snapshot_id"])["observations"]}
    rejected = [k for k, _ in deps.store.list_objects("raw") if k.endswith("-rejected.json")]
    assert len(rejected) == 1
    body = json.loads(deps.store.get("raw", rejected[0])[0])
    assert body["records"][0]["record"]["session_date"] == "2026-01-07" and "ohlc_high_below_low" in body["records"][0]["reasons"]
    assert body["synthetic"] is True
    # the raw provider response was persisted too (before parsing)
    assert any(not k.endswith("-rejected.json") for k, _ in deps.store.list_objects("raw"))


def test_negative_values_and_calendar_misalignment_are_rejected(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    deps = make_deps(provider=mock_provider(overrides={"2026-01-06": {"volume": -5}, "2026-01-08": {"close": -1.0}}))
    snap = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)["snapshot"]
    reasons = snap["quality_details"]["rejected_records"]["reasons"]
    assert reasons.get("negative_volume") == 1 and reasons.get("negative_close") == 1


# ------------------------------------------------------------------ ING-07
def test_reingesting_same_session_keeps_one_curated_object(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    a = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "a"), deps=deps)
    b = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "b"), deps=deps)
    assert len(_curated(deps)) == 1
    assert _manifest(deps, b["input_snapshot_id"])["observations"][0]["curated_checksum"] == _manifest(deps, a["input_snapshot_id"])["observations"][0]["curated_checksum"]


def test_revised_close_keeps_both_values_and_flags_source_revised(make_deps: Any, mock_provider: Any, octx: Any) -> None:
    m = mock_provider()
    deps = make_deps(provider=m)
    run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "r1"), deps=deps)
    (first,) = _curated(deps, "finance/etf-daily/SPY/SPY/2026-01-09/completed_daily/")
    old_close = json.loads(deps.store.get("curated", first)[0])["observation"]["close"]
    # relative to the mock bar (its level depends on the calendar coverage start)
    new_close = round(old_close + 1.0, 4)
    m.overrides = {"2026-01-09": {"close": new_close, "high": round(old_close * 2, 4)}}
    res = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "r2"), deps=deps)
    keys = _curated(deps, "finance/etf-daily/SPY/SPY/2026-01-09/completed_daily/")
    assert len(keys) == 2
    closes = sorted(json.loads(deps.store.get("curated", k)[0])["observation"]["close"] for k in keys)
    assert new_close in closes and len(set(closes)) == 2
    snap = res["snapshot"]
    assert "source_revised" in snap["quality_flags"]
    assert snap["quality_details"]["source_revised"] == [{"instrument_id": "SPY", "session_date": "2026-01-09"}]


# ------------------------------------------------------------------ ING-08
def test_snapshot_result_fields_and_contract(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    snap = res["snapshot"]
    assert validate(snap, "input-snapshot").valid
    for f in ("input_snapshot_id", "manifest_checksum", "source_timestamps", "coverage", "quality_flags", "dataset", "lineage"):
        assert f in snap
    assert snap["lineage"]["retrieved_at"] == "2026-01-12T14:30:00Z" and snap["lineage"]["calendar_version"].startswith("fixture-synthetic-v1")
    assert snap["source_timestamps"] == {"earliest": "2026-01-05T21:00:00Z", "latest": "2026-01-09T21:00:00Z"}
    assert snap["coverage"] == {"start": "2026-01-05", "end": "2026-01-09"} and res["coverage_complete"] is True
    # the manifest checksum is the stored manifest's
    _, stored = deps.store.get("snapshots", key_snapshot_manifest(res["input_snapshot_id"]))
    assert stored.checksum == snap["manifest_checksum"]
    # no storage location anywhere in the response
    text = json.dumps(res)
    for bucket in deps.store._buckets.values():
        assert bucket not in text
    assert "s3://" not in text and "VersionId" not in text


def test_same_content_later_retrieval_new_id_same_content_checksum(make_deps: Any, octx: Any, clock: FrozenClock) -> None:
    deps = make_deps()
    a = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09", "same-1"), deps=deps)
    clock.advance(hours=2)
    b = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09", "same-2"), deps=deps)
    assert a["input_snapshot_id"] != b["input_snapshot_id"]
    assert a["content_checksum"] == b["content_checksum"] and a["snapshot"]["manifest_checksum"] != b["snapshot"]["manifest_checksum"]
    assert "no_new_observations" in b["snapshot"]["quality_flags"] and "no_new_observations" not in a["snapshot"]["quality_flags"]


def test_snapshot_artifacts_cannot_be_overwritten(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    res = run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09"), deps=deps)
    sid = res["input_snapshot_id"]
    for key in (key_snapshot_manifest(sid), key_snapshot_payload(sid, "observations.json")):
        before = deps.store.head("snapshots", key)
        with pytest.raises(ArtifactExists):
            deps.store.put_once("snapshots", key, b'{"tampered": true}')
        assert deps.store.head("snapshots", key).checksum == before.checksum  # type: ignore[union-attr]


# ------------------------------------------------------------------ ING-09
def test_duplicate_scheduler_delivery_yields_one_snapshot(make_deps: Any, sctx: Any, at: Any) -> None:
    deps = make_deps()
    at("2026-01-12T14:00:00Z")
    first = ingest(sctx(), SCHED, deps=deps)
    at("2026-01-12T14:03:00Z")  # a retry three minutes later
    second = ingest(sctx(), SCHED, deps=deps)
    assert first.response["input_snapshot_id"] == second.response["input_snapshot_id"]
    assert second.replayed and not first.replayed and len(_snapshots(deps)) == 1
    assert len(deps.provider.calls) == 1
    d = __import__("datetime").date(2026, 1, 12)
    assert scheduled_idempotency_key("beta", "finance/etf-daily/SPY", d) == "sched-beta-finance_etf-daily_SPY-2026-01-12-fixture"
    # regression (gamma's first phase 2 day): a provider switch on the same day yields a new key
    assert scheduled_idempotency_key("gamma", "finance/etf-daily/SPY", d, "yfinance") != scheduled_idempotency_key("gamma", "finance/etf-daily/SPY", d, "fixture")


def test_on_demand_requires_idempotency_key(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    body = on_demand("2026-01-09", "2026-01-09")
    del body["idempotency_key"]
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), body, deps=deps)
    assert ei.value.code == "VALIDATION_FAILED"


def test_reused_key_with_different_body_fails(make_deps: Any, octx: Any) -> None:
    deps = make_deps()
    run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "same"), deps=deps)
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-08", "2026-01-09", "same"), deps=deps)
    assert ei.value.code == "IDEMPOTENCY_KEY_REUSED"


# ------------------------------------------------------------------ ING-11
@pytest.mark.parametrize("dataset_id", ["finance/index-level/SPX", "finance/universe/SPX", "finance/etf-daily/QQQ"])
def test_other_dataset_identities_are_validation_failed(make_deps: Any, octx: Any, dataset_id: str) -> None:
    deps = make_deps()
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), {**on_demand("2026-01-09", "2026-01-09"), "dataset_id": dataset_id}, deps=deps)
    assert ei.value.code == "VALIDATION_FAILED" and ei.value.details["dataset_id"] == dataset_id
    assert ei.value.details["enabled_datasets"] == ["finance/etf-daily/SPY"]
    assert deps.provider.calls == []


def test_unsupported_contract_major_is_refused(make_deps: Any, octx: Any) -> None:
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", contract_version="9.0.0"), deps=make_deps())
    assert ei.value.code == "UNSUPPORTED_CONTRACT_VERSION"


# ------------------------------------------------------------------ COST-05
def test_budget_enforced_refuses_before_any_provider_call(make_deps: Any, octx: Any) -> None:
    deps = make_deps(budget_state=json.dumps({"state": "enforced"}))
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09"), deps=deps)
    assert ei.value.code == "BUDGET_EXCEEDED" and ei.value.retryable is False
    assert deps.provider.calls == [] and _snapshots(deps) == []


def test_budget_state_read_from_ssm(make_deps: Any, octx: Any, s3: Any) -> None:
    import boto3
    from finplan_platform.core.ingestion_budget import STATE_PARAMETER, SsmBudgetGate

    ssm = boto3.client("ssm", region_name="us-east-2")
    deps = make_deps()
    deps.budget_gate = SsmBudgetGate(ssm)
    assert run_ingestion(octx(), on_demand("2026-01-09", "2026-01-09", "b1"), deps=deps)["new_snapshot"]  # parameter absent
    ssm.put_parameter(Name=STATE_PARAMETER, Value=json.dumps({"state": "enforced"}), Type="String")
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-08", "2026-01-08", "b2"), deps=deps)
    assert ei.value.code == "BUDGET_EXCEEDED"


# ------------------------------------------------------------------ ING-14 / ING-16 / ING-17 / ING-18 (yfinance, mocked)
def _yf_deps(make_deps: Any, fake: Any, clock: FrozenClock) -> Any:
    cfg = config_with(**{"phase": 2, "ingest.provider": "yfinance"})
    p = YFinanceProvider(dataset_id=cfg.dataset_id, ticker="SPY", settings=cfg.ingest["provider_settings"], clock=clock, sleep=lambda s: clock.advance(seconds=s), library=fake, function_timeout_seconds=120)
    return make_deps(provider=p, cfg=cfg)


def test_yfinance_snapshot_normalizes_fields_and_records_lineage(make_deps: Any, fake_yf: Any, octx: Any, clock: FrozenClock, no_network: None) -> None:
    deps = _yf_deps(make_deps, fake_yf(), clock)
    res = run_ingestion(octx(), on_demand("2026-01-02", "2026-01-09"), deps=deps)
    obs = {o["session_date"]: o for o in _payload(deps, res["input_snapshot_id"])["observations"]}
    assert set(obs) == {"2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"}
    o = obs["2026-01-07"]
    assert (o["close"], o["adj_close"], o["dividend"], o["split_ratio"]) == (103.5, 103.25, 1.0, 1.0)
    assert obs["2026-01-08"]["split_ratio"] == 2.0 and all(x["kind"] == "completed_daily" for x in obs.values())
    snap = res["snapshot"]
    assert "finality_inferred" in snap["quality_flags"]
    lin = snap["lineage"]
    assert (lin["provider"], lin["provider_library"], lin["library_version"]) == ("yfinance", "yfinance", "1.7.0")
    assert lin["calendar_version"].startswith("xnys-exchange_calendars-4.13.2-") and lin["retrieved_at"] == "2026-01-12T14:30:00Z"
    m = _manifest(deps, res["input_snapshot_id"])
    assert m["adjustment_basis"] == "unadjusted" and m["calendar"]["exchange"] == "XNYS"


def test_yfinance_throttled_twice_then_success_commits_one_snapshot(make_deps: Any, fake_yf: Any, octx: Any, clock: FrozenClock) -> None:
    fake = fake_yf(["throttle", "throttle"])
    deps = _yf_deps(make_deps, fake, clock)
    res = run_ingestion(octx(), on_demand("2026-01-02", "2026-01-09"), deps=deps)
    assert res["new_snapshot"] and len(_snapshots(deps)) == 1 and len(fake.history_calls) == 3
    assert _manifest(deps, res["input_snapshot_id"])["raw_response"]["attempts"] == 3


def test_yfinance_exhausted_attempts_rate_limited_no_snapshot(make_deps: Any, fake_yf: Any, octx: Any, clock: FrozenClock) -> None:
    deps = _yf_deps(make_deps, fake_yf(["throttle"] * 10), clock)
    with pytest.raises(PlatformError) as ei:
        run_ingestion(octx(), on_demand("2026-01-02", "2026-01-09"), deps=deps)
    assert ei.value.code == "RATE_LIMITED" and ei.value.retryable and ei.value.details["attempts"] == 4
    assert _snapshots(deps) == [] and list(deps.store.list_objects("snapshots")) == []


def test_yfinance_empty_response_flags_and_stays_committed(make_deps: Any, fake_yf: Any, octx: Any, clock: FrozenClock) -> None:
    deps = _yf_deps(make_deps, fake_yf(["empty"] * 10), clock)
    res = run_ingestion(octx(), on_demand("2026-01-05", "2026-01-09"), deps=deps)
    snap = res["snapshot"]
    assert {"empty_response", "missing_sessions"} <= set(snap["quality_flags"])
    assert snap["status"] == "committed" and "approval_rule_version" not in snap
    assert snap["quality_details"]["missing_sessions"] == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    assert validate(snap, "input-snapshot").valid


def test_yfinance_missing_adjusted_close_is_partial_response(make_deps: Any, fake_yf: Any, octx: Any, clock: FrozenClock) -> None:
    deps = _yf_deps(make_deps, fake_yf(drop_columns=("Adj Close",)), clock)
    snap = run_ingestion(octx(), on_demand("2026-01-08", "2026-01-09"), deps=deps)["snapshot"]
    assert "partial_response" in snap["quality_flags"]
    assert {"session_date": "2026-01-09", "instrument_id": "SPY", "missing_fields": ["adj_close"]} in snap["quality_details"]["partial_response"]
