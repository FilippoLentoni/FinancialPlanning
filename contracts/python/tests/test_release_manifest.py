"""ENV-06 (release manifest schema), with the D3/D4 extra fields."""

from finplan_contracts.validate import validate

from conftest import fixture

SPEC_FIELDS = {"repo", "environment", "region", "release_id", "source_commit", "artifact_digest", "contract_version", "deployed_at", "previous_release_id", "outputs"}


def test_manifest_fields(store):
    s = store.get("release-manifest").schema
    assert SPEC_FIELDS <= set(s["required"])
    assert {"served_contract_majors", "approved_by", "approved_at", "rolled_back_from"} <= set(s["properties"])


def test_gamma_manifest_records_release_contract_and_previous_release():
    m = fixture("release-manifest/valid/gamma-financelambdastool.json")
    assert validate(m, "release-manifest").valid
    assert m["release_id"].startswith("rel_") and m["previous_release_id"].startswith("rel_") and m["contract_version"]
    assert m["outputs"]["tool-catalog"] == "/finplan/gamma/financelambdastool/contract/tool-catalog"


def test_prod_requires_recorded_approval():
    assert validate(fixture("release-manifest/valid/prod-financialplanning-approved.json"), "release-manifest").valid
    assert not validate(fixture("release-manifest/invalid/prod-without-approval.json"), "release-manifest").valid


def test_rollback_records_source():
    m = fixture("release-manifest/valid/prod-rollback.json")
    assert m["rolled_back_from"] and validate(m, "release-manifest").valid


def test_outputs_follow_ssm_convention_and_own_segment():
    for name in ("output-in-other-repo-segment", "output-in-other-environment", "output-unknown-category"):
        assert not validate(fixture(f"release-manifest/invalid/{name}.json"), "release-manifest").valid, name


def test_release_id_prefix():
    assert validate(fixture("release-manifest/invalid/release-id-wrong-prefix.json"), "release-manifest").code == "INVALID_IDENTIFIER"
