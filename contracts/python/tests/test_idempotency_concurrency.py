"""Task 2.3: idempotency and concurrency fragments; duplicate, reused-key and conflict fixtures."""

from finplan_contracts.validate import validate

from conftest import fixture


def test_idempotency_key_pattern(store):
    pat = store.get("idempotency").schema["$defs"]["idempotency_key"]["pattern"]
    assert pat == "^[A-Za-z0-9_-]{1,128}$"
    assert validate(fixture("idempotency/invalid/key-too-long.json"), "idempotency").code == "VALIDATION_FAILED"
    assert not validate(fixture("idempotency/invalid/key-bad-characters.json"), "idempotency").valid


def test_request_hash_is_sha256(store):
    d = store.get("idempotency").schema["$defs"]["request_hash"]
    assert "RFC 8785" in d["description"] and d["$ref"].endswith("common.json#/$defs/checksum")
    assert not validate(fixture("idempotency/invalid/request-hash-not-sha256.json"), "idempotency").valid


def test_duplicate_request_record_and_proxied_key_validate():
    for name in ("completed-record", "duplicate-request-replayed", "proxied-derived-key"):
        assert validate(fixture(f"idempotency/valid/{name}.json"), "idempotency").valid, name


def test_retention_at_least_seven_days():
    assert not validate(fixture("idempotency/invalid/retention-under-7-days.json"), "idempotency").valid


def test_reused_key_and_conflict_error_fixtures():
    reused = fixture("error/valid/idempotency-key-reused.json")
    conflict = fixture("error/valid/conflict.json")
    assert validate(reused, "error").valid and reused["retryable"] is False
    assert validate(conflict, "error").valid and conflict["retryable"] is False
    assert validate({**reused, "retryable": True}, "error").code == "VALIDATION_FAILED"


def test_revision_and_expected_revision(store):
    defs = store.get("concurrency").schema["$defs"]
    assert defs["revision"] == {"type": "integer", "minimum": 0, "description": defs["revision"]["description"]}
    assert defs["expected_revision"]["type"] == "integer"
    assert validate(fixture("concurrency/valid/head.json"), "concurrency").valid
    for name in ("negative-revision", "missing-revision", "string-revision"):
        assert not validate(fixture(f"concurrency/invalid/{name}.json"), "concurrency").valid


def test_write_tools_require_expected_revision_and_key():
    for schema in ("tools/create-override-version-request", "tools/publish-plan-version-request"):
        assert not validate(fixture(f"{schema}/invalid/missing-expected-revision.json"), schema).valid
