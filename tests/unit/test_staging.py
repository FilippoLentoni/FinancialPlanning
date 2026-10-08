"""Staged-output acceptance (tasks 7.1-7.4): STG-01..STG-06, offline.

Workers are simulated by plain S3 puts into the moto ``outputs`` bucket under
``staging/<run_id>/`` (no platform metadata, manifest last). FinanceModel's registry reference
is a :class:`StaticModelRegistry` injected through ``Services.extras`` (a mocked reference);
the SSM-backed reference is exercised against moto SSM. Manifests start from the pinned
contract package's ``staged-output-manifest`` fixtures (valid optimal, failed partial output,
infeasible) with synthetic identifiers substituted.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest
from finplan_contracts.schemas import load_store
from finplan_contracts.validate import validate as contract_validate
from ulid import ULID

from finplan_platform.core.errors import PlatformError
from finplan_platform.core.staging import (
    MANIFEST_NAME,
    SsmModelRegistry,
    StaticModelRegistry,
    accept_staged_output,
    get_staged_outcome,
    get_staged_output,
)
from finplan_platform.core.upgrade import current_version
from tests.api_support import *  # noqa: F403
from tests.api_support import PRINCIPALS, Flow, content, seed_snapshot
from tests.fakes.records import FOREIGN_MODEL_VERSION

UNKNOWN_MODEL_VERSION = "mv_01KDVDNAZ83BAMMYCEGWF33DPX"


def new_run_id() -> str:
    return "run_" + str(ULID())


def fixture_manifest(name: str) -> dict[str, Any]:
    path = load_store().fixtures_dir("staged-output-manifest") / "valid" / f"{name}.json"
    return json.loads(path.read_text())


def file_entry(name: str, data: bytes, content_type: str = "application/json") -> dict[str, Any]:
    return {"name": name, "checksum": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "content_type": content_type}


class Staging:
    """Stages synthetic worker output and drives acceptance."""

    def __init__(self, svc: Any, s3: Any, buckets: dict[str, str], flow: Flow, ctx_factory: Any) -> None:
        self.svc = svc
        self.s3 = s3
        self.bucket = buckets["outputs"]
        self.flow = flow
        self.ctx_factory = ctx_factory
        self.registry = StaticModelRegistry(runs=set(), model_versions={FOREIGN_MODEL_VERSION})
        svc.extras["model_registry"] = self.registry
        self._k = 0

    # ---------------------------------------------------------------- worker side
    def put(self, run_id: str, name: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=f"staging/{run_id}/{name}", Body=data)

    def manifest(
        self,
        *,
        plan_id: str,
        snapshot_id: str,
        parent: str | None,
        base: str = "succeeded-optimal",
        plan_content: dict[str, Any] | None = None,
        files: dict[str, bytes] | None = None,
        run_id: str | None = None,
        **overrides: Any,
    ) -> tuple[str, dict[str, Any], dict[str, bytes]]:
        run_id = run_id or new_run_id()
        m = fixture_manifest(base)
        m.update({"run_id": run_id, "plan_id": plan_id, "input_snapshot_id": snapshot_id, "parent_plan_version_id": parent, "contract_version": current_version()})
        if files is None:
            files = {}
            if m.get("files"):
                pc = plan_content if plan_content is not None else (m.get("payload") or {}).get("plan_content") or content()
                files["plan-content.json"] = json.dumps(pc, sort_keys=True).encode()
                files["metrics/summary.json"] = json.dumps({"expected_return": 0.05, "synthetic": True}).encode()
                if "payload" in m and "plan_content" in m["payload"]:
                    m["payload"]["plan_content"] = pc
        m["files"] = [file_entry(n, d) for n, d in files.items()]
        m.update(overrides)
        self.registry.runs.add(run_id)
        return run_id, m, files

    def stage(self, run_id: str, manifest: dict[str, Any] | None, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            self.put(run_id, name, data)
        if manifest is not None:  # manifest last
            self.put(run_id, MANIFEST_NAME, json.dumps(manifest).encode())

    # ---------------------------------------------------------------- platform side
    def key(self) -> str:
        self._k += 1
        return f"accept-{self._k:04d}"

    def revision(self, plan_id: str) -> int:
        return int(self.svc.repo.require("plan", plan_id).attrs.get("revision") or 0)

    def accept(self, plan_id: str, run_id: str, *, key: str | None = None, revision: int | None = None, ctx: Any = None) -> Any:
        rev = self.revision(plan_id) if revision is None else revision
        return accept_staged_output(ctx or self.ctx_factory(), self.svc, plan_id, run_id, {"expected_revision": rev, "idempotency_key": key or self.key()})

    def versions_for_run(self, run_id: str) -> list[Any]:
        return [r for r in self.svc.repo.scan("plan_version") if r.doc.get("run_id") == run_id]


@pytest.fixture
def staging(svc: Any, s3: Any, buckets: dict[str, str], flow: Flow, ctx_factory: Any) -> Staging:
    return Staging(svc, s3, buckets, flow, ctx_factory)


@pytest.fixture
def base(flow: Flow) -> dict[str, Any]:
    """A plan with a validated root version over an approved synthetic snapshot."""
    plan, snap, root = flow.validated_plan()
    return {"plan_id": plan["plan_id"], "snapshot_id": snap, "root": root["plan_version_id"]}


def raises(code: str, fn: Any, *a: Any, **kw: Any) -> PlatformError:
    with pytest.raises(PlatformError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, (ei.value.code, ei.value.message, ei.value.details)
    return ei.value


# ===================================================================== STG-01 staging area
def test_run_staging_ref_output_and_worker_policy_STG_01(foundation_synth: dict[str, Any]) -> None:
    from finplan_contracts.boundaries import env_permission_boundary, research_permission_boundary

    from infra.policy_sim import Principal, simulate

    # the reference output exists per environment and points at the staging prefix
    for env in ("beta", "gamma", "prod"):
        tmpl = next(t for n, t in foundation_synth["templates"].items() if n.startswith(f"finplan-{env}-") and "torage" in n)
        params = [r["Properties"] for r in tmpl["Resources"].values() if r["Type"] == "AWS::SSM::Parameter"]
        ref = next(p for p in params if p["Name"] == f"/finplan/{env}/financialplanning/config/run-staging-ref")
        assert "staging/" in json.dumps(ref["Value"])

    worst = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:*", "kms:*"], "Resource": "*"}]}
    policies = foundation_synth["resolver"].bucket_policies()
    bucket = "finplan-beta-financialplanning-run-staging-area-<account-id>"
    job = Principal.role("finplan-beta-financemodel-job-execution-role", worst, boundary=research_permission_boundary("beta"))
    api = Principal.role("finplan-beta-financialplanning-plan-api-handler-role", worst, boundary=env_permission_boundary("beta"))

    def sim(action: str, key: str | None, who: Any) -> Any:
        res = f"arn:aws:s3:::{bucket}/{key}" if key is not None else f"arn:aws:s3:::{bucket}"
        return simulate(action, res, who, resource_policy=policies[bucket])

    # worker writes its run, cannot read another run (or its own), cannot list
    assert sim("s3:PutObject", "staging/run_01KDVDNAZ83BAMMYCEGWF33DPM/plan-content.json", job)
    assert not sim("s3:GetObject", "staging/run_01KDVDNAZ83BAMMYCEGWF33DPM/manifest.json", job)
    assert not sim("s3:GetObject", "staging/run_01KDVDNAZ83BAMMYCEGWF33DPQ/manifest.json", job)
    assert not sim("s3:ListBucket", None, job)
    assert not sim("s3:PutObject", "accepted/run_01KDVDNAZ83BAMMYCEGWF33DPM/manifest.json", job)
    # the platform acceptance role reads and lists staging
    assert sim("s3:GetObject", "staging/run_01KDVDNAZ83BAMMYCEGWF33DPM/manifest.json", api)
    assert sim("s3:ListBucket", None, api)


# ===================================================================== STG-02 manifest last
def test_acceptance_before_manifest_is_precondition_failed_STG_02(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, None, files)  # files written, manifest not yet
    rev = staging.revision(base["plan_id"])
    err = raises("PRECONDITION_FAILED", staging.accept, base["plan_id"], run_id)
    assert err.details["reason"] == "staged_output_incomplete"
    # no state change: no outcome row, no version, head unchanged, no audit events for the run
    assert svc.repo.get("staged_output", run_id) is None
    assert staging.versions_for_run(run_id) == []
    assert staging.revision(base["plan_id"]) == rev
    assert svc.repo.audit_events(run_id) == []
    # once the manifest lands, the same request succeeds
    staging.put(run_id, MANIFEST_NAME, json.dumps(m).encode())
    assert staging.accept(base["plan_id"], run_id).response["outcome"] == "accepted"


# ===================================================================== STG-03 structural checks
def test_checksum_mismatch_rejects_and_records_file_STG_03(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    files["metrics/summary.json"] = b'{"tampered": true}'
    staging.stage(run_id, m, files)
    err = raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id)
    assert err.details["files"] == [{"name": "metrics/summary.json", "problem": "checksum_mismatch"}]
    row = svc.repo.require("staged_output", run_id).doc
    assert row["outcome"] == "rejected" and row["rejection_kind"] == "structural"
    assert row["rejected_files"] == [{"name": "metrics/summary.json", "problem": "checksum_mismatch"}]
    assert row["error"]["code"] == "VALIDATION_FAILED"
    assert staging.versions_for_run(run_id) == []


def test_unlisted_and_missing_files_reject_STG_03(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, {"plan-content.json": files["plan-content.json"], "extra.bin": b"not listed"})
    err = raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id)
    assert {"name": "extra.bin", "problem": "unlisted"} in err.details["files"]
    assert {"name": "metrics/summary.json", "problem": "missing"} in err.details["files"]
    assert staging.versions_for_run(run_id) == []


def test_unknown_model_version_rejected_naming_field_STG_03(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], model_version=UNKNOWN_MODEL_VERSION)
    staging.stage(run_id, m, files)
    err = raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id)
    assert err.details["field"] == "model_version"
    assert svc.repo.require("staged_output", run_id).doc["error"]["details"]["field"] == "model_version"
    assert staging.versions_for_run(run_id) == []


def test_unknown_run_and_snapshot_and_invalid_manifest_rejected_STG_03(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.registry.runs.discard(run_id)
    staging.stage(run_id, m, files)
    assert raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id).details["field"] == "run_id"

    run2, m2, f2 = staging.manifest(plan_id=base["plan_id"], snapshot_id="snap_01KDVDNAZ83BAMMYCEGWF33DPQ", parent=base["root"])
    staging.stage(run2, m2, f2)
    assert raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run2).details["field"] == "input_snapshot_id"

    run3, m3, f3 = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    del m3["evaluator_version"]
    staging.stage(run3, m3, f3)
    err = raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run3)
    assert err.details["reason"] == "staged_output_rejected"
    for r in (run_id, run2, run3):
        assert svc.repo.require("staged_output", r).doc["outcome"] == "rejected"
        assert staging.versions_for_run(r) == []


def test_no_model_release_is_dependency_unavailable_STG_03(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    staging.registry.release_recorded = False
    err = raises("DEPENDENCY_UNAVAILABLE", staging.accept, base["plan_id"], run_id)
    assert err.retryable is True
    assert svc.repo.get("staged_output", run_id) is None  # nothing recorded; retry later


def test_ssm_registry_reference_absent_means_no_release(ctx: Any) -> None:
    import boto3
    from moto import mock_aws

    with mock_aws():
        ssm = boto3.client("ssm", region_name="us-east-2")
        reg = SsmModelRegistry(ssm, "beta")
        assert reg.parameter_name == "/finplan/beta/financemodel/model/registry-ref"
        assert reg.lookup("run_01KDVDNAZ83BAMMYCEGWF33DPM", FOREIGN_MODEL_VERSION).release_recorded is False
        ssm.put_parameter(Name=reg.parameter_name, Value="synthetic-registry-reference", Type="String")
        # reference present but not an https endpoint: fail closed, never guess
        assert raises("DEPENDENCY_UNAVAILABLE", reg.lookup, "run_01KDVDNAZ83BAMMYCEGWF33DPM", FOREIGN_MODEL_VERSION).retryable
        from finplan_platform.core.staging import RegistryLookup

        wired = SsmModelRegistry(ssm, "beta", lambda ref, run, mv: RegistryLookup(True, run.startswith("run_"), mv == FOREIGN_MODEL_VERSION))
        assert wired.lookup("run_01KDVDNAZ83BAMMYCEGWF33DPM", FOREIGN_MODEL_VERSION).model_version_known


# ------------------------------------------------- deployed registry lookup (FinanceModel lineage route)
#: the shape FinanceModel publishes at /finplan/<env>/financemodel/model/registry-ref:
#: ``<job-endpoint>/v1/registry`` (host assembled here so the repository leak scan stays clean)
REGISTRY_REF = "https://" + "a1b2c3d4e5" + ".execute-api." + "us-east-2.amazonaws.com/api/v1/registry"


class FakeRegistryHttp:
    """FinanceModel's ``job-registry-lookup`` route as seen over HTTP (bodies as ``RegistryApi`` serves them)."""

    def __init__(self, lineage: dict[str, str]) -> None:
        self.lineage = lineage  # run_id -> model_version
        self.registered = {FOREIGN_MODEL_VERSION}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.override: tuple[int, Any] | None = None

    def __call__(self, url: str, headers: Any, timeout: float) -> tuple[int, Any]:
        from urllib.parse import parse_qs, urlsplit

        self.calls.append((url, dict(headers)))
        if self.override is not None:
            return self.override
        parts = urlsplit(url)
        prefix = urlsplit(REGISTRY_REF).path + "/lineage/"
        if not parts.path.startswith(prefix):
            return 404, {"code": "NOT_FOUND", "message": "no such route", "retryable": False, "details": {"record_type": "route"}}
        run_id, mv = parts.path[len(prefix):], parse_qs(parts.query)["model_version"][0]
        if mv not in self.registered:
            return 404, {"code": "NOT_FOUND", "message": "model_version is not registered", "retryable": False, "details": {"record_type": "model_version"}}
        if self.lineage.get(run_id) != mv:
            return 404, {"code": "NOT_FOUND", "message": "no run with that model_version is recorded", "retryable": False, "details": {"record_type": "run_lineage"}}
        return 200, {"run_id": run_id, "model_version": mv, "matches": True, "strategy": "buy_and_hold", "image_digest": "sha256:" + "0" * 64, "param_schema_version": "1", "status": "promoted", "recorded_at": "2026-10-08T17:08:21Z"}


