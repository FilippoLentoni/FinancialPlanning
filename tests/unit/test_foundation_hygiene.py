"""Public-repo hygiene of FOUNDATION-owned files (leak scan, copied-$id) and the full CDK app synth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from finplan_contracts import copied_id, leak_scan

ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_PATHS = [
    "pyproject.toml",
    "cdk.json",
    "contracts-pin.json",
    "config",
    "docs/storage.md",
    "docs/platform-core.md",
    "infra/app.py",
    "infra/policy_sim.py",
    "infra/stacks/common.py",
    "infra/stacks/policies.py",
    "infra/stacks/storage.py",
    "infra/stacks/metadata.py",
    "platform/finplan_platform/core",
    "platform/finplan_platform/handlers/sweep.py",
    "scripts/check_contracts_pin.py",
    "tests/conftest.py",
    "tests/fakes",
    "tests/unit/test_repository.py",
    "tests/unit/test_policy_simulation.py",
]


def test_leak_scan_passes_on_foundation_files_and_storage_doc() -> None:
    count, findings = leak_scan.scan_paths([ROOT / p for p in FOUNDATION_PATHS])
    assert count > 20
    assert findings == [], [str(f) for f in findings]


def test_no_contract_schema_copied_into_the_service() -> None:
    """CS-01: the service validates with the pinned package; it never vendors schemas."""
    found = []
    for p in ("platform", "infra", "config", "tests", "docs", "scripts"):
        if (ROOT / p).exists():
            found += copied_id.scan_tree(ROOT / p)
    assert found == [], found


@pytest.mark.synth
def test_full_app_synthesizes_offline(tmp_path: Path) -> None:
    """Every environment synthesizes, including stack modules other agents add (cdk synth equivalent)."""
    import aws_cdk as cdk

    from infra.app import build_app

    app = build_app(cdk.App(outdir=str(tmp_path)))
    assembly = app.synth()
    names = {s.stack_name for s in assembly.stacks} | {s.stack_name for nested in ("Beta", "Gamma", "Prod") for s in assembly.get_nested_assembly(f"assembly-{nested}").stacks}
    for env in ("beta", "gamma", "prod"):
        assert f"finplan-{env}-financialplanning-storage" in names
        assert f"finplan-{env}-financialplanning-metadata" in names


def test_optional_module_loader_skips_missing_but_not_broken_modules(tmp_path: Path, monkeypatch: Any) -> None:
    import sys

    from infra.app import optional_module

    assert optional_module("infra.stacks.does_not_exist") is None
    pkg = tmp_path / "brokenpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("import definitely_missing_dependency_x\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):
        optional_module("brokenpkg.mod")
    sys.modules.pop("brokenpkg", None)
