"""CS-02 inventory, CS-09 synthetic flags, CS-10 producer/consumer runs of the suite."""

import json
import shutil

import pytest

from finplan_contracts import conformance
from finplan_contracts.schemas import SchemaStore

from conftest import CONTRACTS


@pytest.fixture()
def tmp_root(tmp_path):
    root = tmp_path / "contracts"
    root.mkdir()
    for part in ("VERSION", "core", "finance", "domains", "fixtures", "conformance"):
        src = CONTRACTS / part
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / part)
    return root


def test_producer_suite_passes():
    report = conformance.run_producer()
    assert report.ok, [str(p) for p in report.problems]
    assert report.fixtures_checked >= 300 and report.schemas_checked == len(SchemaStore())


def test_inventory_covers_spec_list_and_tools(store):
    assert conformance.check_inventory(store) == []
    for name in conformance.REQUIRED_SCHEMAS:
        assert name in store
    assert len(conformance.PUBLISHED_TOOLS) == 13


def test_inventory_fails_when_invalid_fixture_missing(tmp_root):
    shutil.rmtree(tmp_root / "fixtures" / "caller" / "invalid")
    problems = conformance.check_inventory(SchemaStore(tmp_root))
    assert any(p.check == "CS-02" and p.subject == "caller" and "invalid" in p.message for p in problems)


def test_inventory_fails_when_required_schema_missing(tmp_root):
    (tmp_root / "core" / "v1" / "staged-output-manifest.json").unlink()
    problems = conformance.check_inventory(SchemaStore(tmp_root))
    assert any(p.subject == "staged-output-manifest" and "required schema missing" in p.message for p in problems)
    assert any(p.subject == "staged-output-manifest" and "no matching schema" in p.message for p in problems)


def test_inventory_fails_when_tool_schema_missing(tmp_root):
    (tmp_root / "core" / "v1" / "tools" / "get-plan-response.json").unlink()
    shutil.rmtree(tmp_root / "fixtures" / "tools" / "get-plan-response")
    problems = conformance.check_inventory(SchemaStore(tmp_root))
    assert any("get_plan" in p.message and "response" in p.message for p in problems)


def test_suite_detects_valid_fixture_that_fails(tmp_root):
    path = tmp_root / "fixtures" / "execution" / "valid" / "paper.json"
    doc = json.loads(path.read_text())
    doc["mode"] = "live"
    path.write_text(json.dumps(doc))
    report = conformance.run_suite(tmp_root)
    assert any(p.subject == "execution/valid/paper.json" for p in report.problems)


def test_suite_detects_wrong_error_code(tmp_root):
    path = tmp_root / "fixtures" / "execution" / "invalid" / "live-mode.json"
    doc = json.loads(path.read_text())
    doc["mode"] = "paper"
    doc["publication_id"] = "pl_01JA2B3C4D5E6F7G8H9JKMNPQR"
    path.write_text(json.dumps(doc))
    report = conformance.run_suite(tmp_root)
    assert any("expected error code OPERATION_NOT_PERMITTED" in p.message for p in report.problems)


def test_synthetic_flag_hygiene(tmp_root):
    assert conformance.check_synthetic_flags(tmp_root) == []
    path = tmp_root / "fixtures" / "caller" / "valid" / "hosted-agent.json"
    doc = json.loads(path.read_text())
    del doc["synthetic"]
    path.write_text(json.dumps(doc))
    problems = conformance.check_synthetic_flags(tmp_root)
    assert [p.subject for p in problems] == ["caller/valid/hosted-agent.json"]


def test_synthetic_exceptions_are_listed_in_readme():
    exc = conformance.synthetic_exceptions(CONTRACTS)
    assert "budget-allocation/valid/defaults.json" in exc
    assert all((CONTRACTS / "fixtures" / e).is_file() for e in exc)


def test_extra_fixture_check_hook(monkeypatch, tmp_root):
    calls = []

    def extra(root, files):
        calls.append(len(files))
        return [conformance.Problem("CS-09", "x", "flagged")]

    monkeypatch.setattr(conformance, "EXTRA_FIXTURE_CHECKS", [extra])
    report = conformance.run_suite(tmp_root)
    assert calls and any(p.message == "flagged" for p in report.problems)


def test_consumer_mode_runs_same_suite_and_validates_documents(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(CONTRACTS / "fixtures" / "tools" / "get-plan-request" / "valid" / "by-plan-id.json", docs / "ok.json")
    report = conformance.run_consumer(CONTRACTS, documents=docs, schema="tools/get-plan-request")
    assert report.ok and report.mode == "consumer"
    (docs / "bad.json").write_text(json.dumps({"plan_id": "pv_01JA2B3C4D5E6F7G8H9JKMNPQR"}))
    assert not conformance.run_consumer(CONTRACTS, documents=docs, schema="tools/get-plan-request").ok


def test_cases_manifest_covers_every_fixture():
    cases = conformance.load_cases(CONTRACTS)
    files = {p.relative_to(CONTRACTS / "fixtures").as_posix() for p in conformance.fixture_files(CONTRACTS) if not p.parts[-2] == "vectors"}
    assert files == set(cases)


def test_main_returns_zero(capsys):
    assert conformance.main([]) == 0
    assert "PASS" in capsys.readouterr().out
    assert conformance.main(["--json"]) == 0