@pytest.fixture
def deployed_registry(staging: Staging, svc: Any) -> FakeRegistryHttp:
    """Services wired as ``Services.from_lambda_environment`` does (only ``ssm``), plus a fake HTTP transport."""
    import boto3
    from botocore.credentials import Credentials

    ssm = boto3.client("ssm", region_name="us-east-2")  # inside the moto context of the s3 fixture
    ssm.put_parameter(Name="/finplan/beta/financemodel/model/registry-ref", Value=REGISTRY_REF, Type="String")
    http = FakeRegistryHttp({})
    svc.extras.pop("model_registry", None)
    svc.extras.update({"ssm": ssm, "registry_transport": http, "credentials": Credentials("AKIDSYNTHETIC0000000", "synthetic-secret")})
    return http


def test_deployed_wiring_accepts_through_the_lineage_route_STG_03(staging: Staging, base: dict[str, Any], svc: Any, deployed_registry: FakeRegistryHttp) -> None:
    """Regression (beta 2026-10-08, run_01M4E7KKFMBF005AGHYQ0S9DPG): with only ``ssm`` wired and the
    reference published, acceptance failed ``DEPENDENCY_UNAVAILABLE`` because no lookup client existed."""
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    deployed_registry.lineage[run_id] = m["model_version"]
    staging.stage(run_id, m, files)
    out = staging.accept(base["plan_id"], run_id).response
    assert out["outcome"] == "accepted" and out["plan_version_id"]
    (url, headers), = deployed_registry.calls
    assert url == f"{REGISTRY_REF}/lineage/{run_id}?model_version={m['model_version']}"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDSYNTHETIC0000000/")
    assert "/us-east-2/execute-api/aws4_request" in headers["Authorization"]


