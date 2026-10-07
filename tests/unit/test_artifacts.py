"""Write-once artifact writer (STO-04), grants (STO-07), no version IDs (STO-05)."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import pytest
from finplan_contracts.validate import validate

from finplan_platform.core.artifacts import (
    ArtifactExists,
    ArtifactStore,
    check_upload_grant,
    key_plan_content,
    key_snapshot_manifest,
    sha256_checksum,
)
from finplan_platform.core.errors import PlatformError


def test_put_once_returns_sha256_and_reads_back(artifacts: ArtifactStore) -> None:
    body = b'{"synthetic":true}'
    stored = artifacts.put_once("plans", key_plan_content("pl_A", "pv_A"), body)
    assert stored.checksum == sha256_checksum(body) and stored.created
    data, again = artifacts.get("plans", key_plan_content("pl_A", "pv_A"))
    assert data == body and again.checksum == stored.checksum


def test_second_write_fails_and_stored_checksum_is_unchanged(artifacts: ArtifactStore) -> None:
    """STO-04: overwrite attempt on snapshots/plans fails; the stored artifact is unchanged."""
    for role, key in (("snapshots", key_snapshot_manifest("snap_A")), ("plans", key_plan_content("pl_A", "pv_A"))):
        first = artifacts.put_once(role, key, b"original")
        with pytest.raises(ArtifactExists) as ei:
            artifacts.put_once(role, key, b"tampered")
        assert ei.value.code == "IMMUTABLE_RECORD"
        assert ei.value.existing_checksum == first.checksum
        data, stored = artifacts.get(role, key)
        assert data == b"original" and stored.checksum == first.checksum
        # the error envelope carries no bucket name or key
        env = ei.value.to_envelope("cor_test00000001")
        assert key not in json.dumps(env) and validate(env, "error").valid


def test_identical_retry_is_accepted_but_different_bytes_are_not(artifacts: ArtifactStore) -> None:
    key = key_plan_content("pl_B", "pv_B")
    artifacts.put_once("plans", key, b"same")
    again = artifacts.put_once_or_verify("plans", key, b"same")
    assert not again.created and again.checksum == sha256_checksum(b"same")
    with pytest.raises(ArtifactExists):
        artifacts.put_once_or_verify("plans", key, b"other")


def test_reader_detects_corruption(artifacts: ArtifactStore, s3: Any, buckets: dict[str, str]) -> None:
    key = key_plan_content("pl_C", "pv_C")
    s3.put_object(Bucket=buckets["plans"], Key=key, Body=b"bytes", Metadata={"sha256": "0" * 64})
    with pytest.raises(PlatformError) as ei:
        artifacts.get("plans", key)
    assert ei.value.code == "INTERNAL"


def test_trusted_ref_has_no_location_or_version(artifacts: ArtifactStore, s3: Any, buckets: dict[str, str]) -> None:
    """STO-05: references carry IDs and checksums only (versioning is protection, not identity)."""
    s3.put_bucket_versioning(Bucket=buckets["plans"], VersioningConfiguration={"Status": "Enabled"})
    stored = artifacts.put_once("plans", key_plan_content("pl_D", "pv_D"), b"{}")
    ref = stored.to_ref("art_01KDVDNYG8A6KFTN7C0C2W0YH8", "plan_content", synthetic=True)
    assert validate(ref, "artifact-ref").valid
    text = json.dumps(ref)
    assert "VersionId" not in text and "version_id" not in text and buckets["plans"] not in text and "pl_D/" not in text


def test_sse_kms_with_environment_key(s3: Any, buckets: dict[str, str], kms_key_id: str) -> None:
    store = ArtifactStore(s3, buckets, kms_key_id=kms_key_id)
    store.put_once("reports", "rec_1/art_1", b"report")
    head = s3.head_object(Bucket=buckets["reports"], Key="rec_1/art_1")
    assert head["ServerSideEncryption"] == "aws:kms" and head["SSEKMSKeyId"] == kms_key_id


# ------------------------------------------------------------------ STO-07
def test_excel_upload_grant_is_size_limited_and_expires_in_15_minutes(artifacts: ArtifactStore, buckets: dict[str, str]) -> None:
    grant = artifacts.excel_upload_grant("imp_01KDVDNAZ83BAMMYCEGWF33DPM", max_bytes=5 * 1024 * 1024)
    policy = json.loads(base64.b64decode(grant.fields["policy"]))
    conds = policy["conditions"]
    assert ["content-length-range", 1, 5 * 1024 * 1024] in conds
    assert {"key": "uploads/incoming/imp_01KDVDNAZ83BAMMYCEGWF33DPM.xlsx"} in conds
    assert {"bucket": buckets["raw"]} in conds
    assert grant.key.startswith("uploads/incoming/")
    from finplan_platform.core.clock import parse_timestamp

    assert parse_timestamp(grant.expires_at) - parse_timestamp(grant.issued_at) == timedelta(minutes=15)
    view = grant.client_view()
    assert view["expires_at"] == grant.expires_at and "key" not in view
    with pytest.raises(ValueError):
        artifacts.excel_upload_grant("imp_X", max_bytes=10, ttl_seconds=901)


def test_expired_upload_grant_is_refused(artifacts: ArtifactStore, clock: Any) -> None:
    grant = artifacts.excel_upload_grant("imp_01KDVDNAZ83BAMMYCEGWF33DPM", max_bytes=1024)
    check_upload_grant(grant.expires_at, clock)  # still valid
    clock.advance(minutes=15)
    assert grant.is_expired(clock.now())
    with pytest.raises(PlatformError) as ei:
        check_upload_grant(grant.expires_at, clock)
    assert ei.value.code == "PRECONDITION_FAILED" and ei.value.details["reason"] == "upload_grant_expired"


def test_download_grant_is_time_limited(artifacts: ArtifactStore) -> None:
    artifacts.put_once("plans", key_plan_content("pl_E", "pv_E"), b"{}")
    g = artifacts.download_grant("plans", key_plan_content("pl_E", "pv_E"), 300)
    assert "X-Amz-Expires=300" in g["url"] or "Expires=" in g["url"]
    with pytest.raises(ValueError):
        artifacts.download_grant("plans", key_plan_content("pl_E", "pv_E"), 7200)
