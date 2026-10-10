"""Deployed test events and outcome expectations share the scheduled morning cutoff."""
from datetime import datetime

import pytest

from finplan_platform.core.config import EnvConfig, load_config
from tests.integration.test_daily_loop_deployed import _scheduler_event, _target_session

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
