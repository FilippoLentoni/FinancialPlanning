#!/usr/bin/env python3
"""Release identity, artifact digest, release manifest and published references (task 10.3; PIPE-03, PIPE-04, PIPE-06).

Build stage (once per commit):

* :func:`mint_release_id` - ``rel_`` + ULID (contracts: minted by the repository's build stage,
  shared by every environment of that build);
* :func:`assembly_digest` - ``sha256:`` over the sorted (path, SHA-256) list of every file of the
  cloud assembly (templates, asset sources, manifests). Beta, gamma and prod deploy the same
  BuildOutput, so every manifest of one release records the same ``artifact_digest`` (PIPE-03);
* :class:`ReleaseInfo` (``release-info.json`` in BuildOutput) - release ID, source commit,
  artifact digest, pinned contract version and wheel digest, served contract majors;
* :func:`store_build_output` / :func:`fetch_build_output` - the release ledger in the pipeline
  store (``releases/<release_id>/build-output.zip``), used by the rollback path: the build stage
  re-emits a recorded release's BuildOutput without rebuilding (PIPE-06; the digest is re-verified).

Each deploy (``PublishManifest`` action, :func:`publish_release`):

1. every platform output parameter (:func:`platform_outputs`: ``api/plan-endpoint``,
   ``api/ingestion-endpoint``, ``config/run-staging-ref``, ``config/ingest-schedule`` and the six
   ``config/bucket-<role>``) must exist - the stacks create them; a missing one fails the stage;
2. the manifest (:func:`build_manifest`) validates against the pinned contract
   ``core/v1/release-manifest.json`` and the registered SSM value rules; prod manifests carry
   ``approved_by``/``approved_at`` from the manual approval action; a rollback records
   ``rolled_back_from`` (the release it replaces);
3. writes, each checked with :func:`finplan_contracts.ssm.check_write` as the ``pipeline`` writer
   bound to the environment: ``release/manifest`` (advanced tier above 4 KB), ``release/current-release-id``
   and ``config/budget-enforced-role-names`` (the platform runtime roles the budget action denies);
4. copies the manifest into the ledger (``releases/<release_id>/manifests/<env>.json``).

All AWS access goes through injected clients; the unit suite uses moto and fakes.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finplan_contracts import ssm as contract_ssm
from finplan_contracts.validate import validate
from ulid import ULID

__all__ = [
    "BUCKET_ROLES",
    "REPO",
    "ManifestError",
    "ReleaseInfo",
    "approval_record",
    "assembly_digest",
    "build_manifest",
    "contract_pin",
    "fetch_build_output",
    "mint_release_id",
    "platform_enforced_role_names",
    "platform_outputs",
    "publish_release",
    "store_build_output",
    "zip_dir",
]

REPO = "financialplanning"
BUCKET_ROLES = ("raw", "curated", "snapshots", "plans", "outputs", "reports")
RELEASES_PREFIX = "releases/"
ADVANCED_TIER_BYTES = 4096
APPROVAL_ACTION = "ApproveProd"
#: Platform runtime roles the budget action denies at 100% (contract D4 budget-enforced-role-names).
ENFORCED_LOGICAL_ROLES = ("ingestion-handler", "plan-api-handler")


class ManifestError(ValueError):
    pass


# ===================================================================== build stage
def mint_release_id(now: datetime | None = None) -> str:
    return "rel_" + str(ULID.from_datetime(now) if now else ULID())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assembly_digest(assembly: str | Path) -> str:
    root = Path(assembly)
    if not (root / "manifest.json").is_file():
        raise ManifestError(f"{root} is not a cloud assembly (manifest.json missing)")
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8") + b"\0" + _sha256(path.read_bytes()).encode("ascii") + b"\n")
    return "sha256:" + h.hexdigest()


def contract_pin(root: str | Path) -> tuple[str, str]:
    """(pinned contract version, ``sha256:`` digest of the pinned wheel) from ``contracts-pin.json``."""
    pin = json.loads((Path(root) / "contracts-pin.json").read_text(encoding="utf-8"))
    return str(pin["version"]), "sha256:" + str(pin["sha256"])


@dataclass
class ReleaseInfo:
    release_id: str
    source_commit: str
    artifact_digest: str
    contract_version: str
    contract_digest: str
    served_contract_majors: list[int]
    region: str
    built_at: str
    rollback: bool = False
    synthetic: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def load(cls, path: str | Path) -> ReleaseInfo:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


# ===================================================================== outputs
def _name(env: str, category: str, name: str) -> str:
    return contract_ssm.build(env, REPO, category, name)


def platform_outputs(env: str) -> dict[str, str]:
    """Logical output key -> SSM parameter name (own segment only; design P2/P4, platform-pipeline spec)."""
    out = {
        "plan-endpoint": _name(env, "api", "plan-endpoint"),
        "ingestion-endpoint": _name(env, "api", "ingestion-endpoint"),
        "run-staging-ref": _name(env, "config", "run-staging-ref"),
        "ingest-schedule": _name(env, "config", "ingest-schedule"),
    }
    out.update({f"bucket-{r}": _name(env, "config", f"bucket-{r}") for r in BUCKET_ROLES})
    return out


def platform_enforced_role_names(env: str) -> list[str]:
    return [f"finplan-{env}-{REPO}-{logical}-role" for logical in ENFORCED_LOGICAL_ROLES]


# ===================================================================== manifest
def build_manifest(
    info: ReleaseInfo,
    env: str,
    *,
    deployed_at: str,
    previous_release_id: str | None,
    approval: Mapping[str, str] | None = None,
    rolled_back_from: str | None = None,
    outputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "repo": REPO,
        "environment": env,
        "region": info.region,
        "release_id": info.release_id,
        "source_commit": info.source_commit,
        "artifact_digest": info.artifact_digest,
        "contract_version": info.contract_version,
        "contract_digest": info.contract_digest,
        "deployed_at": deployed_at,
        "previous_release_id": previous_release_id,
        "outputs": dict(outputs if outputs is not None else platform_outputs(env)),
        "served_contract_majors": sorted(set(info.served_contract_majors)),
        "synthetic": info.synthetic,
    }
    if approval:
        doc["approved_by"] = approval["approved_by"]
        doc["approved_at"] = approval["approved_at"]
    if rolled_back_from is not None:
        doc["rolled_back_from"] = rolled_back_from
    res = validate(doc, "release-manifest")
    problems = [f"{i.pointer or '/'}: {i.message}" for i in res.issues]
    problems += contract_ssm.validate_value(_name(env, "release", "manifest"), json.dumps(doc))
    if problems:
        raise ManifestError("release manifest is invalid: " + "; ".join(dict.fromkeys(problems)))
    return doc


def _ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def approval_record(codepipeline: Any, pipeline_name: str, execution_id: str, action_name: str = APPROVAL_ACTION) -> dict[str, str]:
    """Approver and time of the manual approval in this pipeline execution (prod manifest)."""
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"pipelineName": pipeline_name, "filter": {"pipelineExecutionId": execution_id}}
        if token:
            kw["nextToken"] = token
        resp = codepipeline.list_action_executions(**kw)
        for d in resp.get("actionExecutionDetails") or []:
            if d.get("actionName") == action_name and d.get("status") == "Succeeded":
                who = d.get("updatedBy") or ((d.get("output") or {}).get("executionResult") or {}).get("externalExecutionSummary")
                when = d.get("lastUpdateTime")
                if who and when:
                    return {"approved_by": str(who), "approved_at": _ts(when)}
        token = resp.get("nextToken")
        if not token:
            break
    raise ManifestError(f"no succeeded manual approval '{action_name}' in pipeline execution {execution_id}; prod manifests require approved_by and approved_at")


# ===================================================================== publish
def _get(ssm: Any, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code == "ParameterNotFound":
            return None
        raise


def _put(ssm: Any, env: str, name: str, value: str) -> None:
    decision = contract_ssm.check_write(name, contract_ssm.Writer(REPO, "pipeline", env))
    if not decision:
        raise ManifestError("; ".join(decision.reasons))
    problems = contract_ssm.validate_value(name, value)
    if problems:
        raise ManifestError(f"{name}: " + "; ".join(problems))
    tier = "Advanced" if len(value.encode("utf-8")) > ADVANCED_TIER_BYTES else "Standard"
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True, Tier=tier)


def publish_release(
    info: ReleaseInfo,
    env: str,
    *,
    ssm: Any,
    s3: Any | None = None,
    store_bucket: str | None = None,
    now: datetime | None = None,
    approval: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Publish the manifest, the current release pointer and the platform's enforced role names (PIPE-04)."""
    outputs = platform_outputs(env)
    missing = [name for name in outputs.values() if _get(ssm, name) is None]
    if missing:
        raise ManifestError(f"the {env} deploy did not publish: {', '.join(missing)}")
    if env == "prod" and not approval:
        raise ManifestError("prod manifests require the approval record (approved_by, approved_at)")
    pointer = _name(env, "release", "current-release-id")
    manifest_name = _name(env, "release", "manifest")
    current = _get(ssm, pointer)
    previous: str | None = current
    rolled_back_from: str | None = None
    if current == info.release_id:  # a re-run of the same release keeps its recorded predecessor
        existing = json.loads(_get(ssm, manifest_name) or "{}")
        previous = existing.get("previous_release_id")
        rolled_back_from = existing.get("rolled_back_from")
    elif info.rollback:
        rolled_back_from = current
    deployed_at = _ts(now or datetime.now(UTC))
    manifest = build_manifest(info, env, deployed_at=deployed_at, previous_release_id=previous, approval=approval, rolled_back_from=rolled_back_from, outputs=outputs)
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    _put(ssm, env, manifest_name, body)
    _put(ssm, env, pointer, info.release_id)
    _put(ssm, env, _name(env, "config", "budget-enforced-role-names"), ",".join(platform_enforced_role_names(env)))
    if s3 is not None and store_bucket:
        s3.put_object(Bucket=store_bucket, Key=f"{RELEASES_PREFIX}{info.release_id}/manifests/{env}.json", Body=body.encode("utf-8"), ContentType="application/json")
    return manifest


