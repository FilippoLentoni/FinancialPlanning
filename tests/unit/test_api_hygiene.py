"""Public-repo hygiene of the API-owned files: leak scan (OWN-03) and copied-$id check (CS-01)."""

from __future__ import annotations

from pathlib import Path

from finplan_contracts import copied_id, leak_scan

ROOT = Path(__file__).resolve().parents[2]
API_PATHS = [
    "platform/finplan_platform/core/plans.py",
    "platform/finplan_platform/core/versions.py",
    "platform/finplan_platform/core/validation.py",
    "platform/finplan_platform/core/publication.py",
    "platform/finplan_platform/core/execution.py",
    "platform/finplan_platform/core/upgrade.py",
    "platform/finplan_platform/core/contract_io.py",
    "platform/finplan_platform/core/services.py",
    "platform/finplan_platform/core/snapshot_reads.py",
    "platform/finplan_platform/handlers/api.py",
    "infra/stacks/api.py",
    "docs/plan-api.md",
    "tests/api_support.py",
    "tests/contract/test_api_contract.py",
    *(str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests" / "unit").glob("test_api_*.py"))),
    "tests/unit/test_plan_api_docs.py",
]


def test_leak_scan_passes_on_api_files() -> None:
    count, findings = leak_scan.scan_paths([ROOT / p for p in API_PATHS])
    assert count == len(API_PATHS) and findings == [], [str(f) for f in findings]


def test_no_contract_schema_is_declared_in_api_files() -> None:
    for p in API_PATHS:
        found = copied_id.scan_tree(ROOT / p) if (ROOT / p).is_dir() else [f for f in copied_id.scan_tree((ROOT / p).parent) if f.path.endswith(Path(p).name)]
        assert found == [], [str(f) for f in found]
