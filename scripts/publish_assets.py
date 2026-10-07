#!/usr/bin/env python3
"""Publish the cloud assembly's file assets to the pipeline store (task 10.1; build stage).

The environment stacks are synthesized with :func:`infra.stacks.tooling.deployment_synthesizer`,
so every Lambda code asset is addressed as ``assets/<sha256>.zip`` in the pipeline store and the
pipeline's CloudFormation actions need nothing else. This script reads every ``*.assets.json``
manifest of the assembly (nested stage assemblies included) and uploads each **file** asset once:

* ``zip`` packaging: a deterministic zip of the source directory (sorted entries, fixed timestamps
  and modes), so the same build produces the same bytes;
* ``file`` packaging: the file as is (stack templates; the pipeline reads templates from the
  BuildOutput artifact, but they are published too so the store holds a complete assembly);
* the object key already carries the content hash, so an existing key is skipped and new keys are
  written with ``If-None-Match: *`` (write-once, never overwritten);
* container-image assets are refused: the platform has no image repository yet (the ownership
  matrix lacks a FinancialPlanning image-repository row; task 6.17's image mode is a follow-up).

Only destinations in the pipeline store are accepted. ``${AWS::AccountId}`` and ``${AWS::Region}``
placeholders are resolved from ``--account``/``--region``, else from the CodeBuild environment
(``CODEBUILD_BUILD_ARN``, ``AWS_REGION``). Tests inject a moto S3 client; nothing here runs at import.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infra.stacks.tooling import (  # noqa: E402
    BOOTSTRAP_PREFIX,
    PIPELINE_STORE_STEM,
    shared_name,
)

__all__ = ["PublishError", "Published", "account_from_build_arn", "deterministic_zip", "iter_asset_manifests", "main", "planned_uploads", "publish"]

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class Published:
    asset_id: str
    bucket: str
    key: str
    size: int
    uploaded: bool  # False: already present (content-addressed)


def deterministic_zip(directory: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            rel = path.relative_to(directory).as_posix()
            info = zipfile.ZipInfo(rel, date_time=ZIP_EPOCH)
            mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def iter_asset_manifests(assembly: Path) -> Iterator[Path]:
    yield from sorted(assembly.rglob("*.assets.json"))


def _resolve(value: str, account: str, region: str) -> str:
    return value.replace("${AWS::AccountId}", account).replace("${AWS::Region}", region).replace("${AWS::Partition}", "aws")


def planned_uploads(assembly: Path, *, account: str, region: str, skip_prefixes: tuple[str, ...] = (BOOTSTRAP_PREFIX,)) -> list[tuple[str, Path, str, str, str]]:
    """(asset id, source path, packaging, bucket, key) for every file asset; refuses image assets.

    Keys under ``bootstrap/`` (the staged tooling template) are published only by the bootstrap.
    """
    store = f"{shared_name(PIPELINE_STORE_STEM)}-{account}"
    out: dict[tuple[str, str], tuple[str, Path, str, str, str]] = {}
    for manifest in iter_asset_manifests(assembly):
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        if doc.get("dockerImages"):
            raise PublishError(f"{manifest.name}: container-image assets are not supported yet (no FinancialPlanning image repository; see docs/pipeline.md)")
        for asset_id, asset in sorted((doc.get("files") or {}).items()):
            src = asset.get("source") or {}
            path = (manifest.parent / str(src.get("path", ""))).resolve()
            packaging = str(src.get("packaging", "file"))
            if packaging not in ("zip", "file"):
                raise PublishError(f"{manifest.name}: asset {asset_id} has unsupported packaging {packaging!r}")
            for dest in (asset.get("destinations") or {}).values():
                bucket = _resolve(str(dest["bucketName"]), account, region)
                key = _resolve(str(dest["objectKey"]), account, region)
                if bucket != store:
                    raise PublishError(f"{manifest.name}: asset {asset_id} targets a bucket outside the pipeline store")
                if key.startswith(skip_prefixes):
                    continue
                out[(bucket, key)] = (asset_id, path, packaging, bucket, key)
    return [out[k] for k in sorted(out)]


def _exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) if hasattr(exc, "response") else ""
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def publish(assembly: str | os.PathLike[str], s3: Any, *, account: str, region: str) -> list[Published]:
    results: list[Published] = []
    for asset_id, path, packaging, bucket, key in planned_uploads(Path(assembly), account=account, region=region):
        if _exists(s3, bucket, key):
            results.append(Published(asset_id, bucket, key, 0, False))
            continue
        if packaging == "zip":
            if not path.is_dir():
                raise PublishError(f"asset {asset_id}: source directory is missing from the assembly")
            body = deterministic_zip(path)
        else:
            if not path.is_file():
                raise PublishError(f"asset {asset_id}: source file is missing from the assembly")
            body = path.read_bytes()
        s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
        results.append(Published(asset_id, bucket, key, len(body), True))
    return results


def account_from_build_arn(arn: str | None) -> str | None:
    """``arn:aws:codebuild:<region>:<account>:build/...`` -> account (the build's own account)."""
    parts = (arn or "").split(":")
    return parts[4] if len(parts) > 5 and parts[2] == "codebuild" else None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs AWS (exercised in the build stage)
    ap = argparse.ArgumentParser(description="Publish file assets of a cloud assembly to the pipeline store.")
    ap.add_argument("assembly")
    ap.add_argument("--account", default=None)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    args = ap.parse_args(argv)
    account = args.account or account_from_build_arn(os.environ.get("CODEBUILD_BUILD_ARN"))
    if not account or not args.region:
        print("account and region are required (--account/--region or the CodeBuild environment)", file=sys.stderr)
        return 2
    import boto3

    for p in publish(args.assembly, boto3.client("s3", region_name=args.region), account=account, region=args.region):
        print(f"{'uploaded' if p.uploaded else 'present '} {p.key}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
