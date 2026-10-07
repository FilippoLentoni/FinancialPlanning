"""CS-05 (error envelope) and CS-06 (registered error codes)."""

import pytest

from finplan_contracts.validate import validate

from conftest import FIXTURES, fixture

SPEC_CODES = [
    "VALIDATION_FAILED", "INVALID_IDENTIFIER", "NOT_FOUND", "CONFLICT", "IDEMPOTENCY_KEY_REUSED", "IMMUTABLE_RECORD",
    "PRECONDITION_FAILED", "UNAUTHORIZED", "FORBIDDEN", "OPERATION_NOT_PERMITTED", "BUDGET_EXCEEDED", "RATE_LIMITED",
    "DEPENDENCY_UNAVAILABLE", "UNSUPPORTED_CONTRACT_VERSION", "INTERNAL",
]


def test_every_spec_code_registered_with_default_retryable(store):
    schema = store.get("error-codes").schema
    assert set(SPEC_CODES) <= set(schema["$defs"]["code"]["enum"])
    table = schema["x-finplan-error-codes"]
    for code in SPEC_CODES:
        assert isinstance(table[code]["retryable"], bool)
    assert table["RATE_LIMITED"]["retryable"] is True
    assert table["VALIDATION_FAILED"]["retryable"] is False
    assert table["IDEMPOTENCY_KEY_REUSED"]["retryable"] is False


@pytest.mark.parametrize("code", SPEC_CODES)
def test_fixture_per_registered_code(code):
    slug = code.lower().replace("_", "-")
    assert validate(fixture(f"error-codes/valid/{slug}.json"), "error-codes").valid
    env = fixture(f"error/valid/{slug}.json")
    assert env["code"] == code and validate(env, "error").valid


def test_envelope_has_required_fields(store):
    assert set(store.get("error").schema["required"]) == {"code", "message", "retryable", "details", "correlation_id", "contract_version"}


def test_validation_failed_names_the_json_pointer():
    assert validate(fixture("error/valid/validation-failed.json"), "error").valid
    assert not validate(fixture("error/invalid/validation-failed-without-pointer.json"), "error").valid
    assert not validate(fixture("error/invalid/validation-failed-retryable.json"), "error").valid


def test_unsupported_contract_version_lists_served_majors():
    assert fixture("error/valid/unsupported-contract-version.json")["details"]["served_contract_majors"] == [1]
    assert not validate(fixture("error/invalid/unsupported-version-without-majors.json"), "error").valid


def test_no_stack_traces_or_storage_locations():
    for name in ("stack-trace-field", "traceback-in-message", "storage-uri-in-details"):
        assert not validate(fixture(f"error/invalid/{name}.json"), "error").valid, name


def test_unregistered_code_rejected():
    assert not validate(fixture("error/invalid/unregistered-code.json"), "error").valid


def test_validator_messages_never_echo_storage_locations():
    bad = fixture("artifact-ref/invalid/s3-uri-artifact-id.json")
    res = validate(bad, "artifact-ref")
    env = res.to_error_envelope("corr-test-00000003")
    assert "s3://" not in str(env)
    assert validate(env, "error").valid


def test_all_error_fixture_files_counted():
    assert len(list((FIXTURES / "error" / "valid").glob("*.json"))) >= len(SPEC_CODES)
