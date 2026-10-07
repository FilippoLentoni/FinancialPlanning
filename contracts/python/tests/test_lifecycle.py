"""ID-05..ID-09: snapshot identity, plan-version lineage/status, publication, execution."""

import copy

from finplan_contracts.validate import validate

from conftest import fixture


# ---------------------------------------------------------------- ID-05
def test_snapshot_metadata_carries_identity_fields(store):
    required = set(store.get("input-snapshot").schema["required"])
    assert {"manifest_checksum", "source_timestamps", "lineage", "coverage", "quality_flags", "dataset", "domain", "status"} <= required
    snap = fixture("input-snapshot/valid/approved-etf-daily.json")
    assert validate(snap, "input-snapshot").valid
    assert snap["lineage"]["retrieved_at"] and snap["coverage"]["start"] and snap["coverage"]["end"]


def test_snapshot_provider_lineage_fields_are_optional():
    """14.3: provider_library and library_version are optional, next to the retrieval timestamp."""
    snap = fixture("input-snapshot/valid/approved-etf-daily.json")
    assert {"provider_library", "library_version", "retrieved_at"} <= set(snap["lineage"])
    assert validate(fixture("input-snapshot/valid/schema-upgrade-without-provider-lineage-fields.json"), "input-snapshot").valid
    assert not validate(fixture("input-snapshot/invalid/lineage-missing-retrieved-at.json"), "input-snapshot").valid


def test_snapshot_without_manifest_checksum_fails():
    assert validate(fixture("input-snapshot/invalid/missing-manifest-checksum.json"), "input-snapshot").code == "VALIDATION_FAILED"


def test_no_object_store_versioning_identity(store):
    props = set(store.get("input-snapshot").schema["properties"]) | set(store.get("plan-version").schema["properties"])
    assert not {p for p in props if "version_id" in p and p.startswith(("s3", "object"))}


# ---------------------------------------------------------------- ID-06
def test_lineage_root_override_and_excel_children():
    for name in ("root-model-run", "manual-override-child", "excel-import-child", "no-effect-override"):
        assert validate(fixture(f"plan-version/valid/{name}.json"), "plan-version").valid, name
    root = fixture("plan-version/valid/root-model-run.json")
    child = fixture("plan-version/valid/manual-override-child.json")
    excel = fixture("plan-version/valid/excel-import-child.json")
    assert root["parent_plan_version_id"] is None and root["origin"] == "model_run" and root["run_id"]
    assert child["parent_plan_version_id"] == root["plan_version_id"] and child["run_id"] is None
    assert excel["origin"] == "excel_import" and excel["source_artifact"]["kind"] == "excel_source"


def test_no_effect_override_checksum_equals_parent():
    root = fixture("plan-version/valid/root-model-run.json")
    no_effect = fixture("plan-version/valid/no-effect-override.json")
    assert no_effect["parent_plan_version_id"] == root["plan_version_id"]
    assert no_effect["checksum"] == root["checksum"] and no_effect["no_effect"] is True


def test_lineage_rules_enforced():
    cases = {
        "override-without-parent": "INVALID_IDENTIFIER",
        "manual-override-with-run-id": "VALIDATION_FAILED",
        "model-run-without-run-id": "INVALID_IDENTIFIER",
        "excel-import-without-source-artifact": "VALIDATION_FAILED",
        "excel-import-wrong-source-kind": "VALIDATION_FAILED",
        "unknown-origin": "VALIDATION_FAILED",
    }
    for name, code in cases.items():
        assert validate(fixture(f"plan-version/invalid/{name}.json"), "plan-version").code == code, name


def test_override_tool_request_cannot_supply_plan_version_id():
    """ID-03: a client-supplied plan_version_id is rejected with VALIDATION_FAILED."""
    res = validate(fixture("tools/create-override-version-request/invalid/client-supplied-plan-version-id.json"), "tools/create-override-version-request")
    assert res.code == "VALIDATION_FAILED" and res.primary().field == "plan_version_id"


# ---------------------------------------------------------------- ID-07
def test_status_enum_and_invalid_requires_errors(store):
    assert store.get("plan-version").schema["$defs"]["status"]["enum"] == ["pending_validation", "validated", "invalid"]
    assert validate(fixture("plan-version/valid/invalid-partial-output.json"), "plan-version").valid
    assert not validate(fixture("plan-version/invalid/invalid-status-without-errors.json"), "plan-version").valid


# ---------------------------------------------------------------- ID-08
def test_publication_references_one_validated_version_with_checksum():
    pub = fixture("publication/valid/publication.json")
    assert validate(pub, "publication").valid
    assert pub["plan_version_id"].startswith("pv_") and pub["plan_version_checksum"].startswith("sha256:")
    assert not validate(fixture("publication/invalid/unvalidated-version-status.json"), "publication").valid
    assert not validate(fixture("publication/invalid/missing-checksum.json"), "publication").valid


def test_superseding_publication_points_to_earlier_one():
    a = fixture("publication/valid/publication.json")
    b = fixture("publication/valid/superseding-publication.json")
    assert b["supersedes_publication_id"] == a["publication_id"] and validate(b, "publication").valid


# ---------------------------------------------------------------- ID-09
def test_paper_and_simulated_executions_are_valid():
    assert validate(fixture("execution/valid/paper.json"), "execution").valid
    assert validate(fixture("execution/valid/simulated.json"), "execution").valid


def test_live_execution_mode_rejected_operation_not_permitted():
    res = validate(fixture("execution/invalid/live-mode.json"), "execution")
    assert not res.valid and res.code == "OPERATION_NOT_PERMITTED"
    env = res.to_error_envelope("corr-test-00000002")
    assert env["code"] == "OPERATION_NOT_PERMITTED" and validate(env, "error").valid


def test_any_non_phase1_mode_rejected():
    exe = fixture("execution/valid/paper.json")
    for mode in ("live", "LIVE", "real", ""):
        bad = copy.deepcopy(exe)
        bad["mode"] = mode
        assert validate(bad, "execution").code == "OPERATION_NOT_PERMITTED", mode


def test_publish_tool_accepts_no_execution_fields():
    for name in ("mode-live", "execute-flag"):
        res = validate(fixture(f"tools/publish-plan-version-request/invalid/{name}.json"), "tools/publish-plan-version-request")
        assert res.code == "VALIDATION_FAILED", name
