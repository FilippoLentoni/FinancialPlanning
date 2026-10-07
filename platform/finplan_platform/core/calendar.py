"""Versioned exchange session calendars (market-data-ingestion; task 6.1; ING-03, ING-04, ING-15).

A calendar is a JSON artifact shipped with the service under
``finplan_platform/data/calendars/``. It maps every covered **weekday** to ``regular``,
``early_close`` or ``holiday`` (with the session's open and close in exchange-local time);
weekends are derived and never listed. Dates outside ``coverage`` are not guessed: a lookup
fails with ``PRECONDITION_FAILED`` and details naming the calendar coverage (ING-03).

Two calendars ship:

* ``xnys-exchange_calendars-<version>-<start>-<end>.json``: generated at **build time** from
  the exactly pinned ``exchange_calendars`` library (exchange ``XNYS``) by
  ``scripts/generate_calendar.py``. The ingestion function never imports the library at run
  time. Its version string ``xnys-exchange_calendars-<library_version>-<YYYYMMDD>-<YYYYMMDD>``
  names the exchange, the library, the pinned version and the coverage (ING-15, design P5).
* ``fixture-synthetic-v1-<start>-<end>.json``: the synthetic fixture calendar, with rule-based
  holidays and early closes (never real exchange data). It may be used only with the fixture
  provider and in tests (spec "XNYS session calendar from a pinned library").

Times: :meth:`SessionCalendar.session_close_utc` converts the local close (``16:00``, or the
early close such as ``13:00``) in ``America/New_York`` to UTC with ``zoneinfo``, so daylight
saving never shifts the local close.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .errors import PlatformError

__all__ = [
    "CALENDAR_DIR",
    "SESSION_STATUSES",
    "TRADING_STATUSES",
    "SessionCalendar",
    "SessionDay",
    "calendar_for_provider",
    "fixture_calendar",
    "load_calendar",
    "load_calendar_file",
    "parse_date",
    "xnys_calendar",
]

SESSION_STATUSES = ("regular", "early_close", "holiday", "weekend")
TRADING_STATUSES = ("regular", "early_close")
CALENDAR_DIR = Path(__file__).resolve().parents[1] / "data" / "calendars"
FIXTURE_CALENDAR_PREFIX = "fixture-synthetic-"
XNYS_CALENDAR_PREFIX = "xnys-exchange_calendars-"


def parse_date(value: str | date, pointer: str = "") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise PlatformError.validation(f"{value!r} is not a YYYY-MM-DD date", pointer=pointer) from None


@dataclass(frozen=True)
class SessionDay:
    day: date
    status: str  # regular | early_close | holiday | weekend
    open_local: time | None = None
    close_local: time | None = None

    @property
    def is_session(self) -> bool:
        return self.status in TRADING_STATUSES


@dataclass(frozen=True)
class SessionCalendar:
    """An immutable, versioned session calendar."""

    version: str
    exchange: str
    timezone: str
    coverage_start: date
    coverage_end: date
    days: Mapping[date, SessionDay] = field(repr=False)
    source: Mapping[str, Any] = field(default_factory=dict, repr=False)
    synthetic: bool = False

    # ------------------------------------------------------------ construction
    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> SessionCalendar:
        start = date.fromisoformat(doc["coverage"]["start"])
        end = date.fromisoformat(doc["coverage"]["end"])
        default_open = time.fromisoformat(doc["regular_open"])
        default_close = time.fromisoformat(doc["regular_close"])
        days: dict[date, SessionDay] = {}
        for ds, entry in doc["days"].items():
            d = date.fromisoformat(ds)
            if d.weekday() >= 5:
                raise ValueError(f"calendar {doc['version']} lists weekend date {ds}")
            status = entry["status"] if isinstance(entry, Mapping) else str(entry)
            if status not in ("regular", "early_close", "holiday"):
                raise ValueError(f"calendar {doc['version']}: unknown status {status!r} on {ds}")
            if status == "holiday":
                days[d] = SessionDay(d, status)
                continue
            op = time.fromisoformat(entry["open"]) if isinstance(entry, Mapping) and entry.get("open") else default_open
            cl = time.fromisoformat(entry["close"]) if isinstance(entry, Mapping) and entry.get("close") else default_close
            days[d] = SessionDay(d, status, op, cl)
        cur = start
        while cur <= end:
            if cur.weekday() < 5 and cur not in days:
                raise ValueError(f"calendar {doc['version']} has no entry for weekday {cur.isoformat()} inside its coverage")
            cur += timedelta(days=1)
        return cls(
            version=str(doc["version"]),
            exchange=str(doc["exchange"]),
            timezone=str(doc["timezone"]),
            coverage_start=start,
            coverage_end=end,
            days=days,
            source=dict(doc.get("source") or {}),
            synthetic=bool(doc.get("synthetic", False)),
        )

    # ------------------------------------------------------------ lookups
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def coverage(self) -> dict[str, str]:
        return {"start": self.coverage_start.isoformat(), "end": self.coverage_end.isoformat()}

    def covers(self, d: date) -> bool:
        return self.coverage_start <= d <= self.coverage_end

    def require_covered(self, *dates: date) -> None:
        """``PRECONDITION_FAILED`` naming the coverage when any date is outside it (ING-03)."""
        outside = sorted({d.isoformat() for d in dates if not self.covers(d)})
        if outside:
            raise PlatformError.precondition(
                "requested dates are outside the session calendar's coverage",
                reason="calendar_coverage",
                calendar_version=self.version,
                calendar_coverage=self.coverage,
                dates_outside_coverage=outside,
            )

    def day(self, d: date) -> SessionDay:
        self.require_covered(d)
        if d.weekday() >= 5:
            return SessionDay(d, "weekend")
        return self.days[d]

    def status(self, d: date) -> str:
        return self.day(d).status

    def is_session(self, d: date) -> bool:
        return self.day(d).is_session

    def sessions_between(self, start: date, end: date) -> list[SessionDay]:
        """Trading sessions (regular or early close) in ``[start, end]``; both ends must be covered."""
        self.require_covered(start, end)
        out: list[SessionDay] = []
        cur = start
        while cur <= end:
            sd = self.day(cur)
            if sd.is_session:
                out.append(sd)
            cur += timedelta(days=1)
        return out

    def iter_days(self) -> Iterator[SessionDay]:
        cur = self.coverage_start
        while cur <= self.coverage_end:
            yield self.day(cur)
            cur += timedelta(days=1)

    def previous_session(self, d: date) -> SessionDay:
        """The latest session strictly before ``d`` (``PRECONDITION_FAILED`` past the coverage start)."""
        cur = d - timedelta(days=1)
        while True:
            self.require_covered(cur)
            sd = self.day(cur)
            if sd.is_session:
                return sd
            cur -= timedelta(days=1)

    def session_open_utc(self, d: date) -> datetime:
        sd = self.day(d)
        if not sd.is_session or sd.open_local is None:
            raise ValueError(f"{d.isoformat()} is not a session")
        return datetime.combine(d, sd.open_local, tzinfo=self.tz).astimezone(UTC)

    def session_close_utc(self, d: date) -> datetime:
        """Session close (early closes included) as aware UTC."""
        sd = self.day(d)
        if not sd.is_session or sd.close_local is None:
            raise ValueError(f"{d.isoformat()} is not a session")
        return datetime.combine(d, sd.close_local, tzinfo=self.tz).astimezone(UTC)

    def local_date(self, at: datetime) -> date:
        """Exchange-local calendar date of an instant."""
        return at.astimezone(self.tz).date()

    def latest_closed_session(self, at: datetime) -> SessionDay:
        """Most recent session whose close is at or before ``at``."""
        d = self.local_date(at)
        self.require_covered(d)
        sd = self.day(d)
        if sd.is_session and self.session_close_utc(d) <= at:
            return sd
        return self.previous_session(d)

    def describe(self) -> dict[str, Any]:
        return {"version": self.version, "exchange": self.exchange, "timezone": self.timezone, "coverage": self.coverage, "synthetic": self.synthetic, "source": dict(self.source)}


def load_calendar_file(path: str | Path) -> SessionCalendar:
    return SessionCalendar.from_doc(json.loads(Path(path).read_text(encoding="utf-8")))


@lru_cache(maxsize=8)
def _load_by_prefix(prefix: str, directory: str) -> SessionCalendar:
    matches = sorted(Path(directory).glob(f"{prefix}*.json"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected exactly one calendar matching {prefix}*.json in {directory}, found {[m.name for m in matches]}")
    return load_calendar_file(matches[0])


def load_calendar(version: str, directory: str | Path | None = None) -> SessionCalendar:
    """Load a shipped calendar by its exact version string."""
    path = Path(directory or CALENDAR_DIR) / f"{version}.json"
    cal = load_calendar_file(path)
    if cal.version != version:
        raise ValueError(f"calendar file {path.name} declares version {cal.version}")
    return cal


def fixture_calendar(directory: str | Path | None = None) -> SessionCalendar:
    """The synthetic fixture calendar (fixture provider and tests only)."""
    return _load_by_prefix(FIXTURE_CALENDAR_PREFIX, str(directory or CALENDAR_DIR))


def xnys_calendar(directory: str | Path | None = None) -> SessionCalendar:
    """The build-time XNYS calendar generated from the pinned ``exchange_calendars``."""
    return _load_by_prefix(XNYS_CALENDAR_PREFIX, str(directory or CALENDAR_DIR))


def calendar_for_provider(provider_id: str, directory: str | Path | None = None) -> SessionCalendar:
    """The fixture provider uses the synthetic calendar; every real or mock provider uses XNYS."""
    if provider_id == "fixture":
        return fixture_calendar(directory)
    return xnys_calendar(directory)
