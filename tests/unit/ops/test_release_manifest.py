"""Release identity, manifest builder and publisher (task 10.3; PIPE-04, PIPE-03 digest, PIPE-06 rollback record).

Manifests are validated against the pinned contract ``core/v1/release-manifest.json``; SSM writes
go to moto; the approval record comes from a fake CodePipeline client.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from finplan_contracts import ssm as contract_ssm
from finplan_contracts.validate import validate

from scripts import release
from scripts.release import ManifestError, ReleaseInfo

COMMIT = "0123456789abcdef0123456789abcdef01234567"
T0 = datetime(2026, 1, 12, 14, 30, tzinfo=UTC)


def _info(release_id: str | None = None, **kw: Any) -> ReleaseInfo:
    return ReleaseInfo(
        release_id=release_id or release.mint_release_id(T0),
        source_commit=COMMIT,
        artifact_digest="sha256:" + "a" * 64,
        contract_version="0.1.0",
        contract_digest="sha256:" + "b" * 64,
        served_contract_majors=[0],
        region="us-east-2",
        built_at="2026-01-12T14:00:00Z",
        **kw,
    )


def _publish_outputs(ssm: Any, env: str, skip: str | None = None) -> None:
    for key, name in release.platform_outputs(env).items():
        if key != skip:
            ssm.put_parameter(Name=name, Value=f"synthetic-{key}", Type="String")


# ------------------------------------------------------------------ build-stage identity
def test_release_id_and_digest() -> None:
    rid = release.mint_release_id(T0)
    assert not contract_ssm.validate_value("/finplan/beta/financialplanning/release/current-release-id", rid)
    assert validate({"release_id": rid}, "identifiers").valid


def test_assembly_digest_is_stable_and_content_sensitive_PIPE_03(ops_assembly: Path, tmp_path: Path) -> None:
    d1 = release.assembly_digest(ops_assembly)
    copy_dir = tmp_path / "copy"
    shutil.copytree(ops_assembly, copy_dir)
    assert release.assembly_digest(copy_dir) == d1
    tpl = next(copy_dir.glob("assembly-Beta/*.template.json"))
    tpl.write_text(tpl.read_text() + " ")
    assert release.assembly_digest(copy_dir) != d1
    with pytest.raises(ManifestError):
        release.assembly_digest(tmp_path)


def test_contract_pin_comes_from_the_pin_file() -> None:
    root = Path(__file__).resolve().parents[3]
    version, digest = release.contract_pin(root)
    pin = json.loads((root / "contracts-pin.json").read_text())
    assert version == pin["version"] and digest == "sha256:" + pin["sha256"] and len(digest) == 71


# ------------------------------------------------------------------ PIPE-04 builder
def test_manifest_validates_against_the_contract_schema_PIPE_04() -> None:
    info = _info()
    doc = release.build_manifest(info, "beta", deployed_at="2026-01-12T15:00:00Z", previous_release_id=None)
    assert validate(doc, "release-manifest").valid
    assert doc["outputs"]["plan-endpoint"] == "/finplan/beta/financialplanning/api/plan-endpoint"
    assert set(doc["outputs"]) == {"plan-endpoint", "ingestion-endpoint", "run-staging-ref", "ingest-schedule", *(f"bucket-{r}" for r in release.BUCKET_ROLES)}
    assert all(v.startswith("/finplan/beta/financialplanning/") for v in doc["outputs"].values())
    assert doc["artifact_digest"] == info.artifact_digest and doc["served_contract_majors"] == [0]


def test_prod_manifest_requires_approval_PIPE_04() -> None:
    with pytest.raises(ManifestError, match="approved"):
        release.build_manifest(_info(), "prod", deployed_at="2026-01-12T15:00:00Z", previous_release_id=None)
    doc = release.build_manifest(_info(), "prod", deployed_at="2026-01-12T15:00:00Z", previous_release_id=None, approval={"approved_by": "synthetic-approver", "approved_at": "2026-01-12T14:55:00Z"})
    assert doc["approved_by"] == "synthetic-approver"


def test_output_outside_the_own_segment_is_rejected_PIPE_04() -> None:
    with pytest.raises(ManifestError):
        release.build_manifest(_info(), "beta", deployed_at="2026-01-12T15:00:00Z", previous_release_id=None, outputs={"plan-endpoint": "/finplan/gamma/financialplanning/api/plan-endpoint"})


# ------------------------------------------------------------------ PIPE-04 publisher
def test_publish_writes_manifest_pointer_and_role_names_PIPE_04(ssm: Any, s3: Any) -> None:
    _publish_outputs(ssm, "beta")
    s3.create_bucket(Bucket="example-store", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
    info = _info()
    manifest = release.publish_release(info, "beta", ssm=ssm, s3=s3, store_bucket="example-store", now=T0)
    stored = json.loads(ssm.get_parameter(Name="/finplan/beta/financialplanning/release/manifest")["Parameter"]["Value"])
    assert stored == manifest and stored["previous_release_id"] is None and stored["deployed_at"] == "2026-01-12T14:30:00Z"
    assert not contract_ssm.validate_value("/finplan/beta/financialplanning/release/manifest", json.dumps(stored))
    assert ssm.get_parameter(Name="/finplan/beta/financialplanning/release/current-release-id")["Parameter"]["Value"] == info.release_id
    roles = ssm.get_parameter(Name="/finplan/beta/financialplanning/config/budget-enforced-role-names")["Parameter"]["Value"]
    assert roles == "finplan-beta-financialplanning-ingestion-handler-role,finplan-beta-financialplanning-plan-api-handler-role"
    ledger = s3.get_object(Bucket="example-store", Key=f"releases/{info.release_id}/manifests/beta.json")["Body"].read()
    assert json.loads(ledger) == manifest


def test_missing_output_fails_the_stage_PIPE_04(ssm: Any) -> None:
    _publish_outputs(ssm, "gamma", skip="ingestion-endpoint")
    with pytest.raises(ManifestError, match="ingestion-endpoint"):
        release.publish_release(_info(), "gamma", ssm=ssm, now=T0)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/finplan/gamma/financialplanning/release/manifest")


def test_previous_release_chain_and_idempotent_rerun_PIPE_04(ssm: Any) -> None:
    _publish_outputs(ssm, "beta")
    first, second = _info(), _info(release.mint_release_id(datetime(2026, 1, 13, tzinfo=UTC)))
    release.publish_release(first, "beta", ssm=ssm, now=T0)
    m2 = release.publish_release(second, "beta", ssm=ssm, now=T0)
    assert m2["previous_release_id"] == first.release_id
    again = release.publish_release(second, "beta", ssm=ssm, now=T0)  # a retried PublishManifest action
    assert again["previous_release_id"] == first.release_id


def test_rollback_records_rolled_back_from_PIPE_06(ssm: Any) -> None:
    _publish_outputs(ssm, "prod")
    approval = {"approved_by": "synthetic-approver", "approved_at": "2026-01-12T14:00:00Z"}
    rel_x, rel_y = _info(), _info(release.mint_release_id(datetime(2026, 1, 13, tzinfo=UTC)))
    release.publish_release(rel_x, "prod", ssm=ssm, now=T0, approval=approval)
    release.publish_release(rel_y, "prod", ssm=ssm, now=T0, approval=approval)
    rolled = ReleaseInfo(**{**rel_x.__dict__, "rollback": True})
    m = release.publish_release(rolled, "prod", ssm=ssm, now=T0, approval=approval)
    assert m["release_id"] == rel_x.release_id and m["rolled_back_from"] == rel_y.release_id and m["previous_release_id"] == rel_y.release_id
    assert m["artifact_digest"] == rel_x.artifact_digest
    with pytest.raises(ManifestError, match="approval"):
        release.publish_release(rel_x, "prod", ssm=ssm, now=T0)


class FakeCodePipeline:
    def __init__(self, details: list[dict[str, Any]]) -> None:
        self.details = details
        self.calls: list[dict[str, Any]] = []

    def list_action_executions(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if "nextToken" not in kw:
            return {"actionExecutionDetails": [{"actionName": "DeployStorage", "status": "Succeeded"}], "nextToken": "p2"}
        return {"actionExecutionDetails": self.details}


def test_approval_record_reads_the_manual_approval() -> None:
    cp = FakeCodePipeline([{"actionName": "ApproveProd", "status": "Succeeded", "updatedBy": "synthetic-approver", "lastUpdateTime": T0}])
    rec = release.approval_record(cp, "finplan-shared-financialplanning-pipeline", "exec-1")
    assert rec == {"approved_by": "synthetic-approver", "approved_at": "2026-01-12T14:30:00Z"}
    assert cp.calls[0]["filter"] == {"pipelineExecutionId": "exec-1"}
    with pytest.raises(ManifestError):
        release.approval_record(FakeCodePipeline([]), "p", "exec-2")


# ------------------------------------------------------------------ release ledger (rollback without rebuild)
def test_store_and_fetch_build_output_PIPE_06(s3: Any, tmp_path: Path, ops_assembly: Path) -> None:
    s3.create_bucket(Bucket="example-store", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
    out = tmp_path / "build-output"
    shutil.copytree(ops_assembly, out / "cdk.out")
    info = _info()
    info.artifact_digest = release.assembly_digest(out / "cdk.out")
    (out / "release-info.json").write_text(info.to_json())
    release.store_build_output(s3, "example-store", info, out)
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):  # write-once (If-None-Match)
        release.store_build_output(s3, "example-store", info, out)
    back = release.fetch_build_output(s3, "example-store", info.release_id, tmp_path / "rollback")
    assert back.rollback is True and back.release_id == info.release_id and back.artifact_digest == info.artifact_digest
    assert release.assembly_digest(tmp_path / "rollback" / "cdk.out") == info.artifact_digest
    with pytest.raises(ManifestError, match="no stored build output"):
        release.fetch_build_output(s3, "example-store", "rel_01KDVDNAZ83BAMMYCEGWF33DPM", tmp_path / "none")
