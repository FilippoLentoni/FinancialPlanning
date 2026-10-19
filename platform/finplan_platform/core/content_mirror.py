"""Write-once DynamoDB copies of immutable S3 evidence bytes.

An artifact's exact bytes are gzip-compressed (``mtime=0``, so identical bytes always
compress identically) and split into ``CONTENT_PART_BYTES`` slices, each stored as its
own item ``pk = content#<artifact_key>#<NNNN>`` in the same table as the records that
reference the artifact::

    pk            content#<artifact_key>#0000
    record_type   artifact_content
    artifact_key  <artifact_key>
    checksum      sha256:<hex of the uncompressed bytes>
    part / parts  0-based slice number / slice count
    size_bytes    uncompressed length
    encoding      gzip(RFC8785 JSON)
    data          binary gzip slice

Items are written with ``attribute_not_exists(pk)``. A retry that carries the same
checksum and slice is a no-op; anything else is ``IMMUTABLE_RECORD``. Content items carry
none of the tables' index attributes, so they never appear in a GSI.
"""
from __future__ import annotations

import gzip
from typing import Any, Iterable

from botocore.exceptions import ClientError

from .artifacts import sha256_checksum
from .errors import PlatformError

#: gzip bytes per item; with the other attributes an item stays below DynamoDB's 400 KB limit.
CONTENT_PART_BYTES = 350_000
RECORD_TYPE = "artifact_content"
ENCODING = "gzip(RFC8785 JSON)"


def content_pk(artifact_key: str, part: int) -> str:
    return f"content#{artifact_key}#{part:04d}"


def put_content(client: Any, table_name: str, artifact_key: str, checksum: str, raw: bytes, *, part_bytes: int = CONTENT_PART_BYTES, record_type: str = "artifact") -> int:
    """Write the content items of ``raw``; returns the part count. Idempotent for identical bytes."""
    packed = gzip.compress(raw, mtime=0)
    parts = [packed[i:i + part_bytes] for i in range(0, len(packed), part_bytes)] or [b""]
    for index, data in enumerate(parts):
        item = {"pk": {"S": content_pk(artifact_key, index)}, "record_type": {"S": RECORD_TYPE},
                "artifact_key": {"S": artifact_key}, "checksum": {"S": checksum}, "part": {"N": str(index)},
                "parts": {"N": str(len(parts))}, "size_bytes": {"N": str(len(raw))},
                "encoding": {"S": ENCODING}, "data": {"B": data}}
        try:
            client.put_item(TableName=table_name, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            old = client.get_item(TableName=table_name, Key={"pk": item["pk"]}, ConsistentRead=True).get("Item") or {}
            if old.get("checksum", {}).get("S") != checksum or bytes(old.get("data", {}).get("B", b"")) != data:
                raise PlatformError.immutable("DynamoDB artifact content conflicts with retry", record_type=record_type) from None
    return len(parts)


def read_content_bytes(client: Any, table_name: str, artifact_key: str, *, expected_checksum: str | None, allowed_prefixes: Iterable[str], record_type: str = "artifact") -> bytes | None:
    """Reassemble an artifact's bytes from its content items.

    Returns ``None`` when the artifact has no content copy (part 0 missing, e.g. it was
    written before mirroring). A partial copy is ``NOT_FOUND``; a checksum mismatch is
    ``IMMUTABLE_RECORD``.
    """
    if not isinstance(artifact_key, str) or not any(artifact_key.startswith(p) for p in allowed_prefixes):
        raise PlatformError.not_permitted("invalid artifact reference")
    chunks: list[bytes] = []
    total: int | None = None
    while total is None or len(chunks) < total:
        item = client.get_item(TableName=table_name, Key={"pk": {"S": content_pk(artifact_key, len(chunks))}}, ConsistentRead=True).get("Item")
        if not item:
            if not chunks:
                return None
            raise PlatformError.not_found("artifact content is incomplete in DynamoDB", record_type=record_type)
        total = int(item["parts"]["N"])
        chunks.append(bytes(item["data"]["B"]))
    raw = gzip.decompress(b"".join(chunks))
    if expected_checksum is not None and sha256_checksum(raw) != expected_checksum:
        raise PlatformError.immutable("DynamoDB artifact content checksum mismatch", record_type=record_type)
    return raw
