"""Contracts 1.1.0 (change add-research-universe-and-daily-loop): CON-01 (schemas, fixtures and the
production-strategy ownership rows) and CON-02 (compatibility gate against 1.0.0 without the 0.x
exemption; the 1.0.0 fixtures keep their bytes and validate unchanged)."""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from conftest import CONTRACTS, fixture
from finplan_contracts import compat, conformance, ssm
from finplan_contracts.ownership import Matrix
from finplan_contracts.validate import validate

BASELINE = Path(__file__).parent / "data" / "releases" / "finplan-contracts-schemas-1.0.0.tar.gz"


# ------------------------------------------------------------------ CON-01
@pytest.mark.parametrize(
    "schema",
    [
        "production-strategy",
        "tools/production-strategy-request",
        "tools/production-strategy-response",
    ],
)
def test_new_schemas_exist_with_valid_and_invalid_fixtures(store, schema: str) -> None:
    assert schema in store and store.get(schema).major == 1
    d = store.fixtures_dir(schema)
    assert any((d / "valid").glob("*.json")) and any((d / "invalid").glob("*.json"))


def test_additions_are_optional_open_enums_or_new_defs(store) -> None:
    common = store.get("common").schema["$defs"]
    kinds = common["bias_disclosures"]["items"]["properties"]["kind"]
    assert kinds["x-finplan-open-enum"] is True and set(kinds["x-finplan-known-values"]) == {"hindsight_selection", "survivorship"}
    assert "daily_recommendation" in common["job_type"]["x-finplan-known-values"]
    inst = store.get("instrument").schema
    assert set(inst["properties"]["kind"]["x-finplan-known-values"]) == {"etf", "equity", "cash"}
    assert "kind" not in inst["required"]
    for name in ("input-snapshot", "staged-output-manifest", "snapshot-payload"):
        s = store.get(name).schema
        assert "bias_disclosures" in s["properties"] and "bias_disclosures" not in s["required"], name
    assert "universe" in store.get("snapshot-payload").schema["properties"]
    assert "plan_id" not in store.get("job-submission").schema["required"]


def test_universe_snapshot_and_daily_submission_fixtures() -> None:
    snap = fixture("input-snapshot/valid/approved-universe-with-disclosures.json")
    assert snap["approval_rule_version"] == "approval-v2-universe"
    assert {d["kind"] for d in snap["bias_disclosures"]} == {"hindsight_selection", "survivorship"}
    pay = fixture("snapshot-payload/valid/equity-etf-daily-universe.json")
    cash = [i for i in pay["universe"]["instruments"] if i["kind"] == "cash"]
    assert cash == [{"instrument_id": "USD_CASH", "kind": "cash", "return_assumption": "zero_nominal"}]
    assert not any(o["instrument_id"] == "USD_CASH" for o in pay["observations"])
    sub = fixture("job-submission/valid/daily-recommendation.json")
    assert (sub["job_type"], sub["purpose"]) == ("daily_recommendation", "production_candidate")
    assert validate(sub, "job-submission").valid


def test_production_strategy_tool_is_published(store) -> None:
    assert "production_strategy" in conformance.PUBLISHED_TOOLS
    assert conformance.check_inventory(store) == []


def test_production_strategy_key_single_writer_and_reader() -> None:
    key = next(k for k in ssm.REGISTERED_KEYS if k.key == "production-strategy")
    assert (key.environment, key.repo, key.category, key.name) == ("<env>", "financemodel", "config", "production-strategy")
    assert key.writers == ("runtime",) and key.readers == (ssm.PRODUCTION_STRATEGY_READER,)
    path = ssm.production_strategy_parameter("beta")
    assert ssm.find_registered(path) is key
    assert ssm.check_write(path, ssm.Writer("financemodel", "runtime", "beta", ssm.PRODUCTION_STRATEGY_WRITER))
    # the platform (or any other runtime principal) cannot write it
    assert not ssm.check_write(path, ssm.Writer("financialplanning", "runtime", "beta", "daily-trigger"))
    assert not ssm.check_write(path, ssm.Writer("financemodel", "runtime", "beta", "job-dispatcher"))
    assert not ssm.check_write(path, ssm.Writer("financemodel", "pipeline", "beta"))
    assert not ssm.check_write(path, ssm.Writer("financemodel", "runtime", "gamma", ssm.PRODUCTION_STRATEGY_WRITER))
    assert ssm.check_read(path, "beta") and not ssm.check_read(path, "gamma")


def test_production_strategy_value_validation() -> None:
    path = ssm.production_strategy_parameter("beta")
    doc = fixture("production-strategy/valid/buy-and-hold.json")
    assert ssm.validate_value(path, json.dumps(doc)) == []
    assert ssm.validate_value(path, "") == []  # empty means no strategy
    assert ssm.validate_value(path, json.dumps({**doc, "environment": "gamma"}))
    assert ssm.validate_value(path, json.dumps({"strategy_id": "../x"}))


def test_ownership_rows_list_strategy_writer_reader_and_trigger() -> None:
    m = Matrix.load()
    row = next(r for r in m.rows if r["id"] == "production-strategy-config")
    assert row["owner"] == "financemodel" and row["iac_declared"] is False
    assert row["writer"] == f"financemodel:{ssm.PRODUCTION_STRATEGY_WRITER}"
    assert row["readers"] == [ssm.PRODUCTION_STRATEGY_READER]
    assert row["reference"] == "/finplan/<env>/financemodel/config/production-strategy"
    trig = next(r for r in m.rows if r["id"] == "daily-recommendation-trigger")
    assert trig["owner"] == "financialplanning"
    assert {"daily-trigger", "daily-trigger-role", "daily-trigger-step", "research-plan-ref"} <= set(trig["logical_roles"])
    assert "AWS::StepFunctions::StateMachine" in trig["resource_types"]


# ------------------------------------------------------------------ CON-02
def test_compat_gate_passes_as_minor_without_zero_exemption() -> None:
    report = compat.compare_roots(BASELINE, CONTRACTS, allow_zero_major_breaking=False)
    assert report.ok, [str(c) for c in report.changes if c.kind == compat.BREAKING] + report.problems
    assert report.bump == "minor"
    assert {c.kind for c in report.changes} <= {compat.ADDITIVE, compat.ANNOTATION}


def test_prior_1_3_0_fixtures_keep_their_bytes() -> None:
    """Every fixture from the immediately preceding released wheel keeps its exact bytes."""
    wheel = CONTRACTS.parent / "vendor/finplan-contracts/finplan_contracts-1.3.0-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == "ee347e88ecdd80f135a4cd79c0eec8bc98ca8744b4ec5acf9961e1ada7728638"
    with zipfile.ZipFile(wheel) as archive:
        members = [name for name in archive.namelist() if "/data/fixtures/" in name and name.endswith(".json")]
        assert len(members) > 300
        for name in members:
            rel = name.split("/data/fixtures/", 1)[1]
            assert archive.read(name) == (CONTRACTS / "fixtures" / rel).read_bytes(), rel


def test_baseline_is_1_0_0() -> None:
    with tarfile.open(BASELINE) as tf:
        ver = next(m for m in tf.getmembers() if m.name.endswith("/VERSION"))
        assert tf.extractfile(ver).read().decode().strip() == "1.0.0"  # type: ignore[union-attr]
    assert (CONTRACTS / "VERSION").read_text().strip() == "1.5.0"
