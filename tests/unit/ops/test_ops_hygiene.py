"""Public-repository hygiene of the OPS files and the runbook checklist (tasks 9.x, 10.x, 10.5).

The leak scan (contracts OWN-03, ENV-08) and the copied-``$id`` detector (CS-01) pass on every file
this module owns; the runbook covers every item task 10.5 lists; no price is written anywhere.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from finplan_contracts import copied_id, leak_scan

ROOT = Path(__file__).resolve().parents[3]
OPS_FILES = [
    "infra/stacks/tooling.py",
    "infra/stacks/pipeline.py",
    "platform/finplan_platform/core/budget.py",
    "platform/finplan_platform/handlers/budget_state.py",
    "scripts/build_gates.py",
    "scripts/build_stage.py",
    "scripts/bootstrap.py",
    "scripts/cost_checks.py",
    "scripts/publish_assets.py",
    "scripts/release.py",
    "scripts/stage_runner.py",
    "scripts/synth.py",
    "tests/smoke/smoke_suite.py",
    "tests/smoke/transport.py",
    "tests/smoke/test_prod_smoke.py",
    "docs/bootstrap.md",
    "docs/pipeline.md",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "unit" / "ops").glob("*.py")),
]


def test_leak_scan_passes_on_ops_files() -> None:
    findings = leak_scan.scan_files([ROOT / f for f in OPS_FILES], root=ROOT)
    assert findings == [], [str(f) for f in findings]


def test_no_copied_contract_schema() -> None:
    assert copied_id.scan_tree(ROOT / "infra") == [] and copied_id.scan_tree(ROOT / "scripts") == [] and copied_id.scan_tree(ROOT / "tests" / "smoke") == []


def test_no_price_is_written_in_the_docs() -> None:
    for doc in ("docs/bootstrap.md", "docs/pipeline.md"):
        text = (ROOT / doc).read_text()
        assert not re.search(r"\$\s?\d|\d+(\.\d+)?\s?(USD|usd)\s*(per|/)\s*(hour|month|GB|request)", text), doc


def test_budget_state_writer_is_standalone() -> None:
    tree = ast.parse((ROOT / "platform/finplan_platform/handlers/budget_state.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"json", "os", "re", "datetime", "boto3"}


def test_runbook_covers_the_task_10_5_checklist() -> None:
    text = (ROOT / "docs" / "bootstrap.md").read_text().lower()
    for needle in (
        "in principle on 2026-10-07",
        "exact stacks",
        "cost estimate",
        "existing credentials",
        "not refused",
        "mfa",
        "account_id",
        "session region",
        "codeconnection-ref",
        "scoped roles",
        "source-stage dry run",
        "extend the github app installation",
        "notification address",
        "budget-allocation",
        "platform_infra` 8",
    ):
        assert needle in text, needle
