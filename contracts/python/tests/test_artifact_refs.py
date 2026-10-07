"""CS-08: trusted artifact references; s3:// and path-like inputs rejected with VALIDATION_FAILED."""

import pytest

from finplan_contracts.validate import validate

from conftest import fixture


def test_trusted_reference_fields(store):
    s = store.get("artifact-ref").schema
    assert set(s["required"]) == {"artifact_id", "owner", "kind", "checksum", "content_type"}
    assert s["additionalProperties"] is False


@pytest.mark.parametrize("name", ["s3-uri-artifact-id", "path-artifact-id", "bucket-and-key-fields"])
def test_storage_locations_rejected(name):
    res = validate(fixture(f"artifact-ref/invalid/{name}.json"), "artifact-ref")
    assert res.code == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "schema,name",
    [
        ("tools/get-plan-request", "storage-location-field"),
        ("tools/submit-experiment-request", "s3-configuration-location"),
        ("tools/create-override-version-request", "content-location-uri"),
        ("tools/query-market-data-request", "storage-uri-field"),
        ("job-submission", "s3-input-location"),
    ],
)
def test_tool_requests_reject_s3_uris(schema, name):
    assert validate(fixture(f"{schema}/invalid/{name}.json"), schema).code == "VALIDATION_FAILED"


def test_s3_uri_in_any_request_string_rejected():
    req = fixture("tools/create-override-version-request/valid/override.json")
    req["reason"] = "s3://example-bucket/override.json"
    res = validate(req, "tools/create-override-version-request")
    assert res.code == "VALIDATION_FAILED" and res.primary().pointer == "/reason"


def test_valid_references():
    for name in ("snapshot-manifest", "research-dataset", "excel-source"):
        assert validate(fixture(f"artifact-ref/valid/{name}.json"), "artifact-ref").valid
