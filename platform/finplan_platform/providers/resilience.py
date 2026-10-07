"""Client-side rate limiting and retry with exponential backoff and jitter (task 6.13; ING-17).

:class:`ResilientCaller` wraps one provider request function:

* **minimum interval**: consecutive requests through the same caller are at least
  ``min_request_interval_seconds`` apart (the caller sleeps before sending);
* **retry**: throttling (:class:`ProviderThrottled`), transient failures
  (:class:`ProviderUnavailable`) and empty responses are retried with exponential backoff
  ``min(backoff_max, backoff_initial * 2**(attempt-1))``; with ``jitter`` the wait is drawn
  from ``[delay/2, delay]`` ("equal jitter"), so waits never shrink between attempts;
* **bounded**: at most ``max_attempts`` attempts, and never a wait that would end past the
  ``deadline`` (the function timeout minus a safety margin);
* **exhausted**: a throttled last attempt raises :class:`ProviderThrottled`, any other failure
  :class:`ProviderUnavailable` (both ``retryable``); an empty last response is *returned*,
  because "empty after retries" is data (quality flag ``empty_response``), not an error.

Time comes from an injected clock and an injected ``sleep`` so tests run on a fake clock.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from ..core.clock import Clock, SystemClock
from .base import ProviderError, ProviderThrottled, ProviderUnavailable

__all__ = ["ResilientCaller", "RetryPolicy"]

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    jitter: bool = True
    min_request_interval_seconds: float = 2.0

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> RetryPolicy:
        return cls(
            max_attempts=int(settings["max_attempts"]),
            backoff_initial_seconds=float(settings["backoff_initial_seconds"]),
            backoff_max_seconds=float(settings["backoff_max_seconds"]),
            jitter=bool(settings["jitter"]),
            min_request_interval_seconds=float(settings["min_request_interval_seconds"]),
        )

    def delay(self, attempt: int) -> float:
        """Upper bound of the wait after failed attempt ``attempt`` (1-based)."""
        return min(self.backoff_max_seconds, self.backoff_initial_seconds * (2 ** (attempt - 1)))


@dataclass
class ResilientCaller:
    policy: RetryPolicy
    clock: Clock = field(default_factory=SystemClock)
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    #: absolute deadline (aware UTC); waits that would end after it are not taken
    deadline: datetime | None = None
    safety_margin_seconds: float = 5.0
    waits: list[float] = field(default_factory=list)
    #: backoff waits only (the minimum-interval top-ups are in ``waits`` too)
    backoffs: list[float] = field(default_factory=list)
    _last_request: datetime | None = None

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.waits.append(seconds)
        self.sleep(seconds)

    def _fits(self, seconds: float) -> bool:
        if self.deadline is None:
            return True
        return self.clock.now() + timedelta(seconds=seconds + self.safety_margin_seconds) <= self.deadline

    def _throttle_interval(self) -> None:
        if self._last_request is None:
            return
        elapsed = (self.clock.now() - self._last_request).total_seconds()
        remaining = self.policy.min_request_interval_seconds - elapsed
        if remaining > 0:
            self._wait(remaining)

    def call(self, fn: Callable[[], T], *, is_empty: Callable[[T], bool] = lambda _r: False) -> tuple[T, int]:
        """Run ``fn`` under the policy; returns ``(result, attempts)``."""
        last_exc: ProviderError | None = None
        result: Any = None
        attempt = 0
        while attempt < self.policy.max_attempts:
            attempt += 1
            self._throttle_interval()
            self._last_request = self.clock.now()
            try:
                result = fn()
            except ProviderError as exc:
                last_exc = exc
                result = None
            else:
                last_exc = None
                if not is_empty(result):
                    return result, attempt
            if attempt >= self.policy.max_attempts:
                break
            bound = self.policy.delay(attempt)
            wait = self.rng.uniform(bound / 2.0, bound) if self.policy.jitter else bound
            if not self._fits(max(wait, self.policy.min_request_interval_seconds)):
                break
            self.backoffs.append(wait)
            self._wait(wait)
        if last_exc is not None:
            if isinstance(last_exc, ProviderThrottled):
                raise ProviderThrottled(f"provider still throttling after {attempt} attempts", attempts=attempt) from None
            raise ProviderUnavailable(f"provider unavailable after {attempt} attempts", attempts=attempt) from None
        return result, attempt