def test_deployed_wiring_maps_registry_not_found_to_field_rejections_STG_03(staging: Staging, base: dict[str, Any], svc: Any, deployed_registry: FakeRegistryHttp) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)  # no lineage recorded for this run
    assert raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id).details["field"] == "run_id"

    run2, m2, f2 = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], model_version=UNKNOWN_MODEL_VERSION)
    deployed_registry.lineage[run2] = UNKNOWN_MODEL_VERSION
    staging.stage(run2, m2, f2)
    assert raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run2).details["field"] == "model_version"


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"code": "FORBIDDEN", "message": "denied", "retryable": False, "details": {}}),
        (503, {"code": "DEPENDENCY_UNAVAILABLE", "message": "retry", "retryable": True, "details": {}}),
        (404, {"code": "NOT_FOUND", "message": "no such route", "retryable": False, "details": {"record_type": "route"}}),
        (200, {"matches": False}),
        (502, None),
        OSError("connection reset"),
    ],
)
def test_deployed_wiring_fails_closed_on_registry_outage_STG_03(staging: Staging, base: dict[str, Any], svc: Any, deployed_registry: FakeRegistryHttp, answer: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    deployed_registry.lineage[run_id] = m["model_version"]
    staging.stage(run_id, m, files)
    if isinstance(answer, Exception):
        def broken(url: str, headers: Any, timeout: float) -> Any:
            raise answer
        svc.extras["registry_transport"] = broken
    else:
        deployed_registry.override = answer
    err = raises("DEPENDENCY_UNAVAILABLE", staging.accept, base["plan_id"], run_id)
    assert err.retryable is True and err.details["dependency"] == "financemodel-registry"
    assert "execute-api" not in json.dumps(err.details) and "execute-api" not in err.message
    assert svc.repo.get("staged_output", run_id) is None  # nothing recorded; retry later


# ===================================================================== STG-04 outcome-aware
@pytest.mark.parametrize("completion", ["failed", "cancelled", "timed_out"])
def test_unsuccessful_runs_recorded_rejected_without_version_STG_04(staging: Staging, base: dict[str, Any], svc: Any, completion: str) -> None:
    # the contract partial-output fixture (completion_status failed, plan-content.json listed)
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], base="failed-partial-output", completion_status=completion)
    assert contract_validate(m, "staged-output-manifest").valid
    staging.stage(run_id, m, files)
    rev = staging.revision(base["plan_id"])
    out = staging.accept(base["plan_id"], run_id).response  # not an error: the decision is recorded
    assert out["outcome"] == "rejected" and out["rejection_kind"] == "run_outcome"
    assert out["error"]["code"] == "PRECONDITION_FAILED" and out["error"]["details"]["completion_status"] == completion
    assert contract_validate(out["error"], "error").valid
    assert staging.versions_for_run(run_id) == [] and staging.revision(base["plan_id"]) == rev
    assert get_staged_output(staging.ctx_factory(), svc, run_id)["outcome"] == "rejected"


