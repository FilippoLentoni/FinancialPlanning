"""CS-01 copied-$id detector over fixture consumer trees (task 6.3), and consumer-mode conformance (6.5)."""

import json
import shutil
from pathlib import Path

import pytest

from finplan_contracts import conformance, copied_id
from finplan_contracts.copied_id import scan_tree

from conftest import CONTRACTS

TREES = Path(__file__).parent / "fixtures" / "consumer-trees"


@pytest.fixture()
def clean_tree(tmp_path):
    root = tmp_path / "consumer"
    shutil.copytree(TREES / "clean", root)
    return root


def test_clean_consumer_passes(clean_tree):
    """Referencing contract $ids ($ref, strings) is the intended use and is not reported."""
    assert scan_tree(clean_tree) == []


def test_installed_package_copies_are_ignored(clean_tree):
    """The pinned package's own schemas under node_modules / site-packages are not copies."""
    nm = clean_tree / "node_modules" / "@finplan" / "contracts" / "data" / "core" / "v1"
    nm.mkdir(parents=True)
    shutil.copy(CONTRACTS / "core" / "v1" / "plan.json", nm / "plan.json")
    sp = clean_tree / ".venv" / "lib" / "python3.12" / "site-packages" / "finplan_contracts" / "data" / "core" / "v1"
    sp.mkdir(parents=True)
    shutil.copy(CONTRACTS / "core" / "v1" / "plan.json", sp / "plan.json")
    assert scan_tree(clean_tree) == []


def test_copied_schema_and_embedded_declaration_fail():
    """CS-01: a consumer containing a schema whose $id is in the contract namespace fails and reports the $id."""
    found = scan_tree(TREES / "copied")
    assert sorted((c.rule, c.path, c.schema_id) for c in found) == [
        ("copied-id", "schemas/error.json", "https://contracts.finplan.invalid/core/v1/error.json"),
        ("copied-id", "src/schemas.ts", "https://contracts.finplan.invalid/core/v1/execution.json"),
    ]
    assert "core/v1/error.json" in str(found[0]) or "core/v1/error.json" in str(found[1])


def test_renamed_copy_is_detected_by_content(clean_tree):
    """A vendored copy whose $id and title were changed is still a copy (generated from the current schema)."""
    schema = json.loads((CONTRACTS / "core" / "v1" / "artifact-ref.json").read_text())
    schema["$id"] = "https://consumer.example.invalid/vendored/artifact-ref.json"
    schema["title"] = "Vendored artifact reference (renamed copy)"
    (clean_tree / "schemas" / "artifact-ref.json").write_text(json.dumps(schema, indent=2))
    [c] = scan_tree(clean_tree)
    assert c.rule == "copied-content" and c.path == "schemas/artifact-ref.json" and c.schema_id.endswith("/core/v1/artifact-ref.json")


def test_yaml_copy_detected(clean_tree):
    import yaml

    schema = json.loads((CONTRACTS / "core" / "v1" / "caller.json").read_text())
    (clean_tree / "schemas" / "caller.yaml").write_text(yaml.safe_dump(schema))
    [c] = scan_tree(clean_tree)
    assert c.rule == "copied-id" and c.path == "schemas/caller.yaml"


def test_cli(capsys):
    assert copied_id.main([str(TREES / "clean")]) == 0
    assert copied_id.main([str(TREES / "copied"), "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["ok"] is False


def test_consumer_conformance_fails_on_copied_schema():
    """CS-01 through the conformance runner: consumer mode with --repo fails on a copied $id."""
    report = conformance.run_consumer(CONTRACTS, repo=TREES / "copied")
    assert not report.ok
    assert {p.check for p in report.problems} == {"CS-01"}
    assert conformance.run_consumer(CONTRACTS, repo=TREES / "clean").ok


def test_consumer_conformance_checks_the_pinned_version():
    version = (CONTRACTS / "VERSION").read_text().strip()
    assert conformance.run_consumer(CONTRACTS, expect_version=version).ok
    report = conformance.run_consumer(CONTRACTS, expect_version="9.9.9")
    assert [p.check for p in report.problems] == ["CS-10"]


def test_conformance_cli_consumer_options(capsys):
    assert conformance.main(["--mode", "consumer", "--root", str(CONTRACTS), "--repo", str(TREES / "copied")]) == 1
    assert "CS-01" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        conformance.main(["--repo", str(TREES / "clean")])  # consumer-only option in producer mode


def test_yaml_dates_and_non_json_documents_do_not_crash(clean_tree):
    """0.2.0 (design D13): planning metadata such as openspec ``.openspec.yaml`` (``created: 2026-10-07``),
    YAML holding non-JSON values and unparsable files are skipped, never a crash."""
    meta = clean_tree / "openspec" / "changes" / "some-change"
    meta.mkdir(parents=True)
    (meta / ".openspec.yaml").write_text("schema: spec-driven\ncreated: 2026-10-07\n", encoding="utf-8")
    (meta / "set.yaml").write_text("values: !!set {a: null, b: null}\nwhen: 2026-10-07T10:00:00Z\n", encoding="utf-8")
    (meta / "broken.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    assert scan_tree(clean_tree) == []


def test_yaml_copy_of_a_schema_with_dates_elsewhere_is_still_found(clean_tree):
    """Timestamps stay text, so a YAML copy of a contract schema is still recognised by content."""
    import yaml

    schema = json.loads((CONTRACTS / "core" / "v1" / "plan.json").read_text())
    schema.pop("$id")
    (clean_tree / "copied-plan.yaml").write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")
    (clean_tree / "dated.yaml").write_text("created: 2026-10-07\n", encoding="utf-8")
    found = scan_tree(clean_tree)
    assert [(c.rule, c.path) for c in found] == [("copied-content", "copied-plan.yaml")]
