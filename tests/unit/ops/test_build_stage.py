"""Build-stage gates, packaging and asset publishing (tasks 10.1, 10.2; PIPE-02).

PIPE-02 spec scenario "Leaked identifier": a commit containing an account-ID pattern outside the
allow-listed placeholders fails the build stage and no artifact is produced. The fixture commit is
a temporary tree; the 12-digit number is assembled at run time so this file itself stays clean.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts import build_gates, build_stage, publish_assets

ROOT = Path(__file__).resolve().parents[3]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FAKE_ACCOUNT = "testaccount"


def _fixture_commit(tmp_path: Path, *, leak: bool) -> Path:
    root = tmp_path / "commit"
    (root / "config").mkdir(parents=True)
    for name in ("beta.json", "gamma.json", "prod.json", "shared.json"):
        shutil.copy(ROOT / "config" / name, root / "config" / name)
    shutil.copy(ROOT / "contracts-pin.json", root / "contracts-pin.json")
    (root / "platform").mkdir()
    body = "# deployment notes\nregion = us-east-2\n"
    if leak:
        body += "account = " + "1234" * 3 + "\n"  # an account-ID pattern
    (root / "platform" / "notes.py").write_text(body)
    return root


def _no_bundles(root: Path, out: Path) -> dict[str, Any]:
    """Bundle building is covered by tests/unit/ops/test_lambda_bundle.py (real uv build)."""
    return {}


def _fake_synth(source: Path) -> Any:
    def synth(out: Path) -> Path:
        shutil.copytree(source, out)
        return out

    return synth


# ------------------------------------------------------------------ PIPE-02
def test_leaked_account_id_fails_the_gate_PIPE_02(tmp_path: Path) -> None:
    root = _fixture_commit(tmp_path, leak=True)
    (res,) = build_gates.run_gates(build_gates.GateContext(root=root), only=["leak-scan"])
    assert not res.ok and any("account-id" in p for p in res.problems)
    clean = _fixture_commit(tmp_path / "clean", leak=False)
    (res,) = build_gates.run_gates(build_gates.GateContext(root=clean), only=["leak-scan"])
    assert res.ok
    assert build_gates.main(["--root", str(root), "--only", "leak-scan"]) == 1


def test_leaked_account_id_produces_no_artifact_PIPE_02(tmp_path: Path, ops_assembly: Path) -> None:
    root = _fixture_commit(tmp_path, leak=True)
    out = tmp_path / "build-output"
    synth_calls: list[Path] = []

    def synth(o: Path) -> Path:
        synth_calls.append(o)
        return _fake_synth(ops_assembly)(o)

    with pytest.raises(build_stage.BuildFailed, match="pre-synth gates failed"):
        build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", bundle_fn=_no_bundles, synth_fn=synth, only=("leak-scan",))
    assert not out.exists() and synth_calls == []
    assert not (root / ".build" / "stage").exists()


def test_clean_commit_builds_one_digest_addressed_output(tmp_path: Path, ops_assembly: Path) -> None:
    root = _fixture_commit(tmp_path, leak=False)
    out = tmp_path / "build-output"
    info = build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", bundle_fn=_no_bundles, synth_fn=_fake_synth(ops_assembly), only=("leak-scan", "cost", "pipeline-structure"), log=lambda _m: None)
    assert (out / "cdk.out" / "manifest.json").is_file() and (out / "config" / "beta.json").is_file()
    recorded = json.loads((out / "release-info.json").read_text())
    assert recorded["release_id"] == info.release_id and recorded["source_commit"] == COMMIT
    from scripts.release import assembly_digest

    assert recorded["artifact_digest"] == assembly_digest(ops_assembly) == assembly_digest(out / "cdk.out")
    with pytest.raises(build_stage.BuildFailed, match="already exists"):
        build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", bundle_fn=_no_bundles, synth_fn=_fake_synth(ops_assembly), only=("leak-scan",))
    with pytest.raises(build_stage.BuildFailed, match="commit"):
        build_stage.run_build(root, tmp_path / "o2", source_commit="main", region="us-east-2", synth_fn=_fake_synth(ops_assembly))


def test_post_synth_gate_failure_produces_no_artifact(tmp_path: Path, ops_assembly: Path) -> None:
    root = _fixture_commit(tmp_path, leak=False)
    bad = tmp_path / "bad-assembly"
    shutil.copytree(ops_assembly, bad)
    tpl = next(bad.glob("assembly-Beta/*Storage*.template.json"))
    doc = json.loads(tpl.read_text())
    doc["Resources"]["SyntheticNat"] = {"Type": "AWS::EC2::NatGateway", "Properties": {}}
    tpl.write_text(json.dumps(doc))
    out = tmp_path / "build-output"
    with pytest.raises(build_stage.BuildFailed, match="post-synth"):
        build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", bundle_fn=_no_bundles, synth_fn=_fake_synth(bad), only=("cost",), log=lambda _m: None)
    assert not out.exists()


def test_post_synth_gates_pass_on_the_synthesized_assembly(ops_assembly: Path) -> None:
    ctx = build_gates.GateContext(root=ROOT, assembly=ops_assembly)
    results = build_gates.run_gates(ctx, stage="post")
    assert {r.name for r in results} == {"ownership", "boundaries", "live-perm-scan", "pipeline-structure", "cost", "lambda-bundle"}
    # the offline assembly packages the source tree: only the lambda-bundle gate rejects it
    # (tests/unit/ops/test_lambda_bundle.py); a release synth (scripts/synth.py --release) passes it
    bundle = next(r for r in results if r.name == "lambda-bundle")
    assert not bundle.ok and all("source-only" in p for p in bundle.problems)
    results = [r for r in results if r.name != "lambda-bundle"]
    assert all(r.ok for r in results), [(r.name, r.problems[:3]) for r in results if not r.ok]
    assert not (ROOT / "scripts" / "ownership_known_gaps.json").exists()  # no accepted-gap list (contracts 0.2.0)
    ownership = next(r for r in results if r.name == "ownership")
    assert ownership.problems == [] and "templates checked" in " ".join(ownership.notes)


def test_ownership_gate_fails_an_unrecorded_resource(ops_assembly: Path, tmp_path: Path) -> None:
    bad = tmp_path / "asm"
    shutil.copytree(ops_assembly, bad)
    tpl = bad / "Tooling.template.json"
    doc = json.loads(tpl.read_text())
    doc["Resources"]["StrayQueue"] = {"Type": "AWS::SQS::Queue", "Properties": {"Tags": [{"Key": "logical-role", "Value": "stray"}]}}
    tpl.write_text(json.dumps(doc))
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT, assembly=bad), only=["ownership"])
    assert not res.ok and any("StrayQueue" in p for p in res.problems)


def test_live_permission_gate_fails_a_payment_grant(ops_assembly: Path, tmp_path: Path) -> None:
    bad = tmp_path / "asm"
    shutil.copytree(ops_assembly, bad)
    tpl = next(bad.glob("assembly-Beta/*Api*.template.json"))
    doc = json.loads(tpl.read_text())
    doc["Resources"]["PaymentPolicy"] = {"Type": "AWS::IAM::ManagedPolicy", "Properties": {"PolicyDocument": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "payments:*", "Resource": "*"}]}}}
    tpl.write_text(json.dumps(doc))
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT, assembly=bad), only=["live-perm-scan"])
    assert not res.ok


def test_config_gate_rejects_an_invalid_schedule_and_a_phase_1_real_provider(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for rel in ("pyproject.toml", "uv.lock", "platform", "tests", "scripts"):
        (root / rel).symlink_to(ROOT / rel)
    shutil.copytree(ROOT / "config", root / "config")
    (res,) = build_gates.run_gates(build_gates.GateContext(root=root), only=["config"])
    assert res.ok, res.problems
    beta = json.loads((root / "config" / "beta.json").read_text())
    beta["ingest"]["schedule_time"] = "10:00"
    beta["ingest"]["provider"] = "yfinance"
    (root / "config" / "beta.json").write_text(json.dumps(beta))
    (res,) = build_gates.run_gates(build_gates.GateContext(root=root), only=["config"])
    text = " ".join(res.problems)
    assert not res.ok and "ING-02" in text and "ING-10" in text


def test_config_gate_fails_a_drifted_shipped_calendar_ING_15(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for rel in ("pyproject.toml", "uv.lock", "tests", "scripts", "config"):
        (root / rel).symlink_to(ROOT / rel)
    cal_rel = Path("platform") / "finplan_platform" / "data" / "calendars"
    shutil.copytree(ROOT / cal_rel, root / cal_rel)
    (res,) = build_gates.run_gates(build_gates.GateContext(root=root), only=["config"])
    assert res.ok, res.problems
    (xnys,) = sorted((root / cal_rel).glob("xnys-*.json"))
    doc = json.loads(xnys.read_text())
    doc["sessions"] = doc["sessions"][1:] if isinstance(doc.get("sessions"), list) else doc.get("sessions")
    doc["tampered"] = True
    xnys.write_text(json.dumps(doc, indent=1) + "\n")
    (res,) = build_gates.run_gates(build_gates.GateContext(root=root), only=["config"])
    assert not res.ok and any("calendar (xnys)" in p for p in res.problems)


def test_ingestion_package_gate_reports_the_zip_decision_6_17() -> None:
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT), only=["ingestion-package"])
    assert res.ok, res.problems


def test_boundary_gate_needs_no_pseudo_parameter_workaround() -> None:
    """Contracts 0.2.0 resolves pseudo-parameter references inside Fn::Join; the gate checks templates as synthesized."""
    assert not hasattr(build_gates, "normalize_pseudo") and not hasattr(build_gates, "load_known_gaps")


def test_both_language_conformance_runs_in_the_build_stage_6_5b() -> None:
    """Contracts task 6.5b (CS-10): the pre gates run the Python and TypeScript conformance suites."""
    assert ("conformance-ts", "pre") in {(n, st) for n, st, _ in build_gates.GATES}
    if shutil.which("npm") is None:  # pragma: no cover - the build image and the dev host have Node.js
        pytest.skip("needs node/npm")
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT), only=["conformance-ts"])
    assert res.ok, res.problems
    notes = " ".join(res.notes)
    assert "python producer: PASS" in notes and "typescript producer: PASS" in notes and "typescript consumer: PASS" in notes


def test_conformance_ts_fails_without_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT), only=["conformance-ts"])
    assert not res.ok and any("node/npm not found" in p for p in res.problems)


def test_unknown_gate_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_gates.run_gates(build_gates.GateContext(), only=["nope"])


# ------------------------------------------------------------------ asset publishing
def test_deterministic_zip(tmp_path: Path) -> None:
    d = tmp_path / "src"
    (d / "pkg").mkdir(parents=True)
    (d / "pkg" / "a.py").write_text("x = 1\n")
    (d / "b.txt").write_text("b")
    assert publish_assets.deterministic_zip(d) == publish_assets.deterministic_zip(d)


def test_publish_assets_to_the_store_write_once(ops_assembly: Path, s3: Any) -> None:
    bucket = f"finplan-shared-financialplanning-pipeline-store-{FAKE_ACCOUNT}"
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
    first = publish_assets.publish(ops_assembly, s3, account=FAKE_ACCOUNT, region="us-east-2")
    keys = {p.key for p in first}
    assert first and all(p.uploaded for p in first) and all(k.startswith("assets/") for k in keys)
    assert not any(k.startswith("bootstrap/") for k in keys)  # the staged tooling template is the bootstrap's
    zips = [p for p in first if p.key.endswith(".zip")]
    assert zips  # Lambda code
    second = publish_assets.publish(ops_assembly, s3, account=FAKE_ACCOUNT, region="us-east-2")
    assert {p.key for p in second} == keys and not any(p.uploaded for p in second)


def test_publish_refuses_foreign_buckets_and_images(tmp_path: Path) -> None:
    asm = tmp_path / "asm"
    asm.mkdir()
    (asm / "manifest.json").write_text("{}")
    (asm / "X.assets.json").write_text(json.dumps({"files": {"h": {"source": {"path": "t.json", "packaging": "file"}, "destinations": {"d": {"bucketName": "example-other-bucket", "objectKey": "k"}}}}}))
    with pytest.raises(publish_assets.PublishError, match="outside the pipeline store"):
        publish_assets.planned_uploads(asm, account=FAKE_ACCOUNT, region="us-east-2")
    (asm / "X.assets.json").write_text(json.dumps({"files": {}, "dockerImages": {"img": {}}}))
    with pytest.raises(publish_assets.PublishError, match="container-image"):
        publish_assets.planned_uploads(asm, account=FAKE_ACCOUNT, region="us-east-2")


def test_account_from_the_codebuild_arn() -> None:
    assert publish_assets.account_from_build_arn("arn:aws:codebuild:us-east-2:<account-id>:build/p:1") == "<account-id>"
    assert publish_assets.account_from_build_arn(None) is None
