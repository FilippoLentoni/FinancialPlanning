"""Provider selection from environment configuration (ING-10 phase gate, defence in depth).

The build-stage configuration check already refuses a phase 1 configuration that names any
provider other than ``fixture`` (``core.config.validate_config``). :func:`provider_for_config`
repeats the gate at run time so a mis-deployed configuration can never reach the real
provider: ``yfinance`` is built only when the configuration declares phase 2.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..core.calendar import SessionCalendar, fixture_calendar
from ..core.clock import Clock, SystemClock
from ..core.config import EnvConfig
from ..core.errors import PlatformError
from .base import ProviderAdapter
from .fixture import FixtureProvider

__all__ = ["KNOWN_PROVIDERS", "provider_for_config"]

KNOWN_PROVIDERS = ("fixture", "yfinance")


def provider_for_config(
    cfg: EnvConfig,
    *,
    clock: Clock | None = None,
    library: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    calendar: SessionCalendar | None = None,
) -> ProviderAdapter:
    clock = clock or SystemClock()
    name = cfg.provider
    if name == "fixture":
        return FixtureProvider(dataset_id=cfg.dataset_id, instrument_id=str(cfg.dataset["instrument"]), calendar=calendar or fixture_calendar(), clock=clock)
    if name == "yfinance":
        if cfg.phase != 2:
            raise PlatformError.precondition("the yfinance provider is enabled only by a phase 2 configuration", reason="phase_gate", phase=cfg.phase, provider=name)
        from .yfinance_provider import YFinanceProvider

        return YFinanceProvider(
            dataset_id=cfg.dataset_id,
            ticker=str(cfg.dataset["instrument"]),
            settings=cfg.ingest["provider_settings"],
            clock=clock,
            sleep=sleep,
            library=library,
            function_timeout_seconds=int(cfg.ingest["function_timeout_seconds"]),
        )
    raise PlatformError.precondition(f"unknown provider {name!r}", reason="unknown_provider", provider=name)
