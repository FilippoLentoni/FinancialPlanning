"""ID-01: canonical identifier set and formats; wrong prefix -> INVALID_IDENTIFIER naming the field."""

import pytest

from finplan_contracts.validate import validate

from conftest import fixture

PREFIXES = {
    "portfolio_id": "pf_", "plan_id": "pl_", "plan_version_id": "pv_", "input_snapshot_id": "snap_", "model_version": "mv_",
    "run_id": "run_", "publication_id": "pub_", "execution_id": "exe_",
}


def test_exactly_the_nine_identifiers(store):
    root_props = set(store.get("identifiers").schema["properties"]) - {"synthetic", "release_id"}
    assert root_props == set(PREFIXES) | {"configuration_id"}


@pytest.mark.parametrize("field,prefix", PREFIXES.items())
def test_identifier_patterns(store, field, prefix):
    d = store.get("identifiers").schema["$defs"][field]
    assert d["pattern"].startswith("^" + prefix)
    assert d["x-finplan-error-code"] == "INVALID_IDENTIFIER"


def test_spec_example_is_a_valid_plan_version_id():
    assert validate({"plan_version_id": "pv_01JA2B3C4D5E6F7G8H9JKMNPQR"}, "identifiers").valid


def test_wrong_prefix_fails_with_invalid_identifier_naming_field():
    res = validate(fixture("identifiers/invalid/wrong-prefix-plan-version-id.json"), "identifiers")
    assert res.code == "INVALID_IDENTIFIER"
    assert res.primary().field == "plan_version_id"
    assert res.primary().pointer == "/plan_version_id"


def test_wrong_prefix_error_envelope_is_contract_valid():
    res = validate({"plan_version_id": "pl_01JA2B3C4D5E6F7G8H9JKMNPQR"}, "identifiers")
    env = res.to_error_envelope(correlation_id="corr-test-00000001")
    assert env["code"] == "INVALID_IDENTIFIER" and env["details"]["field"] == "plan_version_id"
    assert env["retryable"] is False
    assert validate(env, "error").valid


def test_wrong_prefix_inside_records_is_invalid_identifier():
    for rel in (
        "plan-version/invalid/parent-wrong-prefix.json",
        "publication/invalid/plan-version-id-wrong-prefix.json",
        "input-snapshot/invalid/wrong-prefix-snapshot-id.json",
        "tools/get-plan-version-request/invalid/wrong-prefix.json",
    ):
        schema = rel.split("/invalid/")[0]
        assert validate(fixture(rel), schema).code == "INVALID_IDENTIFIER", rel


def test_configuration_id_is_cfg_plus_64_lowercase_hex():
    ok = "cfg_" + "0123456789abcdef" * 4
    assert validate({"configuration_id": ok}, "identifiers").valid
    assert validate({"configuration_id": ok.upper().replace("CFG_", "cfg_")}, "identifiers").code == "INVALID_IDENTIFIER"
    assert validate({"configuration_id": ok[:-1]}, "identifiers").code == "INVALID_IDENTIFIER"


def test_non_identifier_problems_are_validation_failed():
    assert validate({"plan_ver_id": "pv_01JA2B3C4D5E6F7G8H9JKMNPQR"}, "identifiers").code == "VALIDATION_FAILED"
