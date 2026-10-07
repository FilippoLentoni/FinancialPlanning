"""Provider adapter interface (market-data-ingestion "Declared provider capabilities"; task 6.2; ING-05).

Every market-data provider sits behind :class:`ProviderAdapter`:

* :meth:`ProviderAdapter.describe` returns :class:`ProviderCapabilities`: the datasets it
  serves, its granularities (``daily`` and/or ``intraday``; intraday support is **never**
  inferred from daily support), history depth, rate limits, whether it can assert finality,
  and the library and version used (snapshot lineage, ING-16).
* :meth:`ProviderAdapter.fetch` returns a :class:`RawResponse`: the provider's answer exactly
  as received, serialized to bytes. The ingestion operation persists it to the ``raw`` bucket
  **before** parsing (design P5 step 5).
* :meth:`ProviderAdapter.parse` turns a raw response into :class:`ProviderRecord` values
  (still provider-shaped; normalization to the contract observation happens in
  :mod:`finplan_platform.core.ingestion_normalize`).

Failures are typed: :class:`ProviderThrottled` (maps to ``RATE_LIMITED``, retryable) and
:class:`ProviderUnavailable` (``DEPENDENCY_UNAVAILABLE``, retryable). An *empty* answer is not
an error: it is a raw response with no rows, which ingestion reports as the ``empty_response``
quality flag after the adapter's retries (ING-18).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "GRANULARITIES",
    "FetchRequest",
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderRecord",
    "ProviderThrottled",
    "ProviderUnavailable",
    "RawResponse",
]

GRANULARITIES = ("daily", "intraday")


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    datasets: tuple[str, ...]
    granularities: tuple[str, ...]
    history_depth_days: int
    rate_limit: Mapping[str, Any]
    #: ``explicit``: records carry a provider finality marker; ``inferred``: finality only
    #: after the configured settle delay (quality flag ``finality_inferred``).
    finality: str
    library: str
    library_version: str
    synthetic: bool = False
    requires_secret: bool = False

    def __post_init__(self) -> None:
        unknown = set(self.granularities) - set(GRANULARITIES)
        if unknown:
            raise ValueError(f"unknown granularities {sorted(unknown)}")
        if self.finality not in ("explicit", "inferred"):
            raise ValueError("finality must be 'explicit' or 'inferred'")

    def supports(self, dataset_id: str, granularity: str) -> bool:
        return dataset_id in self.datasets and granularity in self.granularities

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "datasets": list(self.datasets),
            "granularities": list(self.granularities),
            "history_depth_days": self.history_depth_days,
            "rate_limit": dict(self.rate_limit),
            "finality": self.finality,
            "library": self.library,
            "library_version": self.library_version,
            "synthetic": self.synthetic,
            "requires_secret": self.requires_secret,
        }


@dataclass(frozen=True)
class FetchRequest:
    dataset_id: str
    instrument_id: str
    start: date
    end: date  # inclusive
    granularity: str = "daily"


@dataclass(frozen=True)
class RawResponse:
    """A provider answer as received (``body`` is what the ``raw`` bucket stores)."""

    provider_id: str
    body: bytes
    content_type: str
    retrieved_at: datetime
    attempts: int = 1
    row_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.row_count == 0


@dataclass(frozen=True)
class ProviderRecord:
    """One provider row, before normalization.

    ``final``: ``True``/``False`` when the provider asserts finality, ``None`` when it cannot.
    ``source_ts``: the provider's timestamp for the record (``None`` -> session close).
    Missing price fields stay ``None`` so validation can tell "absent" from "zero".
    """

    instrument_id: str
    session_date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | float | None = None
    adj_close: float | None = None
    dividend: float | None = None
    split_ratio: float | None = None
    final: bool | None = None
    source_ts: str | None = None
    bar: str = "daily"  # daily | intraday
    synthetic: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """Base class for provider failures (never carries provider payloads or credentials)."""

    retryable = True

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class ProviderThrottled(ProviderError):
    """The provider rate-limited the request (``RATE_LIMITED``)."""


class ProviderUnavailable(ProviderError):
    """Transient or repeated non-throttle failure (``DEPENDENCY_UNAVAILABLE``)."""


@runtime_checkable
class ProviderAdapter(Protocol):
    def describe(self) -> ProviderCapabilities: ...

    def fetch(self, request: FetchRequest) -> RawResponse: ...

    def parse(self, raw: RawResponse) -> list[ProviderRecord]: ...


# ------------------------------------------------------------------ raw body helpers
def encode_rows(provider_id: str, request: FetchRequest, rows: list[Mapping[str, Any]], *, retrieved_at: datetime, synthetic: bool, extra: Mapping[str, Any] | None = None) -> bytes:
    """Serialize provider rows as received (JSON, deterministic key order) for the ``raw`` bucket."""
    import json

    doc = {
        "provider": provider_id,
        "request": {"dataset_id": request.dataset_id, "instrument_id": request.instrument_id, "start": request.start.isoformat(), "end": request.end.isoformat(), "granularity": request.granularity},
        "retrieved_at": retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": list(rows),
        "synthetic": synthetic,
        **dict(extra or {}),
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_rows(body: bytes) -> dict[str, Any]:
    import json

    doc = json.loads(body.decode("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        raise ValueError("raw provider body has no rows array")
    return doc


__all__ += ["decode_rows", "encode_rows"]