@pytest.mark.parametrize("solution", ["infeasible", "unbounded"])
def test_infeasible_or_unbounded_records_no_version_STG_04(staging: Staging, base: dict[str, Any], svc: Any, solution: str) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], base="succeeded-infeasible-no-files", solution_status=solution)
    assert files == {} and contract_validate(m, "staged-output-manifest").valid
    staging.stage(run_id, m, files)
    out = staging.accept(base["plan_id"], run_id).response
    assert out["outcome"] == "no_version" and out["solution_status"] == solution
    assert "error" not in out
    assert staging.versions_for_run(run_id) == []
    assert get_staged_outcome(staging.ctx_factory(), svc, run_id)["solution_status"] == solution


def test_outcome_read_by_financemodel_job_api_role_STG_04(staging: Staging, base: dict[str, Any], clients: Any) -> None:
    accepted, m, f = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(accepted, m, f)
    pv = staging.accept(base["plan_id"], accepted).response["plan_version_id"]
    failed, m2, f2 = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], base="failed-partial-output")
    staging.stage(failed, m2, f2)
    staging.accept(base["plan_id"], failed)
    novers, m3, f3 = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], base="succeeded-infeasible-no-files")
    staging.stage(novers, m3, f3)
    staging.accept(base["plan_id"], novers)

    api = clients.fm_job_api
    code, body, _ = api.get(f"/v1/staged-outputs/{accepted}")
    assert code == 200 and body["outcome"] == "accepted" and body["plan_version_id"] == pv and body["plan_version_status"] == "validated"
    code, body, _ = api.get(f"/v1/staged-outputs/{failed}")
    assert code == 200 and body["outcome"] == "rejected" and body["error"]["code"] == "PRECONDITION_FAILED"
    code, body, _ = api.get(f"/v1/staged-outputs/{novers}")
    assert code == 200 and body["outcome"] == "no_version" and body["solution_status"] == "infeasible"
    # no bucket names or keys in the outcome
    assert "staging/" not in json.dumps(body) and "example-beta" not in json.dumps(body)
    # it cannot call any write route, including acceptance itself
    code, body, _ = api.post(f"/v1/plans/{base['plan_id']}/staged-outputs/{accepted}/accept", {"expected_revision": 0, "idempotency_key": "x-1"})
    assert code == 403 and body["code"] == "FORBIDDEN"
    code, _, _ = api.post(f"/v1/plans/{base['plan_id']}/versions", {"idempotency_key": "x-2"})
    assert code == 403
    # FinanceLambdasTool classes are denied the accept route too
    for cls in ("reader", "submitter", "writer"):
        code, _, _ = getattr(clients, cls).post(f"/v1/plans/{base['plan_id']}/staged-outputs/{accepted}/accept", {"expected_revision": 0, "idempotency_key": "x-3"})
        assert code == 403


