#!/usr/bin/env python3
"""Build-stage gates of the platform pipeline (task 10.2; PIPE-02; contracts D6 build stage).

Spec platform-pipeline "Build-stage gates": the build stage fails on any failure of the unit
tests, the contract conformance suite for the pinned version, the ownership check, the
identifier/secret leak scan, the live-financial permission scan, the cost-tag and no-always-on
checks, and the schedule-time and phase 1 provider configuration checks. Every gate reuses the
pinned contract package (:mod:`finplan_contracts`) or an existing platform check; nothing is
re-implemented or copied.

Gates (``pre`` run on the source tree, ``post`` on the synthesized cloud assembly):

=====================  =====  ==============================================================
gate                   stage  what
=====================  =====  ==============================================================
contracts-pin          pre    ``scripts/check_contracts_pin.py`` (+ ``--rebuild`` in the pipeline)
config                 pre    every ``config/<env>.json`` validates (ING-02 schedule, ING-10 phase 1
                              provider, ING-13 daily only), ``shared.json``, ingest pins
                              (``scripts/check_ingest_pins.py``), data hygiene
                              (``scripts/check_data_hygiene.py``) and the shipped XNYS and
                              fixture calendars (``scripts/generate_calendar.py --check``, ING-15)
leak-scan              pre    ``finplan_contracts.leak_scan`` over the repository (OWN-03, ENV-08)
copied-id              pre    ``finplan_contracts.copied_id`` (CS-01); ``contracts/`` (the producer
                              source) is excluded, planning documents under ``openspec/`` are scanned
conformance            pre    ``finplan-conformance conformance --mode consumer --expect-version <pin>``
conformance-ts         pre    the both-language suite on the contract source (task 6.5b, CS-10): Python
                              producer mode over ``contracts/``, then ``npm ci`` (when
                              ``node_modules`` is absent), ``npm run build`` and the TypeScript
                              runner in producer mode and in consumer mode (``--expect-version
                              <pin>``); needs Node.js (CodeBuild installs nodejs 22)
ingestion-package      pre    ``scripts/ingestion_package_size.py``: the ingestion dependency
                              closure fits a zip Lambda (task 6.17; image assets not published yet)
unit                   pre    ``pytest tests/unit tests/contract -m "not live_provider"``
ownership              post   ``finplan_contracts.ownership`` per template (OWN-01, ENV-16); every
                              problem fails the gate (no accepted-gap list)
boundaries             post   ``check_role_boundaries`` (ENV-18) + ``check_shared_resources`` (ENV-16)
live-perm-scan         post   ``finplan_contracts.live_perms`` over every template (ENV-05)
pipeline-structure     post   ``finplan_contracts.pipeline_check`` on the pipeline template (ENV-09)
                              and ``bootstrap.check_deploy_roles`` (scoped deploy roles, ENV-12)
cost                   post   ``scripts/cost_checks.py`` (COST-03 tags, COST-04 no always-on)
lambda-bundle          post   every platform Lambda code asset is a complete arm64 bundle from
                              ``scripts/lambda_bundle.py`` (contract package, dependencies,
                              config, manifest, size within the 250 MB unzipped limit), never
                              the source tree (docs/pipeline.md "Source-only Lambda bundle")
=====================  =====  ==============================================================

The boundary gate checks the synthesized templates as they are: since contracts 0.2.0 the
boundary checker resolves pseudo-parameter references inside ``Fn::Join``/``Fn::Sub`` itself.

Usage: ``python scripts/build_gates.py --stage pre|post|all [--assembly cdk.out] [--only GATE ...]``;
exit 1 when any gate fails. Runs offline.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["GATES", "GateContext", "GateResult", "main", "run_gates", "templates_of"]


@dataclass
class GateContext:
    root: Path = ROOT
    assembly: Path | None = None
    rebuild_contracts: bool = False
    run_unit: bool = True
    extra_pytest_args: tuple[str, ...] = ()
    notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, gate: str, text: str) -> None:
        self.notes.setdefault(gate, []).append(text)


@dataclass
class GateResult:
    name: str
    problems: list[str]
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ===================================================================== helpers
def templates_of(assembly: Path) -> list[Path]:
    return sorted(assembly.rglob("*.template.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def _call_main(fn: Callable[[list[str]], int], argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = fn(argv)
        except SystemExit as exc:  # argparse
            rc = int(exc.code or 0)
    return rc, out.getvalue()[-4000:]


# ===================================================================== pre gates
def gate_contracts_pin(ctx: GateContext) -> list[str]:
    from scripts.check_contracts_pin import check

    return [f"contracts pin: {p}" for p in check(ctx.root, rebuild=ctx.rebuild_contracts)]


def gate_config(ctx: GateContext) -> list[str]:
    from finplan_platform.core.config import ConfigError, load_all, load_shared_config

    problems: list[str] = []
    cfg_dir = ctx.root / "config"
    try:
        load_all(cfg_dir)
        load_shared_config(cfg_dir)
    except ConfigError as exc:
        problems += [f"config: {p}" for p in exc.problems]
    except (OSError, ValueError) as exc:
        problems.append(f"config: {exc}")
    from scripts import check_data_hygiene, check_ingest_pins

    problems += [f"ingest pins: {p}" for p in check_ingest_pins.check(ctx.root / "pyproject.toml", ctx.root / "uv.lock", ctx.root / "platform" / "finplan_platform" / "data" / "calendars")]
    rc, out = _call_main(check_data_hygiene.main, ["--root", str(ctx.root)])
    if rc != 0:
        problems.append("data hygiene: " + out.strip().replace("\n", " | "))
    from scripts import generate_calendar

    cal_dir = ctx.root / "platform" / "finplan_platform" / "data" / "calendars"
    for source in ("xnys", "fixture"):
        problems += [f"calendar ({source}): {p}" for p in generate_calendar.check(source, cal_dir, ctx.root / "pyproject.toml")]
    return problems


TS_DIR = Path("contracts") / "typescript"


def _last_line(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "(no output)"


def gate_conformance_ts(ctx: GateContext) -> list[str]:
    """Task 6.5b (CS-10): the same both-language conformance suite the contract package runs locally."""
    import shutil as _shutil

    from finplan_contracts import conformance

    from scripts.release import contract_pin

    version, _ = contract_pin(ctx.root)
    problems: list[str] = []
    rc, out = _call_main(conformance.main, ["--mode", "producer", "--root", str(ctx.root / "contracts")])
    ctx.note("conformance-ts", "python producer: " + _last_line(out))
    if rc != 0:
        problems.append("conformance (python, producer mode on contracts/) failed: " + _last_line(out))
    npm, node = _shutil.which("npm"), _shutil.which("node")
    if not (npm and node):
        return [*problems, "conformance (typescript) cannot run: node/npm not found on PATH (the build image installs nodejs 22)"]
    ts = ctx.root / TS_DIR
    steps: list[tuple[str, list[str]]] = []
    if not (ts / "node_modules").is_dir():
        steps.append(("npm ci", [npm, "ci", "--no-audit", "--no-fund"]))
    steps += [
        ("npm run build", [npm, "run", "build", "--silent"]),
        ("typescript producer", [node, "dist/cli.js", "conformance", "--mode", "producer", "--root", str(ctx.root / "contracts")]),
        ("typescript consumer", [node, "dist/cli.js", "conformance", "--mode", "consumer", "--expect-version", version]),
    ]
    for label, cmd in steps:
        rc, out = _run(cmd, ts)
        if label.startswith("typescript"):
            ctx.note("conformance-ts", f"{label}: {_last_line(out)}")
        if rc != 0:
            problems.append(f"conformance ({label}) failed: {_last_line(out)}")
            break
    return problems


def gate_ingestion_package(ctx: GateContext) -> list[str]:
    """Task 6.17: the ingestion dependency closure must fit a zip Lambda (image assets are not published yet)."""
    from scripts import ingestion_package_size

    rep = ingestion_package_size.report()
    ctx.note("ingestion-package", f"{rep['total_unzipped_bytes'] // (1024 * 1024)} MiB unzipped, mode {rep['mode']}")
    if rep["mode"] != "zip":
        return [f"ingestion dependency closure ({rep['total_unzipped_bytes']} bytes) exceeds the zip budget ({rep['budget_with_margin_bytes']} bytes); switch the ingestion function to the container-image fallback and add image publishing to the pipeline"]
    return []


def gate_leak_scan(ctx: GateContext) -> list[str]:
    from finplan_contracts import leak_scan

    n, findings = leak_scan.scan_paths([ctx.root], exclude_dirs=leak_scan.DEFAULT_EXCLUDE_DIRS | {".build", "build-output"})
    ctx.note("leak-scan", f"{n} files scanned")
    return [f"leak: {f}" for f in findings]


def gate_copied_id(ctx: GateContext) -> list[str]:
    from finplan_contracts import copied_id

    found = copied_id.scan_tree(ctx.root, exclude_dirs=copied_id.EXCLUDE_DIRS | {"contracts", ".claude", ".build", "build-output"})
    return [f"copied contract schema: {c}" for c in found]


def gate_conformance(ctx: GateContext) -> list[str]:
    from finplan_contracts import conformance

    from scripts.release import contract_pin

    version, _ = contract_pin(ctx.root)
    rc, out = _call_main(conformance.main, ["--mode", "consumer", "--expect-version", version])
    return [] if rc == 0 else ["conformance (consumer mode) failed: " + out.strip().splitlines()[-1] if out.strip() else "conformance failed"]


def gate_unit(ctx: GateContext) -> list[str]:
    if not ctx.run_unit:
        ctx.note("unit", "skipped by request")
        return []
    rc, out = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "not live_provider", "tests/unit", "tests/contract", *ctx.extra_pytest_args], ctx.root)
    return [] if rc == 0 else ["unit/contract tests failed: " + (out.strip().splitlines() or ["?"])[-1]]


# ===================================================================== post gates
def _need_assembly(ctx: GateContext) -> Path:
    if ctx.assembly is None or not (ctx.assembly / "manifest.json").is_file():
        raise FileNotFoundError("post-synth gates need a synthesized cloud assembly (--assembly)")
    return ctx.assembly


def gate_ownership(ctx: GateContext) -> list[str]:
    from finplan_contracts.ownership import check_template

    problems: list[str] = []
    templates = templates_of(_need_assembly(ctx))
    for path in templates:
        report = check_template(_load(path), "financialplanning", name=path.name).to_dict()
        problems += [f"ownership {path.name}: {p['logical_id']} ({p['resource_type']}): {p['message']}" for p in report["problems"]]
    ctx.note("ownership", f"{len(templates)} templates checked")
    return problems


def gate_boundaries(ctx: GateContext) -> list[str]:
    from finplan_contracts.boundaries import (
        check_role_boundaries,
        check_shared_resources,
    )

    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        problems += [f"boundary {path.name}: {f}" for f in check_role_boundaries(t)]
        problems += [f"shared {path.name}: {f}" for f in check_shared_resources(t, repo="financialplanning")]
    return problems


def gate_live_perms(ctx: GateContext) -> list[str]:
    from finplan_contracts import live_perms

    n, findings = live_perms.scan_paths(templates_of(_need_assembly(ctx)))
    ctx.note("live-perm-scan", f"{n} templates scanned")
    return [f"live-financial permission: {f}" for f in findings]


def gate_pipeline_structure(ctx: GateContext) -> list[str]:
    from finplan_contracts.bootstrap import check_deploy_roles
    from finplan_contracts.pipeline_check import check_pipeline_template

    found = 0
    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        if not any(isinstance(r, dict) and r.get("Type") == "AWS::CodePipeline::Pipeline" for r in (t.get("Resources") or {}).values()):
            continue
        found += 1
        problems += [f"pipeline {path.name}: {f}" for f in check_pipeline_template(t)]
        problems += [f"deploy roles {path.name}: {f}" for f in check_deploy_roles(t)]
    if found != 1:
        problems.append(f"expected exactly one pipeline template in the assembly, found {found}")
    return problems


def lambda_code_assets(assembly: Path) -> dict[str, Path]:
    """Every zip file asset of the assembly that carries the platform package (Lambda code)."""
    found: dict[str, Path] = {}
    for manifest in sorted(assembly.rglob("*.assets.json")):
        for asset_id, asset in (_load(manifest).get("files") or {}).items():
            src = asset.get("source") or {}
            if src.get("packaging") != "zip" or not src.get("path"):
                continue
            path = (manifest.parent / src["path"]).resolve()
            if (path / "finplan_platform").is_dir():
                found[asset_id] = path
    return found


def gate_lambda_bundle(ctx: GateContext) -> list[str]:
    """Every platform Lambda code asset is a complete build-stage bundle, never the source tree."""
    from infra.stacks.common import bundle_problems

    from scripts.lambda_bundle import LAMBDA_PLATFORM, MANIFEST, foreign_binaries, verify_bundle

    problems: list[str] = []
    assets = lambda_code_assets(_need_assembly(ctx))
    if not assets:
        problems.append("no platform Lambda code asset found in the assembly")
    for asset_id, path in sorted(assets.items()):
        issues = bundle_problems(path) or verify_bundle(path)
        if issues:
            problems.append(f"Lambda code asset {asset_id[:12]} is not a deployable bundle (source-only package?): {'; '.join(issues)}")
            continue
        manifest = _load(path / MANIFEST)
        if manifest.get("python_platform") != LAMBDA_PLATFORM:
            problems.append(f"Lambda code asset {asset_id[:12]} was built for {manifest.get('python_platform')}, not {LAMBDA_PLATFORM}")
            continue
        foreign = foreign_binaries(path)
        if foreign:
            problems.append(f"Lambda code asset {asset_id[:12]} has {len(foreign)} non-arm64 shared objects, e.g. {foreign[0]}")
        ctx.note("lambda-bundle", f"{'+'.join(manifest.get('functions') or [str(manifest.get('function'))])} {manifest.get('unzipped_bytes', 0) // 2**20} MiB")
    return problems


def gate_cost(ctx: GateContext) -> list[str]:
    from scripts.cost_checks import check_paths

    n, findings = check_paths([_need_assembly(ctx)])
    return [str(f) for f in findings] + ([] if n else ["no templates found"])


Gate = tuple[str, str, Callable[[GateContext], list[str]]]
GATES: tuple[Gate, ...] = (
    ("contracts-pin", "pre", gate_contracts_pin),
    ("config", "pre", gate_config),
    ("leak-scan", "pre", gate_leak_scan),
    ("copied-id", "pre", gate_copied_id),
    ("conformance", "pre", gate_conformance),
    ("conformance-ts", "pre", gate_conformance_ts),
    ("ingestion-package", "pre", gate_ingestion_package),
    ("unit", "pre", gate_unit),
    ("ownership", "post", gate_ownership),
    ("boundaries", "post", gate_boundaries),
    ("live-perm-scan", "post", gate_live_perms),
    ("pipeline-structure", "post", gate_pipeline_structure),
    ("cost", "post", gate_cost),
    ("lambda-bundle", "post", gate_lambda_bundle),
)


def run_gates(ctx: GateContext, *, stage: str = "all", only: Iterable[str] | None = None) -> list[GateResult]:
    selected = set(only or ())
    unknown = selected - {g[0] for g in GATES}
    if unknown:
        raise ValueError(f"unknown gates: {sorted(unknown)}")
    results: list[GateResult] = []
    for name, gstage, fn in GATES:
        if selected and name not in selected:
            continue
        if not selected and stage != "all" and gstage != stage:
            continue
        try:
            problems = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a crashing gate is a failing gate
            problems = [f"{name} could not run: {type(exc).__name__}: {exc}"]
        results.append(GateResult(name, problems, ctx.notes.get(name, [])))
    return results


def report(results: list[GateResult], out: Callable[[str], None] = print) -> bool:
    for r in results:
        out(f"[{'PASS' if r.ok else 'FAIL'}] {r.name}" + (f" ({'; '.join(r.notes)})" if r.notes else ""))
        for p in r.problems[:200]:
            out(f"    {p}")
    return all(r.ok for r in results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the platform build-stage gates (PIPE-02).")
    ap.add_argument("--stage", choices=("pre", "post", "all"), default="all")
    ap.add_argument("--only", nargs="*", help="run only these gates")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--assembly", type=Path, help="synthesized cloud assembly (post gates)")
    ap.add_argument("--rebuild-contracts", action="store_true", help="contracts-pin gate also rebuilds the wheel from contracts/python")
    ap.add_argument("--skip-unit", action="store_true")
    args = ap.parse_args(argv)
    ctx = GateContext(root=args.root.resolve(), assembly=args.assembly.resolve() if args.assembly else None, rebuild_contracts=args.rebuild_contracts, run_unit=not args.skip_unit)
    ok = report(run_gates(ctx, stage=args.stage, only=args.only))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
