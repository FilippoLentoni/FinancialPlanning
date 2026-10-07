"""The ``xlsx-plan-v1`` workbook template (design P8; contract ``finance/v1/excel-plan-template``).

The contract fixes the *logical* content (metadata + allocation rows); this module fixes the
physical layout of the workbook that carries it:

``meta`` sheet (two columns, header row ``key | value``):

====================  =====================================================================
key                   value
====================  =====================================================================
template_version      ``xlsx-plan-v1``
contract_version      contract package version the export was produced under (informational)
plan_id               the plan
base_plan_version_id  the exported version (the parent of the imported child)
base_checksum         its ``sha256:`` content checksum
expected_revision     the plan head revision at export time (optimistic concurrency)
exported_at           export timestamp (UTC)
synthetic             ``TRUE`` for synthetic data
====================  =====================================================================

``allocations`` sheet (header row ``instrument_id | target_weight | note``): one row per
instrument from row 2. The reserved instrument ID ``CASH`` carries the cash weight
(``allocation.cash_weight``), exactly as in the contract's template fixture. ``note`` is free
text, never mapped into plan content. Blank rows are ignored.

Formula-tolerant cells: a template may name columns whose formula cells are accepted with
their *cached* value (never evaluated). ``xlsx-plan-v1`` names none, so every formula cell in
the workbook fails the import.

Fields that the template does not let a user edit (``base_currency``, ``constraints``, ``fees``)
are carried over from the base version unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ALLOCATIONS_SHEET",
    "ALLOCATION_HEADER",
    "CASH_INSTRUMENT_ID",
    "META_HEADER",
    "META_KEYS",
    "META_REQUIRED",
    "META_SHEET",
    "TEMPLATES",
    "TEMPLATE_VERSION",
    "XLSX_CONTENT_TYPE",
    "TemplateSpec",
    "template_spec",
]

TEMPLATE_VERSION = "xlsx-plan-v1"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
META_SHEET = "meta"
ALLOCATIONS_SHEET = "allocations"
META_HEADER = ("key", "value")
ALLOCATION_HEADER = ("instrument_id", "target_weight", "note")
CASH_INSTRUMENT_ID = "CASH"
#: meta keys in the order the export writes them
META_KEYS = ("template_version", "contract_version", "plan_id", "base_plan_version_id", "base_checksum", "expected_revision", "exported_at", "synthetic")
META_REQUIRED = ("template_version", "plan_id", "base_plan_version_id", "base_checksum", "expected_revision", "exported_at")


@dataclass(frozen=True)
class TemplateSpec:
    version: str
    #: allocation columns whose formula cells may be read through their cached value
    formula_tolerant_columns: frozenset[str] = field(default_factory=frozenset)
    max_allocation_rows: int = 500
    max_meta_rows: int = 50


TEMPLATES: dict[str, TemplateSpec] = {TEMPLATE_VERSION: TemplateSpec(TEMPLATE_VERSION)}


def template_spec(version: str = TEMPLATE_VERSION) -> TemplateSpec:
    return TEMPLATES[version]
