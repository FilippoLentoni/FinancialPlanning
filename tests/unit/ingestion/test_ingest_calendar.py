"""Session calendars (task 6.1): ING-03 (holiday, coverage gap), ING-15 (pinned XNYS generator)."""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from finplan_platform.core.calendar import SessionCalendar, calendar_for_provider, load_calendar
from finplan_platform.core.errors import PlatformError

from scripts import check_ingest_pins, generate_calendar

ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ ING-15
def test_xnys_calendar_version_names_exchange_library_version_and_coverage(xnys: SessionCalendar) -> None:
    pins = check_ingest_pins.pinned_versions(ROOT / "pyproject.toml")
    assert xnys.version == f"xnys-exchange_calendars-{pins['exchange-calendars']}-20100101-20271231"
    assert xnys.exchange == "XNYS" and xnys.timezone == "America/New_York"
    assert xnys.source["library"] == "exchange_calendars" and xnys.source["library_version"] == pins["exchange-calendars"]
    assert xnys.source["license"] == "Apache-2.0"
    assert not xnys.synthetic


def test_shipped_xnys_calendar_matches_a_regeneration_from_the_pinned_library() -> None:
    assert generate_calendar.check("xnys") == []
    assert generate_calendar.check("fixture") == []


def test_library_calendar_early_close_and_holidays_honoured(xnys: SessionCalendar) -> None:
    # 2026-11-27 (day after Thanksgiving) is an XNYS early close at 13:00 ET, 2026-11-26 a holiday
    assert xnys.status(date(2026, 11, 27)) == "early_close"
    assert xnys.session_close_utc(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert xnys.status(date(2026, 11, 26)) == "holiday"
    assert xnys.status(date(2026, 1, 10)) == "weekend"
    # DST: the 16:00 local close is 21:00 UTC in January and 20:00 UTC in July
    assert xnys.session_close_utc(date(2026, 1, 9)).hour == 21
    assert xnys.session_close_utc(date(2026, 7, 9)).hour == 20


def test_unpinned_library_fails_the_build(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text((ROOT / "pyproject.toml").read_text().replace('"exchange-calendars==4.13.2"', '"exchange-calendars>=4.13"'))
    problems = check_ingest_pins.check(py, ROOT / "uv.lock", ROOT / "platform/finplan_platform/data/calendars", installed=False)
    assert any("exchange-calendars" in p and "exact" in p for p in problems), problems
    with pytest.raises(SystemExit, match="not pinned"):
        generate_calendar.generate_xnys(date(2026, 1, 1), date(2026, 1, 31), pyproject=py)


def test_calendar_naming_a_different_library_version_fails(tmp_path: Path) -> None:
    cal_dir = tmp_path / "calendars"
    cal_dir.mkdir()
    src = next((ROOT / "platform/finplan_platform/data/calendars").glob("xnys-*.json"))
    shutil.copy(src, cal_dir / src.name.replace("4.13.2", "4.12.0"))
    problems = check_ingest_pins.check(ROOT / "pyproject.toml", ROOT / "uv.lock", cal_dir, installed=False)
    assert any("does not name the pinned exchange_calendars" in p for p in problems)


def test_repository_pins_pass() -> None:
    assert check_ingest_pins.check(ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / "platform/finplan_platform/data/calendars") == []


# ------------------------------------------------------------------ fixture calendar
def test_fixture_calendar_is_synthetic_with_holiday_and_early_close(fixcal: SessionCalendar) -> None:
    statuses = {d.status for d in fixcal.iter_days()}
    assert {"regular", "early_close", "holiday", "weekend"} <= statuses
    assert fixcal.synthetic and fixcal.version.startswith("fixture-synthetic-v1-")
    assert calendar_for_provider("fixture").version == fixcal.version
    assert calendar_for_provider("yfinance").exchange == "XNYS" and not calendar_for_provider("yfinance").synthetic


def test_calendar_loads_by_exact_version(xnys: SessionCalendar) -> None:
    assert load_calendar(xnys.version).version == xnys.version


def test_calendar_with_a_gap_is_rejected() -> None:
    doc = {"version": "t", "exchange": "XNYS", "timezone": "America/New_York", "regular_open": "09:30", "regular_close": "16:00", "coverage": {"start": "2026-01-05", "end": "2026-01-07"}, "days": {"2026-01-05": {"status": "regular"}, "2026-01-07": {"status": "regular"}}}
    with pytest.raises(ValueError, match="no entry"):
        SessionCalendar.from_doc(doc)


# ------------------------------------------------------------------ ING-03 coverage
def test_out_of_coverage_is_precondition_failed_naming_the_coverage(xnys: SessionCalendar) -> None:
    with pytest.raises(PlatformError) as ei:
        xnys.sessions_between(date(2009, 12, 1), date(2010, 1, 5))
    assert ei.value.code == "PRECONDITION_FAILED"
    assert ei.value.details["calendar_coverage"] == {"start": "2010-01-01", "end": "2027-12-31"}
    assert ei.value.details["calendar_version"] == xnys.version
