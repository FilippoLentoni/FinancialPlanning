#!/usr/bin/env python3
"""Ingestion function packaging decision (task 6.17; design P5/P6a "Runtime").

Walks the installed dependency closure of ``finplan-platform[providers]`` (the pinned
``yfinance`` and its dependencies, plus the platform's runtime dependencies), sums the
unzipped size of every distribution's files and compares it with the Lambda zip-package
limit (250 MB unzipped, function code plus layers). Distributions the Lambda Python runtime
already provides (``boto3``, ``botocore``, ``s3transfer``, ``jmespath``, ``urllib3``,
``python-dateutil``, ``six``) are excluded, as the build bundle excludes them.

This is the early (pre-synth) estimate. The authoritative size is measured on the real arm64
bundle by ``scripts/lambda_bundle.py`` and the ``lambda-bundle`` post gate; that bundle also
carries the pinned ``boto3``/``botocore`` (about 25 MB more than this estimate).

Prints a JSON report with ``mode`` ``zip`` or ``image``. The build stage builds a zip bundle
(``scripts/lambda_bundle.py``) when ``mode`` is ``zip``, otherwise a container image from
``infra/docker/ingestion/Dockerfile`` (``FINPLAN_INGESTION_IMAGE_DIR``). The size is measured
on the build host's wheels; the arm64 Lambda wheels differ slightly, so a margin is kept
(``--margin``, default 10%). ``--require-zip`` exits 1 when the zip form does not fit.
"""

from __future__ import annotations

import argparse
import json
import re
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

LAMBDA_UNZIPPED_LIMIT = 250 * 1024 * 1024
RUNTIME_PROVIDED = {"boto3", "botocore", "s3transfer", "jmespath", "urllib3", "python-dateutil", "six"}
ROOT_REQUIREMENT = "finplan-platform"
ROOT_EXTRAS = ("providers",)
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(dist_name: str, extras: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
    try:
        dist = distribution(dist_name)
    except PackageNotFoundError:
        return []
    out = []
    for req in dist.requires or []:
        spec, _, marker = req.partition(";")
        marker = marker.strip()
        if marker:
            m = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", marker)
            if m and m.group(1) not in extras:
                continue
            if not m:
                try:
                    from packaging.markers import Marker

                    if not Marker(marker).evaluate({"extra": ""}):
                        continue
                except Exception:  # noqa: BLE001 - unknown marker: include conservatively
                    pass
        nm = _NAME.match(spec)
        if nm:
            out.append((nm.group(1), tuple(x.strip() for x in (nm.group(3) or "").split(",") if x.strip())))
    return out


def closure() -> dict[str, str]:
    seen: dict[str, str] = {}
    stack: list[tuple[str, tuple[str, ...]]] = [(ROOT_REQUIREMENT, ROOT_EXTRAS)]
    while stack:
        name, extras = stack.pop()
        key = _norm(name)
        if key in seen and not extras:
            continue
        try:
            seen[key] = distribution(name).metadata["Name"]
        except PackageNotFoundError:
            continue
        stack.extend(_requirements(name, extras))
    return seen


def dist_size(name: str) -> int:
    dist = distribution(name)
    total = 0
    for f in dist.files or []:
        try:
            total += f.locate().stat().st_size
        except OSError:
            continue
    return total


def report(margin: float = 0.10) -> dict[str, Any]:
    names = closure()
    sizes = {n: dist_size(n) for k, n in sorted(names.items()) if k not in RUNTIME_PROVIDED and k != ROOT_REQUIREMENT}
    total = sum(sizes.values())
    budget = int(LAMBDA_UNZIPPED_LIMIT * (1 - margin))
    return {
        "total_unzipped_bytes": total,
        "limit_unzipped_bytes": LAMBDA_UNZIPPED_LIMIT,
        "budget_with_margin_bytes": budget,
        "mode": "zip" if total <= budget else "image",
        "largest": sorted(({"distribution": n, "bytes": b} for n, b in sizes.items()), key=lambda x: -x["bytes"])[:8],
        "distributions": len(sizes),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--margin", type=float, default=0.10)
    ap.add_argument("--require-zip", action="store_true")
    args = ap.parse_args(argv)
    rep = report(args.margin)
    print(json.dumps(rep, indent=1))
    return 1 if args.require_zip and rep["mode"] != "zip" else 0


if __name__ == "__main__":
    raise SystemExit(main())
