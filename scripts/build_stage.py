#!/usr/bin/env python3
"""The pipeline's Build stage (tasks 10.1, 10.2, 10.3; PIPE-02, PIPE-03, PIPE-06).

Normal build (``--rollback-to`` empty or ``none``):

1. pre-synth gates (:mod:`scripts.build_gates`): contracts pin, configuration, leak scan,
   copied-id, contract conformance, unit and contract tests;
2. the Lambda bundles (:mod:`scripts.lambda_bundle`: one per function, the locked dependency
   closure for python3.12/arm64 plus the platform package and config), then ``cdk synth`` once
   (:func:`scripts.synth.synth`, deployment synthesizer) in **release mode**
   (``FINPLAN_RELEASE_BUILD=1``, ``FINPLAN_LAMBDA_BUNDLE_DIR``): a function without a complete
   bundle fails the synth, so a source-only package is never built into a release (see
   docs/pipeline.md "Source-only Lambda bundle");
3. post-synth gates: ownership, boundaries, live-permission scan, pipeline structure, cost;
4. package BuildOutput: the cloud assembly, ``release-info.json`` (new ``release_id``, artifact
   digest, pinned contract version and digest, served majors) and the files the post-deploy
   actions need (project metadata, the platform package, config, scripts, tests; never a rebuild);
5. publish the file assets to the pipeline store and store BuildOutput in the release ledger.

Any failure raises :class:`BuildFailed` before anything is written to ``--out``, so a failing gate
produces **no artifact** (spec platform-pipeline "Leaked identifier").

Rollback (``--rollback-to rel_...``): no gate, no synth, no rebuild. The recorded release's stored
BuildOutput is fetched from the ledger, its digest re-verified and re-emitted with
``rollback: true`` so the manifests record ``rolled_back_from`` (contracts D6, PIPE-06).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_gates  # noqa: E402
from scripts.release import (  # noqa: E402
    ReleaseInfo,
    assembly_digest,
    contract_pin,
    fetch_build_output,
    mint_release_id,
    store_build_output,
)

__all__ = ["PACKAGE_PATHS", "BuildFailed", "main", "release_environment", "run_build", "run_rollback"]

#: Copied into BuildOutput for the post-deploy actions (they never read the source checkout).
PACKAGE_PATHS = ("pyproject.toml", "uv.lock", "README.md", "contracts-pin.json", "vendor", "platform", "config", "scripts", "tests", "infra")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", "cdk.out")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class BuildFailed(RuntimeError):
    pass


def _region(root: Path) -> str:
    env = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env:
        return env
    return str(json.loads((root / "config" / "shared.json").read_text(encoding="utf-8"))["region"])


def _served_majors() -> list[int]:
    from finplan_platform.core.upgrade import served_majors

    return list(served_majors())


def _default_synth(out: Path) -> Path:
    from scripts.synth import synth

    return synth(out)


def _default_bundles(root: Path, out: Path) -> dict[str, dict[str, Any]]:
    from scripts.lambda_bundle import build_all

    return build_all(root, out)


@contextlib.contextmanager
def release_environment(bundles: Path) -> Any:
    """``FINPLAN_RELEASE_BUILD=1`` and ``FINPLAN_LAMBDA_BUNDLE_DIR`` for the duration of the synth."""
    from infra.stacks.common import BUNDLE_DIR_ENV, RELEASE_ENV

    saved = {k: os.environ.get(k) for k in (BUNDLE_DIR_ENV, RELEASE_ENV, "FINPLAN_LAMBDA_BUNDLE")}
    os.environ[BUNDLE_DIR_ENV] = str(bundles)
    os.environ[RELEASE_ENV] = "1"
    os.environ.pop("FINPLAN_LAMBDA_BUNDLE", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_build(
    root: Path,
    out: Path,
    *,
    source_commit: str,
    s3: Any | None = None,
    store: str | None = None,
    account: str | None = None,
    region: str | None = None,
    run_unit: bool = True,
    rebuild_contracts: bool = True,
    synth_fn: Callable[[Path], Path] = _default_synth,
    bundle_fn: Callable[[Path, Path], dict[str, dict[str, Any]]] = _default_bundles,
    gates: tuple[str, ...] = ("pre", "post"),
    only: tuple[str, ...] | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> ReleaseInfo:
    if not _SHA.match(source_commit):
        raise BuildFailed("source commit must be the 40-character commit ID from the Source stage")
    if out.exists():
        raise BuildFailed(f"{out} already exists; the build output is produced once per build")
    region = region or _region(root)
    work = root / ".build" / "stage"
    bundles = root / ".build" / "lambda-bundles"
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(bundles, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        ctx = build_gates.GateContext(root=root, rebuild_contracts=rebuild_contracts, run_unit=run_unit)
        if "pre" in gates and not _gates_ok(ctx, "pre", only, log):
            raise BuildFailed("pre-synth gates failed; no artifact produced")
        from infra.stacks.common import SourceOnlyCodeError
        from scripts.lambda_bundle import BundleError

        try:
            manifests = bundle_fn(root, bundles)
        except BundleError as exc:
            raise BuildFailed(f"Lambda bundle build failed; no artifact produced: {exc}") from exc
        for name, m in sorted(manifests.items()):  # functions sharing extras share one bundle
            log(f"lambda bundle {name}: {m['unzipped_bytes'] / 2**20:.1f} MiB unzipped, {m['files']} files, {m['python_platform']}")
        (work / "lambda-bundles.json").write_text(json.dumps(manifests, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        try:
            with release_environment(bundles):
                assembly = synth_fn(work / "cdk.out")
        except SourceOnlyCodeError as exc:
            raise BuildFailed(f"release synth refused source-only Lambda code; no artifact produced: {exc}") from exc
        ctx.assembly = assembly
        if "post" in gates and not _gates_ok(ctx, "post", only, log):
            raise BuildFailed("post-synth gates failed; no artifact produced")
        for rel in PACKAGE_PATHS:
            src = root / rel
            if src.is_dir():
                shutil.copytree(src, work / rel, ignore=_IGNORE)
            elif src.is_file():
                shutil.copy2(src, work / rel)
        version, digest = contract_pin(root)
        info = ReleaseInfo(
            release_id=mint_release_id(now),
            source_commit=source_commit,
            artifact_digest=assembly_digest(assembly),
            contract_version=version,
            contract_digest=digest,
            served_contract_majors=_served_majors(),
            region=region,
            built_at=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        (work / "release-info.json").write_text(info.to_json(), encoding="utf-8")
        if s3 is not None:
            if not (store and account):
                raise BuildFailed("publishing needs the pipeline store name and the account")
            from scripts.publish_assets import publish

            for p in publish(assembly, s3, account=account, region=region):
                log(f"asset {'uploaded' if p.uploaded else 'present'}: {p.key}")
            store_build_output(s3, store, info, work)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(out))
        log(f"release {info.release_id} digest {info.artifact_digest}")
        return info
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(bundles, ignore_errors=True)
        with contextlib.suppress(OSError):
            work.parent.rmdir()  # .build/ when empty


def _names(stage: str) -> set[str]:
    return {name for name, gstage, _ in build_gates.GATES if gstage == stage}


def _gates_ok(ctx: build_gates.GateContext, stage: str, only: tuple[str, ...] | None, log: Callable[[str], None]) -> bool:
    selected = None if only is None else [g for g in only if g in _names(stage)]
    if selected == []:
        return True
    return build_gates.report(build_gates.run_gates(ctx, stage=stage, only=selected), log)


def run_rollback(release_id: str, out: Path, *, s3: Any, store: str, log: Callable[[str], None] = print) -> ReleaseInfo:
    if out.exists():
        raise BuildFailed(f"{out} already exists")
    info = fetch_build_output(s3, store, release_id, out)
    log(f"rollback: re-emitting stored release {release_id} (digest {info.artifact_digest}); nothing rebuilt")
    return info


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CodeBuild entry point (needs AWS for publishing)
    ap = argparse.ArgumentParser(description="Platform pipeline Build stage.")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source-commit", default="")
    ap.add_argument("--rollback-to", default="none")
    ap.add_argument("--store", default=os.environ.get("FINPLAN_PIPELINE_STORE"))
    ap.add_argument("--no-publish", action="store_true", help="local run: no asset publishing or release ledger")
    args = ap.parse_args(argv)
    s3 = None
    account = None
    if not args.no_publish:
        import boto3

        from scripts.publish_assets import account_from_build_arn

        s3 = boto3.client("s3")
        account = account_from_build_arn(os.environ.get("CODEBUILD_BUILD_ARN"))
    try:
        if args.rollback_to and args.rollback_to not in ("none", ""):
            if s3 is None or not args.store:
                raise BuildFailed("rollback needs the pipeline store")
            run_rollback(args.rollback_to, args.out, s3=s3, store=args.store)
        else:
            run_build(ROOT, args.out, source_commit=args.source_commit, s3=s3, store=args.store, account=account)
    except BuildFailed as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
