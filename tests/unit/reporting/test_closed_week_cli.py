"""The daily weekly timer must eventually archive a fully closed seven-day window."""
from datetime import datetime, timezone

import pytest

from scripts import modeling_report as cli


@pytest.mark.parametrize("now,expected", [
    ("2026-10-25T21:10:00+00:00", "2026-10-24"),
    ("2026-10-18T00:30:00+00:00", "2026-10-16"),
    ("2026-03-09T00:30:00+00:00", "2026-03-07"),
    ("2026-11-02T00:30:00+00:00", "2026-10-31"),
])
def test_closed_window_uses_previous_new_york_calendar_day(now, expected):
    assert cli.report_end_day(None, datetime.fromisoformat(now), closed_week=True) == expected


def test_scheduled_reporting_day_is_used_without_utc_date_substitution():
    now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
    assert cli.report_end_day("2026-10-17", now, closed_week=True) == "2026-10-16"
    assert cli.report_end_day("2026-10-17", now) == "2026-10-17"


def test_closed_week_cli_requests_seven_completed_days_before_timer_day(monkeypatch, tmp_path):
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 25, 21, 10, tzinfo=timezone.utc).astimezone(tz)
    class Reader:
        def read_registry(self, experiment):
            return {"approaches": [{"approach_id": "controlled_test"}], "comparison_policy": {}}
    captured = []
    def aggregate(reader, **kwargs):
        captured.append(kwargs)
        return {"runs": [], "evidence_failures": [], "missing_slots": [], "leaderboard": []}
    monkeypatch.setattr(cli, "datetime", Frozen)
    monkeypatch.setattr(ModelingEvidence, "from_ec2", lambda **kw: Reader())
    monkeypatch.setattr(cli, "aggregate", aggregate)
    monkeypatch.setattr(cli, "consolidate", lambda reports: reports[0])
    monkeypatch.setattr(cli, "write_report", lambda *a, **kw: {"directory": str(tmp_path), "archive": str(tmp_path), "revision": "controlled"})
    assert cli.main(["--experiment", "controlled_test", "--weekly", "--closed-week", "--day", "2026-10-25", "--no-include-interim", "--no-s3-archive"]) == 0
    assert captured[0]["start_day"] == "2026-10-18"
    assert captured[0]["end_day"] == "2026-10-24"
    assert captured[0]["finalized"] is True


def test_closed_week_without_weekly_fails_before_any_aws_access(monkeypatch):
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    monkeypatch.setattr(ModelingEvidence, "from_ec2", lambda **kw: pytest.fail("Unexpected AWS access"))
    with pytest.raises(SystemExit) as result:
        cli.main(["--experiment", "controlled_test", "--closed-week"])
    assert result.value.code == 2
