"""Injectable clocks.

Every operation reads time only through a :class:`Clock` carried by the
:class:`~finplan_platform.core.context.OperationContext`, so tests freeze or advance
time deterministically (ING-04 frozen clock at 09:00 ET, MDS-02 orphan grace, STO-07
expired upload grants, idempotency retention).

All datetimes are timezone-aware UTC. :func:`to_timestamp` renders the contract
``timestamp`` format (RFC 3339 UTC ending in ``Z``, ``core/v1/common.json``).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FrozenClock", "to_timestamp", "parse_timestamp", "epoch_seconds", "UTC"]


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class SystemClock:
    """Wall clock (production)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that only moves when told to (tests). Thread-safe."""

    def __init__(self, at: datetime | str = "2026-01-12T14:30:00Z") -> None:
        self._lock = threading.Lock()
        self._now = _coerce(at)

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, at: datetime | str) -> None:
        with self._lock:
            self._now = _coerce(at)

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        """Move forward by ``delta`` or by ``timedelta(**kwargs)``; returns the new time."""
        step = delta if delta is not None else timedelta(**kwargs)
        with self._lock:
            self._now = self._now + step
            return self._now


def _coerce(at: datetime | str) -> datetime:
    if isinstance(at, str):
        return parse_timestamp(at)
    if at.tzinfo is None:
        raise ValueError("FrozenClock needs a timezone-aware datetime")
    return at.astimezone(UTC)


def to_timestamp(dt: datetime, *, millis: bool = False) -> str:
    """Contract timestamp: ``YYYY-MM-DDTHH:MM:SS[.mmm]Z`` (UTC)."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    u = dt.astimezone(UTC)
    if millis:
        return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"
    return u.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> datetime:
    """Parse a contract timestamp (``Z`` or an explicit offset) into aware UTC."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no time zone")
    return dt.astimezone(UTC)


def epoch_seconds(dt: datetime) -> int:
    """Whole epoch seconds (DynamoDB TTL attribute format)."""
    return int(dt.astimezone(UTC).timestamp())
