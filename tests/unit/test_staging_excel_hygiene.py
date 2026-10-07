"""Public-repo hygiene of the STAGING-owned files (leak scan, copied contract ``$id``s)."""

from __future__ import annotations

from pathlib import Path

from finplan_contracts import copied_id, leak_scan

ROOT = Path(__file__).resolve().parents[2]
STAGING_PATHS = [
    "platform/finplan_platform/core/staging.py",
    "platform/finplan_platform/excel",
    "docs/staging.md",
    "docs/excel.md",
    "tests/unit/test_staging.py",
    "tests/unit/test_excel_package.py",
    "tests/unit/test_excel_import.py",
    "tests/unit/excel_support.py",
]


def test_leak_scan_passes_on_staging_and_excel_files() -> None:
    count, findings = leak_scan.scan_paths([ROOT / p for p in STAGING_PATHS])
    assert count >= len(STAGING_PATHS)
    assert findings == [], [str(f) for f in findings]


def test_no_contract_schema_is_copied() -> None:
    """CS-01: staging and Excel validate through the pinned package; no schema is vendored."""
    found = []
    for p in ("platform/finplan_platform/excel", "docs", "tests/unit"):
        found += copied_id.scan_tree(ROOT / p)
    assert found == [], found
