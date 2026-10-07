"""Platform identifier minting (contracts design D2; spec platform-identifiers).

FinancialPlanning mints ``pf_``, ``pl_``, ``pv_``, ``snap_``, ``pub_`` and ``exe_``
identifiers (prefix + ULID). It never mints ``run_``/``mv_`` (FinanceModel) and never
accepts a client-supplied platform ID on a create request. Internal identifiers that
are not contract identifiers use their own prefixes: ``art_`` (artifact IDs, an
``artifact-ref`` ``artifact_id``), ``aud_`` (audit events), ``imp_`` (Excel import
grants). The formats are validated against the pinned contract ``identifiers``
patterns in the unit tests, not copied here.

The ULID timestamp comes from the operation clock, so IDs minted under a frozen clock
are still unique (random part) and time-ordered across clock advances.
"""

from __future__ import annotations

import os
import random
import threading
from datetime import datetime

from ulid import ULID

from .clock import Clock, SystemClock

__all__ = ["PLATFORM_PREFIXES", "INTERNAL_PREFIXES", "IdFactory", "is_platform_id", "prefix_of"]

#: Contract identifiers minted by the platform: logical name -> prefix.
PLATFORM_PREFIXES: dict[str, str] = {
    "portfolio_id": "pf",
    "plan_id": "pl",
    "plan_version_id": "pv",
    "input_snapshot_id": "snap",
    "publication_id": "pub",
    "execution_id": "exe",
}
#: Platform-internal identifiers (not contract identifier fields).
INTERNAL_PREFIXES: dict[str, str] = {"artifact_id": "art", "audit_event_id": "aud", "import_id": "imp", "correlation_id": "cor"}

_ALL = {**PLATFORM_PREFIXES, **INTERNAL_PREFIXES}


class IdFactory:
    """Mints ``<prefix>_<ULID>`` identifiers from an injected clock.

    ``seed`` makes the random part deterministic (tests that compare IDs across runs);
    without it the OS random source is used.
    """

    def __init__(self, clock: Clock | None = None, seed: int | None = None) -> None:
        self._clock = clock or SystemClock()
        self._rng = random.Random(seed) if seed is not None else None
        self._lock = threading.Lock()
        self._last: tuple[int, int] | None = None

    def _random_bytes_unlocked(self) -> bytes:
        if self._rng is None:
            return os.urandom(10)
        # keep the top bit clear so increments never overflow in practice
        return bytes([self._rng.randrange(0, 128)]) + self._rng.randbytes(9)

    def ulid(self, at: datetime | None = None) -> str:
        """Monotonic ULID: within one millisecond (or under a frozen clock) the random part increments,
        so IDs minted by one factory sort in minting order."""
        ts = at or self._clock.now()
        ms = int(ts.timestamp() * 1000)
        with self._lock:
            if self._last is not None and ms <= self._last[0]:
                ms, rand = self._last[0], self._last[1] + 1
                if rand >= 1 << 80:  # pragma: no cover - 2**80 IDs in one millisecond
                    raise OverflowError("ULID random space exhausted for this millisecond")
            else:
                rand = int.from_bytes(self._random_bytes_unlocked(), "big")
            self._last = (ms, rand)
        return str(ULID.from_bytes(ms.to_bytes(6, "big") + rand.to_bytes(10, "big")))

    def new(self, kind: str) -> str:
        """Mint an identifier. ``kind`` is a field name (``plan_version_id``) or a prefix (``pv``)."""
        prefix = _ALL.get(kind, kind)
        if prefix not in _ALL.values():
            raise ValueError(f"the platform does not mint identifiers of kind {kind!r}")
        return f"{prefix}_{self.ulid()}"


def prefix_of(identifier: str) -> str | None:
    head, sep, _ = identifier.partition("_")
    return head if sep else None


def is_platform_id(identifier: str, kind: str) -> bool:
    """Cheap prefix check; full validation uses the contract validators."""
    prefix = _ALL.get(kind, kind)
    return isinstance(identifier, str) and identifier.startswith(prefix + "_") and len(identifier) == len(prefix) + 27
