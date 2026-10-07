"""DOM-01 domain-neutrality check of the core envelope schemas (task 5.3, 12.1 neutrality part)."""

import json
import shutil
from pathlib import Path

import pytest

from finplan_contracts import conformance, neutrality
from finplan_contracts.neutrality import check_root, check_schema, load_deny_terms, match_term

from conftest import CONTRACTS

FIX = Path(__file__).parent / "fixtures" / "neutrality"


@pytest.fixture()
def tmp_root(tmp_path):
    root = tmp_path / "contracts"
    root.mkdir()
    for part in ("VERSION", "core", "finance", "domains", "fixtures", "conformance"):
        src = CONTRACTS / part
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / part)
    return root


def test_deny_list_comes_from_the_finance_adapter_registration():
    reg = json.loads((CONTRACTS / "domains" / "registry.json").read_text())
    [finance] = [d for d in reg["domains"] if d["domain"] == "finance"]
    terms = load_deny_terms(CONTRACTS)
    assert terms == sorted(set(finance["neutrality_deny_terms"]))
    assert "ticker" in terms


def test_current_core_schemas_are_domain_neutral():
    """DOM-01 / 12.1: every core/v1 schema (including the D10 additions and tools/*) passes."""
    problems = check_root()
    assert problems == [], [str(p) for p in problems]


def test_ticker_added_to_envelope_fails(tmp_root):
    """DOM-01 scenario "Finance field in envelope": adding ticker to an envelope schema fails."""
    shutil.copy(FIX / "domain-envelope-with-ticker.json", tmp_root / "core" / "v1" / "domain-envelope.json")
    problems = check_root(tmp_root)
    assert [(p.schema, p.pointer, p.name, p.term) for p in problems] == [("core/v1/domain-envelope.json", "/properties/ticker", "ticker", "ticker")]
    assert neutrality.main(["--root", str(tmp_root)]) == 1


def test_ticker_variants_are_caught_by_tokens():
    terms = load_deny_terms(CONTRACTS)
    schema = json.loads((FIX / "job-submission-with-camelcase-ticker.json").read_text())
    problems = check_schema("job-submission", schema, terms)
    assert [p.name for p in problems] == ["primaryTicker"]
    assert match_term("ticker_symbol", terms) in ("ticker", "symbol")
    assert match_term("plan_version_id", terms) is None
    assert match_term("remaining_allocation_usd", terms) is None  # design D10 budget field, exempt


def test_enum_values_required_names_and_domain_refs_are_checked():
    terms = ["ticker", "holdings", "trade"]
    schema = {
        "$id": "https://contracts.finplan.invalid/core/v1/x.json",
        "type": "object",
        "required": ["holdings"],
        "properties": {
            "kind": {"enum": ["plan_export", "trade"]},
            "payload": {"$ref": "https://contracts.finplan.invalid/finance/v1/instrument.json"},
        },
        "$defs": {"ticker_map": {"type": "object"}},
    }
    kinds = sorted((p.kind, p.name) for p in check_schema("x", schema, terms))
    assert kinds == [
        ("$ref to domain adapter", "https://contracts.finplan.invalid/finance/v1/instrument.json"),
        ("definition", "ticker_map"),
        ("enum value", "trade"),
        ("required name", "holdings"),
    ]


def test_explicit_deny_list_file(tmp_path):
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps(["correlation"]))
    problems = check_root(None, deny)
    assert problems and all(p.term == "correlation" for p in problems)


def test_conformance_suite_runs_the_neutrality_check(tmp_root):
    assert not [p for p in conformance.run_suite(tmp_root).problems if p.check == "DOM-01"]
    shutil.copy(FIX / "domain-envelope-with-ticker.json", tmp_root / "core" / "v1" / "domain-envelope.json")
    report = conformance.run_suite(tmp_root)
    assert any(p.check == "DOM-01" and "ticker" in p.message for p in report.problems)


def test_cli_passes_on_package(capsys):
    assert neutrality.main([]) == 0
    assert "PASS" in capsys.readouterr().out
    assert neutrality.main(["--json"]) == 0
