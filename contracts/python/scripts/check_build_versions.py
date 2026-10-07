#!/usr/bin/env python3
"""Build the Python wheel and the npm tarball and check they carry contracts/VERSION (task 1.1).

Usage (needs uv and npm on PATH; uses the local caches, no AWS):
    uv run python scripts/check_build_versions.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="output directory (default: a temporary directory)")
    args = ap.parse_args(argv)
    version = (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()
    out = args.out or Path(tempfile.mkdtemp(prefix="finplan-build-"))
    out.mkdir(parents=True, exist_ok=True)

    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=CONTRACTS / "python", check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wheels = sorted(out.glob("finplan_contracts-*.whl"))
    if not wheels:
        print("no wheel built", file=sys.stderr)
        return 1
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as zf:
        meta = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        wheel_version = next(line.split(":", 1)[1].strip() for line in zf.read(meta).decode().splitlines() if line.startswith("Version:"))
        bundled = zf.read("finplan_contracts/data/VERSION").decode().strip()
        built = zf.read("finplan_contracts/_version.py").decode()

    subprocess.run(["npm", "pack", "--pack-destination", str(out)], cwd=CONTRACTS / "typescript", check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tgz = sorted(out.glob("finplan-contracts-*.tgz"))[-1]
    with tarfile.open(tgz) as tf:
        pkg = json.load(tf.extractfile("package/package.json"))  # type: ignore[arg-type]
        npm_bundled = tf.extractfile("package/data/VERSION").read().decode().strip()  # type: ignore[union-attr]

    result = {
        "VERSION": version,
        "wheel": wheel.name,
        "wheel_version": wheel_version,
        "wheel_bundled_VERSION": bundled,
        "wheel__version_py": built.strip(),
        "npm_tarball": tgz.name,
        "npm_version": pkg["version"],
        "npm_bundled_VERSION": npm_bundled,
    }
    print(json.dumps(result, indent=2))
    ok = wheel_version == version == bundled == pkg["version"] == npm_bundled and f'"{version}"' in built
    print("OK: Python and npm artifacts carry the same version" if ok else "MISMATCH", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
