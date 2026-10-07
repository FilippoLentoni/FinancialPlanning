#!/usr/bin/env python3
"""Build-time session-calendar generator (task 6.1; ING-15; design P5).

Generates the versioned calendar artifacts shipped in
``platform/finplan_platform/data/calendars/``:

* ``--source xnys``: from the exactly pinned ``exchange_calendars`` library, exchange
  ``XNYS``. The version is ``xnys-exchange_calendars-<library_version>-<YYYYMMDD>-<YYYYMMDD>``.
  The generator refuses to run when the installed library version differs from the exact pin
  in ``pyproject.toml`` (an unpinned or drifted library fails the build).
* ``--source fixture``: the synthetic fixture calendar (rule-based holidays and early closes,
  flagged ``synthetic: true``), used only with the fixture provider and in tests.

``--check`` regenerates in memory and fails when the shipped file differs (the build-stage
gate); without ``--check`` the file is (re)written. The ingestion function never imports the
library: it loads the generated JSON (``finplan_platform.core.calendar``).

Usage::

    uv run python scripts/generate_calendar.py --source xnys            # write
    uv run python scripts/generate_calendar.py --source xnys --check    # build gate
    uv run python scripts/generate_calendar.py --source fixture --check
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_ingest_pins import pinned_versions

CALENDAR_DIR = ROOT / "platform" / "finplan_platform" / "data" / "calendars"
XNYS_DEFAULT_COVERAGE = (date(2024, 1, 1), date(2027, 12, 31))
FIXTURE_DEFAULT_COVERAGE = (date(2025, 1, 1), date(2027, 12, 31))
FIXTURE_VERSION_STEM = "fixture-synthetic-v1"
LIBRARY = "exchange_calendars"
LIBRARY_DIST = "exchange-calendars"


def _compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def xnys_version(library_version: str, start: date, end: date) -> str:
    return f"xnys-{LIBRARY}-{library_version}-{_compact(start)}-{_compact(end)}"


def fixture_version(start: date, end: date) -> str:
    return f"{FIXTURE_VERSION_STEM}-{_compact(start)}-{_compact(end)}"


def _weekdays(start: date, end: date):
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            yield cur
        cur += timedelta(days=1)


# ------------------------------------------------------------------ XNYS (pinned library)
def generate_xnys(start: date, end: date, *, pyproject: Path | None = None) -> dict[str, Any]:
    from importlib.metadata import version as dist_version

    pins = pinned_versions(pyproject or ROOT / "pyproject.toml")
    pinned = pins.get(LIBRARY_DIST)
    if not pinned:
        raise SystemExit(f"{LIBRARY_DIST} is not pinned to an exact version in pyproject.toml; refusing to generate (ING-15)")
    installed = dist_version(LIBRARY_DIST)
    if installed != pinned:
        raise SystemExit(f"installed {LIBRARY_DIST} {installed} differs from the pin {pinned}; run 'uv sync --locked' (ING-15)")

    from zoneinfo import ZoneInfo

    import exchange_calendars as xcals  # build-time only

    cal = xcals.get_calendar("XNYS", start=start.isoformat(), end=end.isoformat())
    tz = ZoneInfo("America/New_York")
    sessions = {ts.date() for ts in cal.sessions}
    early = {ts.date() for ts in cal.early_closes}
    days: dict[str, Any] = {}
    for d in _weekdays(start, end):
        if d not in sessions:
            days[d.isoformat()] = {"status": "holiday"}
            continue
        import pandas as pd

        ts = pd.Timestamp(d)
        open_local = cal.session_open(ts).tz_convert(tz).strftime("%H:%M")
        close_local = cal.session_close(ts).tz_convert(tz).strftime("%H:%M")
        status = "early_close" if d in early else "regular"
        entry: dict[str, Any] = {"status": status}
        if open_local != "09:30":
            entry["open"] = open_local
        if close_local != "16:00" or status == "early_close":
            entry["close"] = close_local
        days[d.isoformat()] = entry
    return {
        "version": xnys_version(installed, start, end),
        "exchange": "XNYS",
        "timezone": "America/New_York",
        "regular_open": "09:30",
        "regular_close": "16:00",
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "synthetic": False,
        "source": {
            "library": LIBRARY,
            "library_version": installed,
            "license": "Apache-2.0",
            "generator": "scripts/generate_calendar.py",
            "note": "Session schedule (dates and local open/close times) only; contains no market data.",
        },
        "days": days,
    }


# ------------------------------------------------------------------ synthetic fixture
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _observed(d: date) -> date | None:
    """Synthetic observation rule: Sunday -> Monday; Saturday -> not observed."""
    if d.weekday() == 6:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return None
    return d


def generate_fixture(start: date, end: date) -> dict[str, Any]:
    holidays: set[date] = set()
    early: set[date] = set()
    for year in range(start.year, end.year + 1):
        for fixed in (date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)):
            obs = _observed(fixed)
            if obs:
                holidays.add(obs)
        thanksgiving = _nth_weekday(year, 11, 3, 4)
        holidays.add(thanksgiving)
        early.add(thanksgiving + timedelta(days=1))
        dec24 = date(year, 12, 24)
        if dec24.weekday() < 5:
            early.add(dec24)
    early -= holidays
    days: dict[str, Any] = {}
    for d in _weekdays(start, end):
        if d in holidays:
            days[d.isoformat()] = {"status": "holiday"}
        elif d in early:
            days[d.isoformat()] = {"status": "early_close", "close": "13:00"}
        else:
            days[d.isoformat()] = {"status": "regular"}
    return {
        "version": fixture_version(start, end),
        "exchange": "XNYS",
        "timezone": "America/New_York",
        "regular_open": "09:30",
        "regular_close": "16:00",
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "synthetic": True,
        "source": {
            "library": "finplan-fixture-calendar",
            "library_version": "1",
            "generator": "scripts/generate_calendar.py",
            "note": "Synthetic rule-based calendar for the fixture provider and tests only; not an exchange schedule.",
        },
        "days": days,
    }


# ------------------------------------------------------------------ io
def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, sort_keys=False) + "\n"


def target_path(doc: dict[str, Any], directory: Path = CALENDAR_DIR) -> Path:
    return directory / f"{doc['version']}.json"


def build(source: str, start: date | None, end: date | None, pyproject: Path | None = None) -> dict[str, Any]:
    if source == "xnys":
        s, e = start or XNYS_DEFAULT_COVERAGE[0], end or XNYS_DEFAULT_COVERAGE[1]
        return generate_xnys(s, e, pyproject=pyproject)
    if source == "fixture":
        s, e = start or FIXTURE_DEFAULT_COVERAGE[0], end or FIXTURE_DEFAULT_COVERAGE[1]
        return generate_fixture(s, e)
    raise SystemExit(f"unknown source {source!r}")


def check(source: str, directory: Path = CALENDAR_DIR, pyproject: Path | None = None) -> list[str]:
    """Problems with the shipped calendar of ``source`` (empty when it matches a regeneration)."""
    prefix = "xnys-" if source == "xnys" else f"{FIXTURE_VERSION_STEM}-"
    shipped = sorted(directory.glob(f"{prefix}*.json"))
    if len(shipped) != 1:
        return [f"expected exactly one shipped {source} calendar, found {[p.name for p in shipped]}"]
    doc = json.loads(shipped[0].read_text(encoding="utf-8"))
    start, end = date.fromisoformat(doc["coverage"]["start"]), date.fromisoformat(doc["coverage"]["end"])
    try:
        fresh = build(source, start, end, pyproject)
    except SystemExit as exc:
        return [str(exc)]
    problems = []
    if shipped[0].name != target_path(fresh, directory).name:
        problems.append(f"shipped calendar {shipped[0].name} does not match the pinned library version (expected {target_path(fresh, directory).name})")
    if render(fresh) != shipped[0].read_text(encoding="utf-8"):
        problems.append(f"shipped calendar {shipped[0].name} differs from a regeneration; rerun scripts/generate_calendar.py --source {source}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=("xnys", "fixture"), required=True)
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--check", action="store_true", help="fail when the shipped file differs from a regeneration")
    ap.add_argument("--out-dir", type=Path, default=CALENDAR_DIR)
    args = ap.parse_args(argv)
    if args.check:
        problems = check(args.source, args.out_dir)
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        if not problems:
            print(f"ok: shipped {args.source} calendar matches the pinned generator")
        return 1 if problems else 0
    doc = build(args.source, args.start, args.end)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    prefix = "xnys-" if args.source == "xnys" else f"{FIXTURE_VERSION_STEM}-"
    for old in args.out_dir.glob(f"{prefix}*.json"):
        old.unlink()
    path = target_path(doc, args.out_dir)
    path.write_text(render(doc), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} ({doc['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
