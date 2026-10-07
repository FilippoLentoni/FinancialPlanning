"""CS-03 / OWN-07 schema compatibility gate with fixture schema pairs (task 7.1)."""

import json
import shutil
from pathlib import Path

import pytest

from finplan_contracts import compat, digests
from finplan_contracts.compat import ADDITIVE, ANNOTATION, BREAKING, classify_bump, compare_roots, diff_schema
from finplan_contracts.schemas import SchemaStore
from finplan_contracts.validate import validate

from conftest import CONTRACTS

PAIRS = Path(__file__).parent / "fixtures" / "compat"
CASES = sorted(p.name for p in PAIRS.iterdir() if (p / "case.json").is_file())


def test_fixture_pairs_present():
    assert len(CASES) >= 15


@pytest.mark.parametrize("case", CASES)
def test_fixture_pair(case):
    expect = json.loads((PAIRS / case / "case.json").read_text())
    report = compare_roots(PAIRS / case / "old", PAIRS / case / "new")
    assert report.ok is expect["expect_ok"], (report.problems, [str(c) for c in report.changes])
    assert report.required_bump == expect["required_bump"]


def test_cs03_optional_field_made_required_in_minor_fails():
    """CS-03 scenario: a pull request marked minor makes an optional field required -> the gate fails."""
    report = compare_roots(PAIRS / "optional-made-required" / "old", PAIRS / "optional-made-required" / "new")
    assert not report.ok
    assert any(c.kind == BREAKING and "'note' newly required" in c.message for c in report.changes)
    assert any("non-major bump" in p for p in report.problems)


def test_cs03_older_document_validates_under_newer_minor():
    """CS-03 scenario: a document produced under 1.2.0 validates with 1.3.0."""
    old, new = PAIRS / "additive-optional-field" / "old", PAIRS / "additive-optional-field" / "new"
    doc = json.loads((old / "fixtures" / "sample-record" / "valid" / "produced-under-old.json").read_text())
    assert validate(doc, "sample-record", store=SchemaStore(new)).valid
    report = compare_roots(old, new)
    assert report.ok and not [c for c in report.changes if "no longer validates" in c.message]


def test_fixture_upgrade_catches_breaking_change(tmp_path):
    """The gate replays the previous release's valid fixtures against the new schemas."""
    old = tmp_path / "old"
    shutil.copytree(PAIRS / "additive-optional-field" / "old", old)
    new = tmp_path / "new"
    shutil.copytree(PAIRS / "additive-optional-field" / "old", new, ignore=shutil.ignore_patterns("fixtures"))
    (new / "VERSION").write_text("1.3.0\n")
    path = new / "core" / "v1" / "sample-record.json"
    schema = json.loads(path.read_text())
    schema["properties"]["note"]["maxLength"] = 5  # tightening: old documents no longer validate
    path.write_text(json.dumps(schema))
    report = compare_roots(old, new)
    assert not report.ok
    assert any("produced-under-old.json no longer validates" in c.message for c in report.changes)
    assert any("'maxLength' changed" in c.message for c in report.changes)


def test_own07_breaking_change_ships_as_new_major_alongside_previous():
    """OWN-07: breaking change -> new major served alongside the previous one, plus a migration entry."""
    report = compare_roots(PAIRS / "major-v2-with-migration" / "old", PAIRS / "major-v2-with-migration" / "new")
    assert report.ok and report.bump == "major"
    migration = (PAIRS / "major-v2-with-migration" / "new" / "migrations" / "v2.yaml").read_text()
    assert "consumers:" in migration and "removal_criterion:" in migration
    in_place = compare_roots(PAIRS / "major-in-place-breaking" / "old", PAIRS / "major-in-place-breaking" / "new")
    assert any("in place" in p for p in in_place.problems)
    missing = compare_roots(PAIRS / "major-v1-removed-without-migration" / "old", PAIRS / "major-v1-removed-without-migration" / "new")
    assert any("migration record" in p for p in missing.problems)


def test_own07_additive_field_is_minor():
    report = compare_roots(PAIRS / "additive-optional-field" / "old", PAIRS / "additive-optional-field" / "new")
    assert report.ok and report.bump == "minor" and report.required_bump == "minor"
    assert [c.kind for c in report.changes] == [ADDITIVE]


def test_zero_major_rule_and_opt_in():
    old, new = PAIRS / "zero-minor-breaking" / "old", PAIRS / "zero-minor-breaking" / "new"
    assert not compare_roots(old, new).ok
    assert compare_roots(old, new, allow_zero_major_breaking=True).ok


def test_old_version_from_raw_tarball(tmp_path):
    """The previous version can be the raw schema tarball published by the build (7.2)."""
    tarball = digests.build_raw_tarball(tmp_path / "out", PAIRS / "additive-optional-field" / "old")
    report = compare_roots(tarball, PAIRS / "additive-optional-field" / "new")
    assert report.ok and report.old_version == "1.2.0"


def test_current_package_against_itself_and_first_release():
    assert compare_roots(None).ok
    report = compare_roots(CONTRACTS, CONTRACTS)
    assert report.changes == [] and report.ok


def test_error_code_registry_addition_is_minor():
    """'Adding a code is a minor release' (CS-06): the registered code list is a registry enum."""
    old = json.loads((CONTRACTS / "core" / "v1" / "error-codes.json").read_text())
    new = json.loads(json.dumps(old))
    new["$defs"]["code"]["enum"].append("QUOTA_EXHAUSTED")
    new["x-finplan-error-codes"]["QUOTA_EXHAUSTED"] = {"retryable": True, "retryable_fixed": False, "description": "x"}
    kinds = {c.kind for c in diff_schema("error-codes", old, new)}
    assert kinds == {ADDITIVE}
    new["x-finplan-error-codes"]["NOT_FOUND"]["retryable"] = True
    assert BREAKING in {c.kind for c in diff_schema("error-codes", old, new)}


def test_diff_classification_details():
    old = {"type": "object", "title": "a", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}
    assert diff_schema("s", old, dict(old, title="b"))[0].kind == ANNOTATION
    assert diff_schema("s", old, dict(old, required=[]))[0].kind == BREAKING  # required made optional
    assert diff_schema("s", old, dict(old, additionalProperties=True))[0].kind == BREAKING
    new = json.loads(json.dumps(old))
    new["properties"]["y"] = {"type": "integer"}
    new["required"] = ["x", "y"]
    assert {c.kind for c in diff_schema("s", old, new)} == {BREAKING}


@pytest.mark.parametrize(
    ("old", "new", "bump"),
    [("1.2.0", "1.3.0", "minor"), ("1.2.0", "1.2.1", "patch"), ("1.2.0", "2.0.0", "major"), ("1.2.0", "1.2.0", "none"), ("1.2.0", "1.1.9", "downgrade"), ("1.0.0-beta.1", "1.0.0", "patch")],
)
def test_classify_bump(old, new, bump):
    assert classify_bump(old, new) == bump


def test_cli(capsys):
    case = PAIRS / "optional-made-required"
    assert compat.main(["--old", str(case / "old"), "--new", str(case / "new")]) == 1
    assert "FAIL" in capsys.readouterr().out
    case = PAIRS / "additive-optional-field"
    assert compat.main(["--old", str(case / "old"), "--new", str(case / "new"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert compat.main(["--first-release"]) == 0
    with pytest.raises(SystemExit):
        compat.main([])