# ===================================================================== release ledger
def zip_dir(directory: Path) -> bytes:
    from scripts.publish_assets import deterministic_zip

    return deterministic_zip(directory)


def store_build_output(s3: Any, bucket: str, info: ReleaseInfo, out_dir: str | Path) -> str:
    """Store BuildOutput for rollback (write-once; the key carries the release ID)."""
    key = f"{RELEASES_PREFIX}{info.release_id}/build-output.zip"
    s3.put_object(Bucket=bucket, Key=key, Body=zip_dir(Path(out_dir)), IfNoneMatch="*")
    s3.put_object(Bucket=bucket, Key=f"{RELEASES_PREFIX}{info.release_id}/release-info.json", Body=info.to_json().encode("utf-8"), IfNoneMatch="*")
    return key


def fetch_build_output(s3: Any, bucket: str, release_id: str, out_dir: str | Path) -> ReleaseInfo:
    """Re-emit a recorded release's BuildOutput (rollback; no rebuild). Verifies the artifact digest."""
    if not release_id.startswith("rel_"):
        raise ManifestError(f"{release_id!r} is not a release_id")
    try:
        body = s3.get_object(Bucket=bucket, Key=f"{RELEASES_PREFIX}{release_id}/build-output.zip")["Body"].read()
    except Exception as exc:
        raise ManifestError(f"release {release_id} has no stored build output in the pipeline store") from exc
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise ManifestError(f"unsafe path in stored build output: {member}")
        zf.extractall(out)
    info = ReleaseInfo.load(out / "release-info.json")
    if info.release_id != release_id:
        raise ManifestError("stored build output belongs to another release")
    digest = assembly_digest(out / "cdk.out")
    if digest != info.artifact_digest:
        raise ManifestError("stored build output digest does not match its release record")
    info.rollback = True
    (out / "release-info.json").write_text(info.to_json(), encoding="utf-8")
    return info
