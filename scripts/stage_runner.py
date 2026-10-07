#!/usr/bin/env python3
"""Post-deploy actions of each environment stage (tasks 10.3, 10.4; PIPE-04, PIPE-05).

Runs in the per-environment stage project from the BuildOutput artifact only (never the source
checkout, never ``cdk synth``):

``publish``
    :func:`scripts.release.publish_release`: verifies the deploy published every platform output,
    writes the release manifest, ``current-release-id`` and the platform's
    ``budget-enforced-role-names``, and copies the manifest to the release ledger. In prod it first
    reads the manual approval (approver, time) of this pipeline execution.
``tests``
    The environment suite: ``integration-beta`` (beta) and ``gamma`` (gamma) run
    ``tests/integration``; ``smoke`` (prod) runs ``tests/smoke``. ``FINPLAN_TARGET_ENV`` and
    ``FINPLAN_SUITE`` tell the tests where they run; the opt-in live-provider test is always
    excluded (``-m "not live_provider"``). The smoke suite must execute at least one test (a smoke
    stage that skipped everything fails). The integration suites are tasks 11.1-11.3 (blocked by
    the bootstrap); until they exist the stage reports that it ran zero tests.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release import ReleaseInfo, approval_record, publish_release  # noqa: E402

__all__ = ["SUITES", "main", "publish_action", "suite_counts", "tests_action"]

SUITES: dict[str, tuple[str, list[str], bool]] = {
    # env -> (suite name, pytest paths, must execute at least one test)
    "beta": ("integration-beta", ["tests/integration"], False),
    "gamma": ("gamma", ["tests/integration"], False),
    "prod": ("smoke", ["tests/smoke"], True),
}


def publish_action(env: str, info: ReleaseInfo, *, ssm: Any, s3: Any | None, codepipeline: Any | None, store: str | None, pipeline_name: str, execution_id: str | None) -> dict[str, Any]:
    approval = None
    if env == "prod":
        if codepipeline is None or not execution_id:
            raise RuntimeError("the prod manifest needs the pipeline execution ID to read the approval")
        approval = approval_record(codepipeline, pipeline_name, execution_id)
    return publish_release(info, env, ssm=ssm, s3=s3, store_bucket=store, approval=approval)


def suite_counts(junit_xml: Path) -> dict[str, int]:
    root = ET.parse(junit_xml).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {k: 0 for k in ("tests", "failures", "errors", "skipped")}
    for s in suites:
        for k in total:
            total[k] += int(s.get(k, 0))
    total["executed"] = total["tests"] - total["skipped"]
    return total


def tests_action(env: str, *, root: Path = ROOT, release_id: str | None = None, run: Callable[..., Any] = subprocess.run, environ: Mapping[str, str] | None = None) -> int:
    suite, paths, must_execute = SUITES[env]
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        env_vars = {**(environ if environ is not None else os.environ), "FINPLAN_TARGET_ENV": env, "FINPLAN_SUITE": suite}
        if release_id:
            env_vars["FINPLAN_RELEASE_ID"] = release_id
        proc = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "not live_provider", f"--junitxml={junit}", *paths], cwd=root, env=env_vars)
        rc = int(getattr(proc, "returncode", 1))
        counts = suite_counts(junit) if junit.is_file() else {"tests": 0, "executed": 0, "failures": 0, "errors": 0, "skipped": 0}
    print(f"{suite}: {counts}")
    if rc == 5 or counts["executed"] == 0:  # pytest 5 = nothing collected
        if must_execute:
            print(f"FAIL: the {suite} suite executed no test", file=sys.stderr)
            return 1
        print(f"NOTE: the {suite} suite executed no test yet (environment suites are tasks 11.1-11.3)")
        return 0
    return 0 if rc == 0 else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CodeBuild entry point (needs AWS)
    ap = argparse.ArgumentParser(description="Post-deploy stage actions: publish the manifest or run the environment tests.")
    ap.add_argument("action", choices=("publish", "tests"))
    ap.add_argument("--env", required=True, choices=("beta", "gamma", "prod"))
    ap.add_argument("--release-info", type=Path, default=ROOT / "release-info.json")
    ap.add_argument("--pipeline-execution-id", default=None)
    ap.add_argument("--store", default=os.environ.get("FINPLAN_PIPELINE_STORE"))
    args = ap.parse_args(argv)
    info = ReleaseInfo.load(args.release_info)
    if args.action == "tests":
        return tests_action(args.env, release_id=info.release_id)
    import boto3

    from infra.stacks.tooling import PIPELINE_NAME

    session = boto3.session.Session(region_name=info.region)
    manifest = publish_action(
        args.env,
        info,
        ssm=session.client("ssm"),
        s3=session.client("s3"),
        codepipeline=session.client("codepipeline"),
        store=args.store,
        pipeline_name=PIPELINE_NAME,
        execution_id=args.pipeline_execution_id,
    )
    print(f"published {args.env} manifest for {manifest['release_id']} (previous {manifest['previous_release_id']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
