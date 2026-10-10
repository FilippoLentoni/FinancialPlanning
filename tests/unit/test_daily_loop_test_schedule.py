"""Deployed test events and outcome expectations share the scheduled morning cutoff."""
from datetime import datetime
from io import BytesIO
import json

import pytest

from finplan_platform.core.config import EnvConfig, load_config
from tests.integration.test_daily_loop_deployed import _scheduler_event, _target_session, test_uni05_universe_snapshot_from_yfinance as run_uni05

DATASET = "finance/equity-etf-daily/research-universe"


def cfg(schedule_time="09:00"):
    base = load_config("beta")
    return EnvConfig({**base.data, "ingest": {**base.ingest, "schedule_time": schedule_time}})


@pytest.mark.parametrize("schedule_time, fire", [("09:00", "2026-10-09T13:00:00Z"), ("09:30", "2026-10-09T13:30:00Z")])
@pytest.mark.parametrize("wallclock", ["2026-10-09T12:00:00Z", "2026-10-09T15:00:00Z", "2026-10-09T21:00:00Z", "2026-10-10T00:26:00Z"])
def test_same_ny_fire_date_before_and_after_close_replays_same_session(schedule_time, fire, wallclock):
    config = cfg(schedule_time)
    now = datetime.fromisoformat(wallclock)
    event = _scheduler_event(config, DATASET, now=now)
    assert event["scheduled_time"] == fire
    assert _target_session(config, now=now) == ("2026-10-08", True)
    assert _target_session(config, now=datetime.fromisoformat(event["scheduled_time"])) == ("2026-10-08", True)


@pytest.mark.parametrize("wallclock, fire, expected", [
    ("2026-10-10T03:59:59Z", "2026-10-09T13:00:00Z", ("2026-10-08", True)),
    ("2026-10-10T04:00:00Z", "2026-10-10T13:00:00Z", ("2026-10-10", False)),
    ("2026-11-09T23:00:00Z", "2026-11-09T14:00:00Z", ("2026-11-06", True)),
])
def test_new_york_date_boundary_weekend_and_dst(wallclock, fire, expected):
    config = cfg()
    now = datetime.fromisoformat(wallclock)
    event = _scheduler_event(config, DATASET, now=now)
    assert event["scheduled_time"] == fire
    assert _target_session(config, now=now) == expected


class UniverseIntegrationDouble:
    def __init__(self, *, no_session=True, latest_changed=False, candidate_status="committed"):
        self.no_session, self.latest_changed = no_session, latest_changed
        self.latest_reads = 0
        self.approved = {
            "input_snapshot_id": "snap_01KDVDP88REHGPBXFX6CHX92KS",
            "lineage": {"provider": "yfinance"},
            "coverage": {"start": "2010-10-01", "end": "2026-10-08"},
            "status": "approved",
            "approval_rule_version": "approval-v2-universe",
            "bias_disclosures": [{"kind": "hindsight_selection"}, {"kind": "survivorship"}],
            "observation_summary": {"instruments_expected": 5, "instruments_complete": 5},
        }
        self.candidate = {**self.approved, "input_snapshot_id": "snap_01KDVDP88REHGPBXFX6CHX92KT", "status": candidate_status, "quality_flags": ["partial_response", "rejected_records"], "coverage": {"start": "2010-10-01", "end": "2026-10-09"}}

    def invoke(self, **kwargs):
        event = json.loads(kwargs["Payload"])
        if self.no_session:
            result = {"quality_flags": ["no_session"], "new_snapshot": False, "input_snapshot_id": self.candidate["input_snapshot_id"], "snapshot": self.candidate}
        else:
            result = {"input_snapshot_id": self.candidate["input_snapshot_id"], "snapshot": {**self.candidate, "dataset": {"dataset_id": event["dataset_id"]}}}
        return {"Payload": BytesIO(json.dumps(result).encode())}

    def call(self, method, path):
        assert method == "GET"
        if path.startswith("/v1/snapshots/latest?"):
            self.latest_reads += 1
            latest = self.candidate if self.latest_changed and self.latest_reads > 1 else self.approved
            return 200, {"snapshot": latest}, {}
        if "/observations?" in path:
            return 200, {"instruments": [{"instrument_id": name} for name in ("VOO", "GOOGL", "NFLX", "AAPL", "NVDA")]}, {}
        return 200, {"snapshot": self.candidate}, {}


@pytest.mark.parametrize("no_session", [True, False])
def test_deployed_universe_check_verifies_retained_approved_baseline_and_reports_freshness(capsys, no_session):
    double = UniverseIntegrationDouble(no_session=no_session)
    run_uni05({"cfg": cfg(), "t": double, "lambda": double}, capsys)
    assert double.latest_reads == 2


@pytest.mark.parametrize("no_session", [True, False])
def test_universe_check_refuses_rejected_data_becoming_latest_approved(capsys, no_session):
    double = UniverseIntegrationDouble(no_session=no_session, latest_changed=True)
    with pytest.raises(AssertionError):
        run_uni05({"cfg": cfg(), "t": double, "lambda": double}, capsys)


def test_universe_check_refuses_provider_quality_flags_marked_approved(capsys):
    double = UniverseIntegrationDouble(no_session=False, candidate_status="approved")
    with pytest.raises(AssertionError, match="blocking provider quality"):
        run_uni05({"cfg": cfg(), "t": double, "lambda": double}, capsys)
