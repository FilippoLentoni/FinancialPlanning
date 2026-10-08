#!/usr/bin/env python3
"""Pipeline synthesis of the cloud assembly (task 10.1; contracts D6 "cloud assembly built once").

Same app as ``infra/app.py`` (``build_app``), but the environment stacks use
:func:`infra.stacks.tooling.deployment_synthesizer`: their Lambda code assets live in the pipeline
store under ``assets/<sha256>.zip`` and their templates carry no CDK bootstrap-version rule, so the
pipeline's CloudFormation actions deploy them without a ``CDKToolkit`` stack. The account-level
stacks keep their own synthesizers (store: bootstrapless; tooling: CLI credentials staging in the
store), see :mod:`infra.stacks.tooling`.

Usage (offline): ``uv run python scripts/synth.py --out cdk.out``, or through the CDK CLI:
``npx aws-cdk@2 synth --app "uv run python scripts/synth.py"`` (the CLI passes ``CDK_OUTDIR``).
That local form packages the source tree (fine for tests, NOT deployable). ``--release`` reproduces
the build stage: it builds the arm64 Lambda bundles (:mod:`scripts.lambda_bundle`, into
``--bundles``, default ``.build/lambda-bundles``) and synthesizes in release mode, where a
missing bundle fails the synth.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aws_cdk as cdk  # noqa: E402

from infra.app import build_app  # noqa: E402
from infra.stacks.tooling import deployment_synthesizer  # noqa: E402

__all__ = ["synth"]


def synth(outdir: str | os.PathLike[str] | None = None, envs: list[str] | None = None) -> Path:
    """Synthesize the full app (all environments, tooling and pipeline); returns the assembly directory."""
    out = Path(outdir or os.environ.get("CDK_OUTDIR") or ROOT / "cdk.out")
    app = cdk.App(default_stack_synthesizer=deployment_synthesizer(), outdir=str(out))
    build_app(app, envs)
    asm = app.synth()
    return Path(asm.directory)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthesize the platform cloud assembly for the pipeline (offline).")
    ap.add_argument("--out", help="assembly directory (default: $CDK_OUTDIR or ./cdk.out)")
    ap.add_argument("--release", action="store_true", help="build the Lambda bundles and synthesize in release mode (as the build stage does)")
    ap.add_argument("--bundles", type=Path, default=ROOT / ".build" / "lambda-bundles", help="bundle directory for --release")
    args = ap.parse_args(argv)
    if not args.release:
        print(synth(args.out))
        return 0
    from scripts.build_stage import release_environment
    from scripts.lambda_bundle import build_all

    for name, m in build_all(ROOT, args.bundles).items():
        print(f"lambda bundle {name}: {m['unzipped_bytes'] / 2**20:.1f} MiB unzipped, {m['files']} files", file=sys.stderr)
    with release_environment(args.bundles):
        print(synth(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
