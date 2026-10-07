"""Write-once artifact storage (platform-storage; tasks 2.3, 2.5; STO-04, STO-07).

:class:`ArtifactStore` is the only way platform code writes or reads bucket objects.
It is constructed with an injected S3 client (boto3 in Lambda, moto or a fake in tests)
and the per-environment bucket names, which handlers resolve from the
``/finplan/<env>/financialplanning/config/bucket-*`` parameters (never from literals).

Guarantees:

* :meth:`put_once` is a conditional create (``If-None-Match: *``) with SSE-KMS under the
  environment key and an S3-verified SHA-256 checksum. A second write to the same key
  fails with :class:`ArtifactExists` and leaves the stored bytes and checksum unchanged.
  :meth:`put_once_or_verify` is the retry-safe variant: an identical retry returns the
  existing artifact, different bytes still fail.
* Every artifact records its checksum (``sha256:<hex>``) in object metadata; :meth:`get`
  re-hashes the bytes and refuses a mismatch (``INTERNAL``), so a reader never trusts a
  corrupt artifact.
* Responses outside the platform never see bucket names or keys: callers turn a
  :class:`StoredArtifact` into a trusted reference with :meth:`StoredArtifact.to_ref`.
  Object version IDs are never returned (STO-05).
* Download grants (:meth:`download_grant`) and Excel upload grants
  (:meth:`excel_upload_grant`) are time-limited presigned requests. The upload grant is a
  presigned POST with a ``content-length-range`` condition, an exact key under
  ``uploads/incoming/`` and an expiry of at most 15 minutes; :func:`check_upload_grant`
  refuses an expired grant on the platform side as well (S3 refuses it server-side).

Key layout (design P2) is built by the ``key_*`` helpers so every module uses the same
layout.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping

from botocore.exceptions import ClientError

from .clock import Clock, SystemClock, parse_timestamp, to_timestamp
from .config import BUCKET_ROLES
from .errors import PlatformError

__all__ = [
    "ArtifactStore",
    "StoredArtifact",
    "ArtifactExists",
    "ArtifactNotFound",
    "UploadGrant",
    "check_upload_grant",
    "sha256_checksum",
    "WRITE_ONCE_PREFIXES",
    "SNAPSHOT_STATUS_TAG",
    "key_plan_content",
    "key_plan_export",
    "key_snapshot_manifest",
    "key_snapshot_payload",
    "key_staging_prefix",
    "key_accepted",
    "key_report",
    "key_excel_source",
    "key_excel_incoming",
    "key_raw_provider",
    "key_curated",
]

#: Object tag mirroring the snapshot catalog status; the FinanceModel read grant requires ``approved``.
SNAPSHOT_STATUS_TAG = "snapshot-status"
#: Bucket role -> key prefixes that are write-once (``""`` = whole bucket). Mirrored by the bucket policies.
WRITE_ONCE_PREFIXES: dict[str, tuple[str, ...]] = {
    "snapshots": ("",),
    "plans": ("",),
    "reports": ("",),
    "outputs": ("accepted/",),
    "raw": ("uploads/excel/",),
}
_SHA_PREFIX = "sha256:"
_META_SHA = "sha256"


def sha256_checksum(data: bytes) -> str:
    return _SHA_PREFIX + hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ key layout
def key_plan_content(plan_id: str, plan_version_id: str) -> str:
    return f"{plan_id}/{plan_version_id}/content.json"


def key_plan_export(plan_id: str, plan_version_id: str, template_version: str) -> str:
    return f"{plan_id}/{plan_version_id}/export-{template_version}.xlsx"


def key_snapshot_manifest(input_snapshot_id: str) -> str:
    return f"{input_snapshot_id}/manifest.json"


def key_snapshot_payload(input_snapshot_id: str, name: str) -> str:
    return f"{input_snapshot_id}/payload/{name}"


def key_staging_prefix(run_id: str) -> str:
    return f"staging/{run_id}/"


def key_accepted(run_id: str, name: str) -> str:
    return f"accepted/{run_id}/{name}"


def key_report(record_id: str, artifact_id: str) -> str:
    return f"{record_id}/{artifact_id}"


def key_excel_source(sha256_hex: str) -> str:
    return f"uploads/excel/{sha256_hex}.xlsx"


def key_excel_incoming(import_id: str) -> str:
    return f"uploads/incoming/{import_id}.xlsx"


def key_raw_provider(provider_id: str, dataset: str, retrieved_at: str, ulid: str) -> str:
    return f"provider/{provider_id}/{dataset}/{retrieved_at}/{ulid}.json"


def key_curated(dataset: str, instrument: str, session_date: str, kind: str, source_ts: str) -> str:
    return f"{dataset}/{instrument}/{session_date}/{kind}/{source_ts}.json"


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class StoredArtifact:
    """A stored object as the platform sees it (internal: carries the key, never exposed)."""

    bucket_role: str
    key: str
    checksum: str
    size_bytes: int
    content_type: str
    created: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def to_ref(self, artifact_id: str, kind: str, *, synthetic: bool | None = None, domain: str | None = None) -> dict[str, Any]:
        """Contract ``core/v1/artifact-ref.json`` trusted reference (no bucket, key or version ID)."""
        ref: dict[str, Any] = {
            "artifact_id": artifact_id,
            "owner": "financialplanning",
            "kind": kind,
            "checksum": self.checksum,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }
        if domain is not None:
            ref["domain"] = domain
        if synthetic is not None:
            ref["synthetic"] = synthetic
        return ref


class ArtifactExists(PlatformError):
    """A write-once key already exists (STO-04). Code ``IMMUTABLE_RECORD``; no location in details."""

    def __init__(self, bucket_role: str, existing_checksum: str | None) -> None:
        super().__init__("IMMUTABLE_RECORD", "artifact already exists and is immutable", bucket_role=bucket_role)
        self.existing_checksum = existing_checksum
        self.bucket_role = bucket_role


class ArtifactNotFound(PlatformError):
    def __init__(self, bucket_role: str) -> None:
        super().__init__("NOT_FOUND", "artifact not found", bucket_role=bucket_role)


@dataclass(frozen=True)
class UploadGrant:
    """A presigned POST upload grant (STO-07). ``url``/``fields`` go to the client; the rest stays internal."""

    import_id: str
    url: str
    fields: Mapping[str, str]
    key: str
    issued_at: str
    expires_at: str
    max_bytes: int

    def client_view(self) -> dict[str, Any]:
        """What the API returns: the POST target and form fields plus the expiry (no bucket/key fields beyond the signed form)."""
        return {"import_id": self.import_id, "upload": {"url": self.url, "fields": dict(self.fields)}, "expires_at": self.expires_at, "max_bytes": self.max_bytes}

    def is_expired(self, now: datetime) -> bool:
        return now >= parse_timestamp(self.expires_at)


def check_upload_grant(expires_at: str, clock: Clock) -> None:
    """Refuse work on an expired upload grant (``PRECONDITION_FAILED`` ``upload_grant_expired``)."""
    if clock.now() >= parse_timestamp(expires_at):
        raise PlatformError.precondition("the upload grant has expired; request a new one", reason="upload_grant_expired", expired_at=expires_at)


# ------------------------------------------------------------------ store
def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)


_PRECONDITION_CODES = {"PreconditionFailed", "ConditionalRequestConflict"}
_NOT_FOUND_CODES = {"NoSuchKey", "404", "NotFound"}


class ArtifactStore:
    def __init__(self, s3_client: Any, buckets: Mapping[str, str], *, kms_key_id: str | None = None, clock: Clock | None = None) -> None:
        unknown = set(buckets) - set(BUCKET_ROLES)
        if unknown:
            raise ValueError(f"unknown bucket roles: {sorted(unknown)}")
        self._s3 = s3_client
        self._buckets = dict(buckets)
        self._kms_key_id = kms_key_id
        self._clock = clock or SystemClock()

    def bucket(self, role: str) -> str:
        try:
            return self._buckets[role]
        except KeyError:
            raise ValueError(f"bucket role {role!r} is not configured") from None

    # -------------------------------------------------------------- writes
    def _sse(self) -> dict[str, Any]:
        """Explicit SSE-KMS with the environment key, or nothing (the bucket default is that key).

        Never ``aws:kms`` without a key ID: S3 would then use the AWS-managed key, which the
        bucket policy's ``DenyOtherKmsKey`` cannot see and the platform does not want.
        """
        if self._kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id, "BucketKeyEnabled": True}
        return {}

    def put_once(
        self,
        role: str,
        key: str,
        body: bytes,
        content_type: str = "application/json",
        *,
        metadata: Mapping[str, str] | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> StoredArtifact:
        """Conditional create. Raises :class:`ArtifactExists` when the key exists (stored bytes unchanged)."""
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("artifact body must be bytes")
        digest = hashlib.sha256(body).digest()
        checksum = _SHA_PREFIX + digest.hex()
        meta = {**(metadata or {}), _META_SHA: digest.hex()}
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket(role),
            "Key": key,
            "Body": bytes(body),
            "ContentType": content_type,
            "IfNoneMatch": "*",
            "ChecksumSHA256": base64.b64encode(digest).decode("ascii"),
            "Metadata": meta,
            **self._sse(),
        }
        if tags:
            from urllib.parse import urlencode

            kwargs["Tagging"] = urlencode(dict(tags))
        try:
            self._s3.put_object(**kwargs)
        except ClientError as exc:
            if _code(exc) in _PRECONDITION_CODES or _status(exc) in (409, 412):
                existing = self.head(role, key)
                raise ArtifactExists(role, existing.checksum if existing else None) from None
            raise
        return StoredArtifact(role, key, checksum, len(body), content_type, True, meta)

    def put_once_or_verify(self, role: str, key: str, body: bytes, content_type: str = "application/json", **kwargs: Any) -> StoredArtifact:
        """Retry-safe create: identical bytes already stored -> the existing artifact (``created`` False)."""
        try:
            return self.put_once(role, key, body, content_type, **kwargs)
        except ArtifactExists as exc:
            if exc.existing_checksum == sha256_checksum(bytes(body)):
                existing = self.head(role, key)
                assert existing is not None
                return StoredArtifact(role, key, existing.checksum, existing.size_bytes, existing.content_type, False, existing.metadata)
            raise

    def put_tags(self, role: str, key: str, tags: Mapping[str, str]) -> None:
        """Replace an object's tags (snapshot approval mirror). Tags are not artifact content."""
        self._s3.put_object_tagging(Bucket=self.bucket(role), Key=key, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})

    def get_tags(self, role: str, key: str) -> dict[str, str]:
        resp = self._s3.get_object_tagging(Bucket=self.bucket(role), Key=key)
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}

    def delete(self, role: str, key: str) -> None:
        """Only the orphan sweep calls this (bucket policy denies every other principal)."""
        self._s3.delete_object(Bucket=self.bucket(role), Key=key)

    # -------------------------------------------------------------- reads
    def head(self, role: str, key: str) -> StoredArtifact | None:
        try:
            resp = self._s3.head_object(Bucket=self.bucket(role), Key=key)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES or _status(exc) == 404:
                return None
            raise
        meta = {k.lower(): v for k, v in (resp.get("Metadata") or {}).items()}
        sha = meta.get(_META_SHA)
        return StoredArtifact(role, key, _SHA_PREFIX + sha if sha else "", int(resp.get("ContentLength", 0)), resp.get("ContentType", "application/octet-stream"), False, meta)

    def exists(self, role: str, key: str) -> bool:
        return self.head(role, key) is not None

    def get(self, role: str, key: str, *, expected_checksum: str | None = None) -> tuple[bytes, StoredArtifact]:
        """Read and verify. Raises :class:`ArtifactNotFound`, or ``INTERNAL`` on a checksum mismatch."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket(role), Key=key)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES or _status(exc) == 404:
                raise ArtifactNotFound(role) from None
            raise
        data = resp["Body"].read()
        meta = {k.lower(): v for k, v in (resp.get("Metadata") or {}).items()}
        actual = sha256_checksum(data)
        recorded = _SHA_PREFIX + meta[_META_SHA] if meta.get(_META_SHA) else None
        if (recorded and recorded != actual) or (expected_checksum and expected_checksum != actual):
            raise PlatformError.internal("stored artifact failed checksum verification", bucket_role=role)
        return data, StoredArtifact(role, key, actual, len(data), resp.get("ContentType", "application/octet-stream"), False, meta)

    def list_objects(self, role: str, prefix: str = "") -> Iterator[tuple[str, datetime]]:
        """(key, last_modified) for every current object under ``prefix``."""
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket(role), "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                yield obj["Key"], obj["LastModified"]
            if not resp.get("IsTruncated"):
                return
            token = resp.get("NextContinuationToken")

    # -------------------------------------------------------------- grants
    def download_grant(self, role: str, key: str, ttl_seconds: int) -> dict[str, str]:
        """Time-limited download (presigned GET). The URL is the only location the client sees."""
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("download grant TTL must be between 1 s and 1 h")
        url = self._s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket(role), "Key": key}, ExpiresIn=ttl_seconds)
        expires = to_timestamp(self._clock.now() + timedelta(seconds=ttl_seconds))
        return {"url": url, "expires_at": expires}

    def excel_upload_grant(self, import_id: str, *, max_bytes: int, ttl_seconds: int = 900, min_bytes: int = 1) -> UploadGrant:
        """Presigned POST into ``raw/uploads/incoming/<import_id>.xlsx`` (STO-07, design P8).

        Conditions: exact key, ``content-length-range`` [min_bytes, max_bytes], the
        spreadsheet content type, and SSE-KMS with the environment key when it is known
        (otherwise the bucket default, which is that key). ``ttl_seconds`` is capped at
        900 (15 min).
        """
        if ttl_seconds > 900:
            raise ValueError("upload grants expire after at most 15 minutes")
        key = key_excel_incoming(import_id)
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fields: dict[str, str] = {"Content-Type": ctype}
        conditions: list[Any] = [
            {"bucket": self.bucket("raw")},
            {"key": key},
            ["content-length-range", int(min_bytes), int(max_bytes)],
            {"Content-Type": ctype},
        ]
        if self._kms_key_id:
            fields["x-amz-server-side-encryption"] = "aws:kms"
            fields["x-amz-server-side-encryption-aws-kms-key-id"] = self._kms_key_id
            conditions.append({"x-amz-server-side-encryption": "aws:kms"})
            conditions.append({"x-amz-server-side-encryption-aws-kms-key-id": self._kms_key_id})
        post = self._s3.generate_presigned_post(Bucket=self.bucket("raw"), Key=key, Fields=fields, Conditions=conditions, ExpiresIn=ttl_seconds)
        now = self._clock.now()
        return UploadGrant(
            import_id=import_id,
            url=post["url"],
            fields=post["fields"],
            key=key,
            issued_at=to_timestamp(now),
            expires_at=to_timestamp(now + timedelta(seconds=ttl_seconds)),
            max_bytes=int(max_bytes),
        )
