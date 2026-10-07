"""Dataset identities (market-data-ingestion "Separate datasets"; task 6.9; ING-11, ING-13).

Three dataset kinds are **registered** with distinct identities (design P6):

* ``finance/etf-daily/<instrument>``: the daily series of one tradable S&P 500 tracking ETF
  (for example SPY); the only kind that can be **enabled** in phases 1 and 2;
* ``finance/index-level/<index>``: an index level series (not tradable);
* ``finance/universe/<index>``: a constituent universe.

Exactly one dataset is enabled per environment: the configured ``etf-daily`` dataset for the
configured ticker, daily granularity only. A request for any other identity (including the
index level when only the ETF is configured) fails with ``VALIDATION_FAILED`` naming the
unknown dataset; one dataset never satisfies a request for another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import EnvConfig
from .errors import PlatformError

__all__ = ["DATASET_KINDS", "DatasetSpec", "enabled_dataset", "parse_dataset_id", "resolve_dataset"]

#: registered kinds -> (asset class, granularities a later phase may enable, description)
DATASET_KINDS: dict[str, dict[str, Any]] = {
    "etf-daily": {"asset_class": "etf", "description": "Daily OHLCV series of one S&P 500 tracking ETF", "enableable": True},
    "index-level": {"asset_class": "index", "description": "Index level series (not tradable)", "enableable": False},
    "universe": {"asset_class": "equity", "description": "Index constituent universe", "enableable": False},
}
_DATASET_RE = re.compile(r"^finance/(?P<kind>[a-z0-9-]+)/(?P<subject>[A-Za-z0-9._-]+)\Z")


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    kind: str
    instrument_id: str
    asset_class: str
    currency: str
    adjustment_basis: str
    granularity: str = "daily"

    def instrument(self, *, synthetic: bool) -> dict[str, Any]:
        """The contract ``finance/v1/instrument.json`` entry of the snapshot payload."""
        out: dict[str, Any] = {
            "instrument_id": self.instrument_id,
            "name": f"{self.instrument_id} S&P 500 tracking ETF daily series" + (" (synthetic)" if synthetic else ""),
            "asset_class": self.asset_class,
            "currency": self.currency,
        }
        if synthetic:
            out["synthetic"] = True
        return out


def parse_dataset_id(dataset_id: Any) -> tuple[str, str]:
    m = _DATASET_RE.match(dataset_id) if isinstance(dataset_id, str) else None
    if not m:
        raise PlatformError.validation("dataset_id must look like finance/<kind>/<subject>", pointer="/dataset_id", dataset_id=str(dataset_id)[:128])
    return m.group("kind"), m.group("subject")


def enabled_dataset(cfg: EnvConfig) -> DatasetSpec:
    ds = cfg.dataset
    if ds["kind"] != "etf-daily" or ds["granularity"] != "daily":  # the build-stage check fails first
        raise PlatformError.precondition("only the daily etf-daily dataset can be enabled", reason="dataset_not_enableable")
    return DatasetSpec(
        dataset_id=cfg.dataset_id,
        kind="etf-daily",
        instrument_id=str(ds["instrument"]),
        asset_class=DATASET_KINDS["etf-daily"]["asset_class"],
        currency=str(ds["currency"]),
        adjustment_basis=str(ds["adjustment_basis"]),
        granularity="daily",
    )


def resolve_dataset(cfg: EnvConfig, dataset_id: Any) -> DatasetSpec:
    """The enabled dataset for ``dataset_id`` or ``VALIDATION_FAILED`` naming the unknown dataset."""
    kind, _subject = parse_dataset_id(dataset_id)
    enabled = enabled_dataset(cfg)
    if dataset_id != enabled.dataset_id:
        registered = kind in DATASET_KINDS
        raise PlatformError.validation(
            f"unknown dataset {dataset_id!r}: it is not enabled in this environment" if registered else f"unknown dataset {dataset_id!r}: kind {kind!r} is not registered",
            pointer="/dataset_id",
            field="dataset_id",
            dataset_id=dataset_id,
            dataset_kind_registered=registered,
            enabled_datasets=[enabled.dataset_id],
        )
    return enabled