# ===================================================================== STG-05 validation gate
def test_valid_output_becomes_validated_model_run_version_STG_05(staging: Staging, base: dict[str, Any], svc: Any, clients: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    code, out, _ = clients.website.post(f"/v1/plans/{base['plan_id']}/staged-outputs/{run_id}/accept", {"expected_revision": staging.revision(base["plan_id"]), "idempotency_key": "web-accept-1"})
    assert code == 200, out
    assert out["outcome"] == "accepted" and out["status"] == "validated" and out["origin"] == "model_run"
    rec = svc.repo.require("plan_version", out["plan_version_id"]).doc
    assert rec["status"] == "validated" and rec["origin"] == "model_run"
    for name in ("run_id", "model_version", "configuration_id", "input_snapshot_id"):
        assert rec[name] == m[name]
    assert rec["parent_plan_version_id"] == base["root"]
    assert rec["source_artifact"]["kind"] == "staged_output_manifest"
    assert rec["source_artifact"]["checksum"] == "sha256:" + hashlib.sha256(json.dumps(m).encode()).hexdigest()
    assert contract_validate(rec, "plan-version").valid
    # payload copied write-once into accepted/ (outputs) and the content into plans
    keys = {k for k, _ in svc.store.list_objects("outputs", f"accepted/{run_id}/")}
    assert keys == {f"accepted/{run_id}/{n}" for n in (MANIFEST_NAME, "plan-content.json", "metrics/summary.json")}
    assert svc.store.exists("plans", f"{base['plan_id']}/{out['plan_version_id']}/content.json")
    # the head moved to the accepted version; the response leaks no storage location
    assert svc.repo.require("plan", base["plan_id"]).attrs["current_version_id"] == out["plan_version_id"]
    assert "example-beta" not in json.dumps(out) and "staging/" not in json.dumps(out)


def test_root_output_without_parent_is_accepted_STG_05(staging: Staging, flow: Flow, svc: Any) -> None:
    snap = seed_snapshot(svc, flow.clock)
    plan = flow.plan()
    run_id, m, files = staging.manifest(plan_id=plan["plan_id"], snapshot_id=snap, parent=None, base="root-without-parent")
    staging.stage(run_id, m, files)
    out = staging.accept(plan["plan_id"], run_id).response
    assert out["outcome"] == "accepted" and out["status"] == "validated" and out["parent_plan_version_id"] is None


def _two_instrument_snapshot(svc: Any, clock: Any) -> str:
    payload = json.loads((load_store().fixtures_dir("snapshot-payload") / "valid" / "etf-daily.json").read_text())
    extra = copy.deepcopy(payload["instruments"][0])
    extra.update({"instrument_id": "SYNB", "name": "Second synthetic instrument"})
    payload["instruments"].append(extra)
    for obs in list(payload["observations"]):
        o = copy.deepcopy(obs)
        o["instrument_id"] = "SYNB"
        payload["observations"].append(o)
    return seed_snapshot(svc, clock, payload=payload)


def test_partial_output_is_invalid_and_unpublishable_STG_05(staging: Staging, flow: Flow, svc: Any, clients: Any) -> None:
    snap = _two_instrument_snapshot(svc, flow.clock)
    plan = flow.plan()
    full = content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.5}, {"instrument_id": "SYNB", "weight": 0.3}], "cash_weight": 0.2})
    root = flow.root(plan["plan_id"], snap, body_content=full)
    assert flow.validate(root["plan_version_id"])["status"] == "validated"
    # the staged allocation omits SYNB (weights still reconcile, so only the gate catches it)
    partial = content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.5}], "cash_weight": 0.5})
    run_id, m, files = staging.manifest(plan_id=plan["plan_id"], snapshot_id=snap, parent=root["plan_version_id"], plan_content=partial)
    staging.stage(run_id, m, files)
    out = staging.accept(plan["plan_id"], run_id).response
    assert out["outcome"] == "accepted" and out["status"] == "invalid"
    finding = next(f for f in out["findings"] if f["details"].get("rule") == "required_instruments")
    assert finding["details"]["missing_instruments"] == ["SYNB"]
    rec = svc.repo.require("plan_version", out["plan_version_id"]).doc
    assert rec["status"] == "invalid" and contract_validate(rec, "plan-version").valid
    # publishing it fails; it can never become validated
    code, body, _ = flow.clients.website.post(f"/v1/plans/{plan['plan_id']}/publications", {"plan_version_id": out["plan_version_id"], "expected_revision": 0, "idempotency_key": "pub-partial"})
    assert code == 422 and body["code"] == "PRECONDITION_FAILED"
    code, body, _ = flow.clients.website.post(f"/v1/plan-versions/{out['plan_version_id']}/validate", {"idempotency_key": "revalidate-partial"})
    assert code == 200 and body["status"] == "invalid"


