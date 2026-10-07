"""Contract versions on requests and records (task 5.4; API-10; design P4 "Schema upgrade").

Rules implemented here:

* **Served majors.** The platform serves the major of the pinned contract package
  (:func:`served_majors`). A request that declares another major (body ``contract_version``
  or header ``X-Finplan-Contract-Version``) fails with ``UNSUPPORTED_CONTRACT_VERSION`` and
  ``details.served_contract_majors`` (API-02). When a new major is adopted, the previous one
  stays in :data:`ADDITIONAL_SERVED_MAJORS` until its records and callers are gone.
* **Writers write only the current version.** Every record a write creates is stamped with
  :func:`current_version` (the ``contract_version`` metadata attribute next to the record).
* **Readers never rewrite.** :func:`serve` returns the stored document with the mutable head
  overlaid, the recorded ``contract_version`` echoed, and *additive* fields that older minors
  lack filled with read-time defaults (:data:`READ_DEFAULTS`). The stored item and its
  checksum are never touched; a record under an unserved major is refused.

The 0.x caveat: the pinned package is 0.2.0 (a 0.x pre-release, beta only), so the served
major is ``0`` until contracts 1.0.0 is published and pinned. Within 0.x a minor may break, so the platform pins the
exact version and treats the major as the compatibility unit, exactly as for 1.x.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from finplan_contracts.schemas import load_store

from .errors import PlatformError
from .repository import Record

__all__ = [
    "ADDITIONAL_SERVED_MAJORS",
    "CONTRACT_VERSION_HEADER",
    "READ_DEFAULTS",
    "check_declared_version",
    "current_version",
    "major_of",
    "serve",
    "served_majors",
]

CONTRACT_VERSION_HEADER = "x-finplan-contract-version"
#: Older majors still served next to the current one (empty while only one major exists).
ADDITIONAL_SERVED_MAJORS: tuple[int, ...] = ()
#: Overrides the pinned package version (tests simulating a newer build; never set in code).
CURRENT_VERSION_OVERRIDE: str | None = None

#: Additive fields defaulted at read time for records written under an older minor.
#: Only fields whose absence has an unambiguous meaning are listed.
READ_DEFAULTS: dict[str, dict[str, Any]] = {
    "plan_version": {"no_effect": False},
    "execution": {"publication_superseded": False},
    "snapshot_catalog": {"quality_flags": []},
}


def current_version() -> str:
    return CURRENT_VERSION_OVERRIDE or load_store().version


def major_of(version: str) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError):
        raise PlatformError.validation("contract_version must be a semantic version", pointer="/contract_version") from None


def served_majors() -> list[int]:
    return sorted({major_of(current_version()), *ADDITIONAL_SERVED_MAJORS})


def check_declared_version(declared: Any, *, pointer: str = "/contract_version") -> None:
    """``UNSUPPORTED_CONTRACT_VERSION`` unless the declared version's major is served (API-02)."""
    if declared is None:
        return
    if not isinstance(declared, str):
        raise PlatformError.validation("contract_version must be a string", pointer=pointer)
    if major_of(declared) not in served_majors():
        raise PlatformError(
            "UNSUPPORTED_CONTRACT_VERSION",
            "the declared contract major is not served by this platform release",
            served_contract_majors=served_majors(),
            declared_contract_version=declared if len(declared) <= 32 else declared[:32],
        )


def serve(record: Record, *, overlay: bool = True) -> dict[str, Any]:
    """The record as served to clients: stored doc + head overlay + read-time defaults.

    The returned dict is a copy; the stored record is never rewritten (API-10).
    """
    recorded = record.attrs.get("contract_version")
    if recorded is not None and major_of(str(recorded)) not in served_majors():
        raise PlatformError(
            "UNSUPPORTED_CONTRACT_VERSION",
            "the record was written under a contract major this release does not serve",
            served_contract_majors=served_majors(),
            record_type=record.table,
        )
    doc = record.view() if overlay else copy.deepcopy(record.doc)
    for k, v in READ_DEFAULTS.get(record.table, {}).items():
        if k not in doc:
            doc[k] = copy.deepcopy(v)
    if recorded is not None:
        doc["contract_version"] = str(recorded)
    return doc


def strip_served(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the served-only ``contract_version`` echo (to compare with stored content)."""
    out = dict(doc)
    out.pop("contract_version", None)
    return out
