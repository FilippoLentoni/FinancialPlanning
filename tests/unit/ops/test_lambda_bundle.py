"""Lambda code bundles and the release-mode synth (docs/pipeline.md "Source-only Lambda bundle").

Regression for the production defect of 2026-10-07: every deployed function was the bare
``platform/`` source tree (~150 KB) and failed at init with ``Runtime.ImportModuleError: No module
named 'finplan_contracts'``; the plan-api role also lacked log-stream permissions, so the failure
left no log.

* the bundle builder (:mod:`scripts.lambda_bundle`) is run for real, for this interpreter's
  platform, from ``uv.lock``; every function's handler then imports in a fresh ``python -I -S``
  whose only non-stdlib path is the bundle;
* a release-mode synth refuses source-only code (missing or incomplete bundle), and the
  ``lambda-bundle`` post gate rejects a source-only or non-arm64 assembly;
* the build stage builds the bundles and synthesizes in release mode;
* the stage runner's API probe names the Lambda import problem on 502/INTERNAL.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from infra.stacks.common import BUNDLE_DIR_ENV, RELEASE_ENV, SourceOnlyCodeError, lambda_code, release_mode
from scripts import build_gates, build_stage, lambda_bundle, stage_runner

ROOT = Path(__file__).resolve().parents[3]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(scope="session")
def local_bundles(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, dict[str, Any]]]:
    out = tmp_path_factory.mktemp("lambda-bundles")
    return out, lambda_bundle.build_all(ROOT, out, python_platform=None)


# ------------------------------------------------------------------ bundle builder
def test_every_function_has_a_bundle_spec_matching_the_stacks() -> None:
    assert set(lambda_bundle.FUNCTIONS) == {"plan-api", "ingestion", "sweeper", "daily-trigger"}
    for name in ("plan-api", "ingestion"):
        assert "providers" in lambda_bundle.FUNCTIONS[name].extras
    for stack, function, handler in (("api", "plan-api", "handlers.api.handler"), ("ingestion", "ingestion", "handlers.ingest.handler"), ("metadata", "sweeper", "handlers.sweep.handler"), ("daily_trigger", "daily-trigger", "handlers.daily_trigger.handler")):
        src = (ROOT / "infra" / "stacks" / f"{stack}.py").read_text(encoding="utf-8")
        assert f'lambda_code("{function}")' in src and handler in src
        assert lambda_bundle.FUNCTIONS[function].handler.endswith(handler)


@pytest.mark.parametrize("function", sorted(lambda_bundle.FUNCTIONS))
def test_bundle_contains_the_contract_package_and_the_locked_dependencies(local_bundles: tuple[Path, dict[str, Any]], function: str) -> None:
    out, manifests = local_bundles
    bundle = out / function
    spec = lambda_bundle.FUNCTIONS[function]
    assert lambda_bundle.verify_bundle(bundle, spec) == []
    for name in ("finplan_platform", "finplan_contracts", "jsonschema", "rfc8785", "openpyxl", "defusedxml", "config", *spec.requires):
        assert (bundle / name).exists(), name
    pinned = json.loads((ROOT / "contracts-pin.json").read_text())["version"]
    assert (bundle / f"finplan_contracts-{pinned}.dist-info").is_dir()  # the pinned, vendored wheel
    assert any((bundle / "finplan_platform" / "data" / "calendars").glob("xnys-exchange_calendars-*.json"))
    assert not list(bundle.rglob("__pycache__"))
    manifest = json.loads((bundle / lambda_bundle.MANIFEST).read_text())
    assert manifest == manifests[function] and manifest["functions"][function] == spec.handler
    assert manifest["unzipped_bytes"] <= lambda_bundle.LAMBDA_UNZIPPED_LIMIT


@pytest.mark.parametrize("function", sorted(lambda_bundle.FUNCTIONS))
def test_handler_imports_with_only_the_bundle_on_sys_path(local_bundles: tuple[Path, dict[str, Any]], function: str) -> None:
    out, _ = local_bundles
    lambda_bundle.import_check(out / function, lambda_bundle.FUNCTIONS[function])


def test_import_check_detects_a_missing_contract_package(local_bundles: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    broken = tmp_path / "sweeper"
    shutil.copytree(local_bundles[0] / "sweeper", broken)
    shutil.rmtree(broken / "finplan_contracts")
    with pytest.raises(lambda_bundle.BundleError, match="finplan_contracts"):
        lambda_bundle.import_check(broken, lambda_bundle.FUNCTIONS["sweeper"])
    assert any("finplan_contracts" in p for p in lambda_bundle.verify_bundle(broken))


def test_functions_with_the_same_extras_share_one_identical_bundle(local_bundles: tuple[Path, dict[str, Any]]) -> None:
    out, manifests = local_bundles
    assert manifests["plan-api"] == manifests["ingestion"] and set(manifests["plan-api"]["functions"]) == {"plan-api", "ingestion"}
    assert (out / "plan-api" / lambda_bundle.MANIFEST).read_bytes() == (out / "ingestion" / lambda_bundle.MANIFEST).read_bytes()
    assert manifests["sweeper"]["unzipped_bytes"] < manifests["ingestion"]["unzipped_bytes"]


def test_source_tree_is_not_a_bundle() -> None:
    problems = lambda_bundle.verify_bundle(ROOT / "platform")
    assert any("bundle-manifest.json" in p for p in problems) and any("finplan_contracts" in p for p in problems)


# ------------------------------------------------------------------ release-mode synth
def test_release_mode_detection() -> None:
    assert release_mode({RELEASE_ENV: "1"}) and release_mode({"CODEBUILD_BUILD_ID": "b:1"})
    assert not release_mode({}) and not release_mode({"CODEBUILD_BUILD_ID": "b:1", RELEASE_ENV: "0"})


def test_release_synth_refuses_source_only_code(tmp_path: Path) -> None:
    with pytest.raises(SourceOnlyCodeError, match="finplan_contracts"):
        lambda_code("plan-api", environ={RELEASE_ENV: "1"})
    with pytest.raises(SourceOnlyCodeError):
        lambda_code("plan-api", environ={"CODEBUILD_BUILD_ID": "build:1"})
    with pytest.raises(SourceOnlyCodeError, match="incomplete"):
        lambda_code("plan-api", environ={RELEASE_ENV: "1", BUNDLE_DIR_ENV: str(tmp_path)})  # no plan-api/ inside
    partial = tmp_path / "plan-api"
    shutil.copytree(ROOT / "platform" / "finplan_platform", partial / "finplan_platform")
    with pytest.raises(SourceOnlyCodeError, match="finplan_contracts missing"):
        lambda_code("plan-api", environ={RELEASE_ENV: "1", BUNDLE_DIR_ENV: str(tmp_path)})
    lambda_code("plan-api", environ={})  # local/offline synth: the source tree is still allowed


def test_release_synth_of_an_environment_fails_without_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    import aws_cdk as cdk

    from infra.app import build_app

    monkeypatch.setenv(RELEASE_ENV, "1")
    monkeypatch.delenv(BUNDLE_DIR_ENV, raising=False)
    with pytest.raises(SourceOnlyCodeError):
        build_app(cdk.App(), ["beta"], app_modules=())


def test_release_synth_uses_the_bundles_and_the_gate_checks_them(local_bundles: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.synth import synth

    monkeypatch.setenv(RELEASE_ENV, "1")
    monkeypatch.setenv(BUNDLE_DIR_ENV, str(local_bundles[0]))
    asm = synth(tmp_path / "cdk.out", envs=["beta"])
    assets = build_gates.lambda_code_assets(asm)
    assert len(assets) == 2  # plan-api+ingestion share one bundle; the sweeper has its own
    assert all((p / "finplan_contracts").is_dir() for p in assets.values())
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT, assembly=asm), only=["lambda-bundle"])
    # host-platform bundles are importable here but are not the Lambda arm64 target
    assert not res.ok and all(lambda_bundle.LAMBDA_PLATFORM in p for p in res.problems)


def test_lambda_bundle_gate_rejects_a_source_only_assembly(ops_assembly: Path) -> None:
    (res,) = build_gates.run_gates(build_gates.GateContext(root=ROOT, assembly=ops_assembly), only=["lambda-bundle"])
    assert not res.ok and all("source-only" in p for p in res.problems)


# ------------------------------------------------------------------ build stage
def _commit(tmp_path: Path) -> Path:
    root = tmp_path / "commit"
    shutil.copytree(ROOT / "config", root / "config")
    shutil.copy(ROOT / "contracts-pin.json", root / "contracts-pin.json")
    (root / "platform").mkdir()
    return root


def test_build_stage_builds_bundles_then_synthesizes_in_release_mode(tmp_path: Path, ops_assembly: Path) -> None:
    root = _commit(tmp_path)
    seen: dict[str, Any] = {}

    def bundles(r: Path, out: Path) -> dict[str, dict[str, Any]]:
        seen["bundles"] = out
        return {"sweeper": {"unzipped_bytes": 1, "files": 1, "python_platform": lambda_bundle.LAMBDA_PLATFORM}}

    def synth(out: Path) -> Path:
        import os

        seen["env"] = (os.environ.get(RELEASE_ENV), os.environ.get(BUNDLE_DIR_ENV))
        shutil.copytree(ops_assembly, out)
        return out

    out = tmp_path / "build-output"
    build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", synth_fn=synth, bundle_fn=bundles, only=("leak-scan",), log=lambda _m: None)
    assert seen["env"] == ("1", str(seen["bundles"]))
    assert json.loads((out / "lambda-bundles.json").read_text())["sweeper"]["files"] == 1
    assert not seen["bundles"].exists()  # bundles live in the assembly's assets, not in BuildOutput twice


def test_build_stage_fails_when_the_release_synth_finds_no_bundle(tmp_path: Path) -> None:
    root = _commit(tmp_path)

    def synth(out: Path) -> Path:
        lambda_code("plan-api")  # what every platform stack does
        raise AssertionError("unreachable: release synth must refuse")

    out = tmp_path / "build-output"
    with pytest.raises(build_stage.BuildFailed, match="source-only"):
        build_stage.run_build(root, out, source_commit=COMMIT, region="us-east-2", synth_fn=synth, bundle_fn=lambda r, o: {}, only=("leak-scan",), log=lambda _m: None)
    assert not out.exists()


def test_build_spec_sets_release_mode() -> None:
    from infra.stacks.pipeline import build_spec

    assert build_spec()["env"]["variables"][RELEASE_ENV] == "1"


# ------------------------------------------------------------------ logging permissions
def test_plan_api_role_may_write_its_own_log_group(ops_assembly: Path) -> None:
    tpl = json.loads(next(ops_assembly.glob("assembly-Beta/*Api*.template.json")).read_text())
    res = tpl["Resources"]
    statements = [s for r in res.values() if r["Type"] == "AWS::IAM::Policy" for s in r["Properties"]["PolicyDocument"]["Statement"]]
    logs = [s for s in statements if s.get("Sid") == "OwnLogStreams"]
    assert len(logs) == 1 and set(logs[0]["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}
    group = next(k for k, r in res.items() if r["Type"] == "AWS::Logs::LogGroup")
    assert logs[0]["Resource"] == {"Fn::GetAtt": [group, "Arn"]}


def test_plan_api_role_may_call_only_the_finance_model_lineage_route(ops_assembly: Path) -> None:
    """STG-03 regression: acceptance calls FinanceModel's registry lineage route with this role."""
    tpl = json.loads(next(ops_assembly.glob("assembly-Beta/*Api*.template.json")).read_text())
    statements = [s for r in tpl["Resources"].values() if r["Type"] == "AWS::IAM::Policy" for s in r["Properties"]["PolicyDocument"]["Statement"]]
    invoke = [s for s in statements if "execute-api:Invoke" in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
    assert [s.get("Sid") for s in invoke] == ["FinanceModelRegistryLineage"]
    assert invoke[0]["Effect"] == "Allow"
    assert json.dumps(invoke[0]["Resource"]).endswith(':*/*/GET/v1/registry/lineage/*"]]}')


# ------------------------------------------------------------------ post-deploy API probe
class _Transport:
    def __init__(self, code: int, body: dict[str, Any]) -> None:
        self.code, self.body, self.calls = code, body, []

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.calls.append((method, path))
        return self.code, self.body, {}


@pytest.mark.parametrize(("code", "body"), [(502, {"code": "INTERNAL", "message": "internal error"}), (502, {}), (500, {"message": "Internal server error"})])
def test_api_probe_names_the_lambda_import_problem(code: int, body: dict[str, Any]) -> None:
    t = _Transport(code, body)
    with pytest.raises(RuntimeError, match="ImportModuleError") as err:
        stage_runner.probe_api(t)
    assert "finplan_contracts" in str(err.value) and t.calls == [("GET", stage_runner.PROBE_PATH)]


def test_api_probe_passes_a_contract_not_found() -> None:
    assert stage_runner.probe_api(_Transport(404, {"code": "NOT_FOUND"}))[0] == 404
    assert stage_runner.api_failure_message(403, {"code": "FORBIDDEN"}) is None


def test_foreign_binaries_detects_non_arm64_extension_modules(local_bundles: tuple[Path, dict[str, Any]]) -> None:
    import platform

    bundle = local_bundles[0] / "sweeper"
    if platform.machine() in ("aarch64", "arm64"):
        assert lambda_bundle.foreign_binaries(bundle) == []
    else:  # an x86 host build: every extension module (rpds, cffi, yaml, ...) is foreign to arm64
        assert any("rpds" in p for p in lambda_bundle.foreign_binaries(bundle))


def test_bundle_is_reproducible_and_carries_no_build_host_path(local_bundles: tuple[Path, dict[str, Any]], tmp_path: Path) -> None:
    out, _ = local_bundles
    assert not list(out.glob("*/*.dist-info/direct_url.json")) and not list(out.glob("*/*.dist-info/uv_cache.json"))
    # every function without extras shares the sweeper's bundle (the daily-trigger step function too)
    again = lambda_bundle.build_all(ROOT, tmp_path / "again", functions={k: v for k, v in lambda_bundle.FUNCTIONS.items() if not v.extras}, python_platform=None)
    first = sorted((p.relative_to(out / "sweeper").as_posix(), p.read_bytes()) for p in (out / "sweeper").rglob("*") if p.is_file())
    second = sorted((p.relative_to(tmp_path / "again" / "sweeper").as_posix(), p.read_bytes()) for p in (tmp_path / "again" / "sweeper").rglob("*") if p.is_file())
    assert first == second and again["sweeper"]["files"] == len(first) - 1  # the manifest counts every file but itself
    assert not (out / "sweeper" / ".lock").exists()