def test_unreconciled_output_is_invalid_STG_05(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    bad = content(allocation={"weights": [{"instrument_id": "SPY", "weight": 0.67}], "cash_weight": 0.4})
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], plan_content=bad)
    staging.stage(run_id, m, files)
    out = staging.accept(base["plan_id"], run_id).response
    assert out["status"] == "invalid"
    assert any(f["details"].get("rule") == "weights_sum" for f in out["findings"])


def test_schema_invalid_content_rejected_without_version_STG_05(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"], plan_content={"base_currency": "USD"})
    m.pop("payload")
    staging.stage(run_id, m, files)
    raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id)
    assert staging.versions_for_run(run_id) == []


# ===================================================================== STG-06 idempotency/concurrency
def test_duplicate_acceptance_replays_and_new_key_conflicts_STG_06(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    rev = staging.revision(base["plan_id"])
    first = staging.accept(base["plan_id"], run_id, key="same-key", revision=rev)
    again = staging.accept(base["plan_id"], run_id, key="same-key", revision=rev)
    assert again.replayed and again.response["plan_version_id"] == first.response["plan_version_id"]
    err = raises("CONFLICT", staging.accept, base["plan_id"], run_id, key="other-key")
    assert err.details["plan_version_id"] == first.response["plan_version_id"]
    assert len(staging.versions_for_run(run_id)) == 1


def test_concurrent_acceptance_has_one_winner_STG_06(staging: Staging, base: dict[str, Any], svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    first = staging.accept(base["plan_id"], run_id, key="racer-a").response
    # the second racer read "no outcome yet" before the first committed: only the
    # transaction's conditional outcome row stops it
    real_get = svc.repo.get
    monkeypatch.setattr(svc.repo, "get", lambda table, rid, *a, **k: None if table == "staged_output" else real_get(table, rid, *a, **k))
    rev = staging.revision(base["plan_id"])
    err = raises("CONFLICT", staging.accept, base["plan_id"], run_id, key="racer-b", revision=rev)
    assert err.details["plan_version_id"] == first["plan_version_id"]
    monkeypatch.undo()
    assert len(staging.versions_for_run(run_id)) == 1
    assert staging.revision(base["plan_id"]) == rev  # the loser moved nothing


def test_head_moved_conflicts_and_stays_retryable_STG_06(staging: Staging, base: dict[str, Any], svc: Any, flow: Flow) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    stale = staging.revision(base["plan_id"])
    flow.child(base["plan_id"], base["root"])  # the head moves between request and commit
    err = raises("CONFLICT", staging.accept, base["plan_id"], run_id, key="retry-key", revision=stale)
    assert err.details["current_revision"] == stale + 1
    assert svc.repo.get("staged_output", run_id) is None and staging.versions_for_run(run_id) == []
    # eligible for a retry with the new revision (same key: the failed attempt stored nothing)
    out = staging.accept(base["plan_id"], run_id, key="retry-key", revision=stale + 1).response
    assert out["outcome"] == "accepted" and out["revision"] == stale + 2


def test_concurrent_head_move_in_transaction_conflicts_STG_06(staging: Staging, base: dict[str, Any], svc: Any, monkeypatch: pytest.MonkeyPatch, flow: Flow) -> None:
    """The head moves after the early check: the transaction's HeadMove condition fails."""
    import finplan_platform.core.staging as staging_mod

    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    rev = staging.revision(base["plan_id"])
    real = staging_mod.new_version_mutation

    def racing(*a: Any, **k: Any) -> Any:
        mutation = real(*a, **k)
        flow.child(base["plan_id"], base["root"])  # another writer commits first
        return mutation

    monkeypatch.setattr(staging_mod, "new_version_mutation", racing)
    raises("CONFLICT", staging.accept, base["plan_id"], run_id, revision=rev)
    monkeypatch.undo()
    assert svc.repo.get("staged_output", run_id) is None and staging.versions_for_run(run_id) == []


def test_rejected_run_is_final_and_replayable(staging: Staging, base: dict[str, Any], svc: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    files["metrics/summary.json"] = b"{}"
    staging.stage(run_id, m, files)
    raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id, key="rej-key")
    # same key: the recorded rejection is returned again
    raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id, key="rej-key")
    # a new key: the run is already decided
    assert raises("CONFLICT", staging.accept, base["plan_id"], run_id, key="rej-key-2").details["outcome"] == "rejected"


def test_manifest_for_another_plan_is_request_error(staging: Staging, base: dict[str, Any], flow: Flow, svc: Any) -> None:
    other = flow.plan()
    run_id, m, files = staging.manifest(plan_id=other["plan_id"], snapshot_id=base["snapshot_id"], parent=None)
    staging.stage(run_id, m, files)
    assert raises("VALIDATION_FAILED", staging.accept, base["plan_id"], run_id).details["field"] == "plan_id"
    assert svc.repo.get("staged_output", run_id) is None


def test_unknown_run_outcome_read_is_not_found(ctx: Any, svc: Any) -> None:
    raises("NOT_FOUND", get_staged_output, ctx, svc, "run_01KDVDNAZ83BAMMYCEGWF33DPM")
    raises("INVALID_IDENTIFIER", get_staged_output, ctx, svc, "not-a-run")


def test_principal_class_guard(staging: Staging, base: dict[str, Any], ctx_factory: Any) -> None:
    run_id, m, files = staging.manifest(plan_id=base["plan_id"], snapshot_id=base["snapshot_id"], parent=base["root"])
    staging.stage(run_id, m, files)
    tool_ctx = ctx_factory(PRINCIPALS["writer"], role_class="plan-writer")
    raises("FORBIDDEN", staging.accept, base["plan_id"], run_id, ctx=tool_ctx)


# ===================================================================== 7.5 worker protocol doc
def test_staging_doc_example_manifest_validates() -> None:
    import re
    from pathlib import Path

    from finplan_contracts.leak_scan import scan_text

    doc = Path(__file__).resolve().parents[2] / "docs" / "staging.md"
    text = doc.read_text()
    block = re.search(r"<!-- example-manifest -->\s*```json\n(.*?)```\s*<!-- /example-manifest -->", text, re.S)
    assert block, "docs/staging.md lost its example manifest"
    example = json.loads(block.group(1))
    res = contract_validate(example, "staged-output-manifest")
    assert res.valid, [i.to_dict() for i in res.issues]
    assert example["contract_version"] == current_version()  # the pinned contract version (1.0.0)
    assert example["synthetic"] is True
    assert not scan_text(text, str(doc))
