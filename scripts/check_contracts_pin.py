#!/usr/bin/env python3
"""Verify the finplan-contracts pin (task 1.2; contracts CS-04 consumer case).

Fails the build ("Digest mismatch") unless ALL of these agree:

1. ``contracts-pin.json``: package, exact version and SHA-256 of the pinned wheel;
2. the vendored wheel file on disk hashes to that SHA-256 (verified with the contract
   package's own ``finplan_contracts.digests.verify``);
3. ``pyproject.toml`` depends on ``finplan-contracts==<version>`` (an exact pin, no range);
4. ``uv.lock`` locks that version with the same wheel hash (uv refuses to install a wheel
   whose hash differs);
5. the installed distribution has that version (when it is installed).

``--rebuild`` additionally rebuilds the wheel from ``contracts/python`` with the pinned
``SOURCE_DATE_EPOCH`` and requires byte identity, proving the vendored artifact is the
reproducible build of the in-repo source. ``--env gamma|prod`` fails for a 0.x pin
(0.x is beta-only; contracts ``docs/consumer-pinning.md``).

``--repin`` is the deliberate re-pin after a contract change: it rebuilds the wheel
reproducibly, replaces the vendored artifact, rewrites ``contracts-pin.json`` and runs
``uv lock`` so the lock records the new digest. It is a human/agent action, never a
build step.

Exit codes: 0 ok, 1 mismatch, 2 usage/config error. Runs offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(root: Path = ROOT, *, rebuild: bool = False, env: str | None = None, pin_path: Path | None = None) -> list[str]:
    problems: list[str] = []
    pin_file = pin_path or root / "contracts-pin.json"
    pin = json.loads(pin_file.read_text(encoding="utf-8"))
    package, version, digest = pin["package"], pin["version"], pin["sha256"].lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return [f"{pin_file.name}: sha256 must be 64 lowercase hex characters"]
    wheel = (root / pin["artifact"]).resolve()
    if not wheel.is_file():
        return [f"pinned artifact {pin['artifact']} is missing"]

    # 2. digest of the artifact (the contract package's own verifier when importable)
    try:
        from finplan_contracts.digests import verify

        ok = verify(wheel, digest)
    except ImportError:  # pragma: no cover - bootstrap before the package is installed
        ok = _sha256(wheel) == digest
    if not ok:
        problems.append(f"Digest mismatch: {wheel.name} sha256 {_sha256(wheel)} != pinned {digest}")
    if f"-{version}-" not in wheel.name:
        problems.append(f"artifact {wheel.name} does not carry the pinned version {version}")

    # 3. exact pin in pyproject
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject.get("project", {}).get("dependencies", [])
    spec = next((d for d in deps if re.match(rf"^{re.escape(package)}\s*[=<>!~]", d)), None)
    if spec is None:
        problems.append(f"pyproject.toml does not depend on {package}")
    elif re.sub(r"\s", "", spec) != f"{package}=={version}":
        problems.append(f"pyproject.toml must pin {package}=={version} exactly (found {spec!r}); ranges are not allowed")
    src = pyproject.get("tool", {}).get("uv", {}).get("sources", {}).get(package, {})
    if src.get("path") and Path(src["path"]).as_posix() != Path(pin["artifact"]).as_posix():
        problems.append(f"[tool.uv.sources] {package} points at {src['path']}, not the pinned artifact")

    # 4. uv.lock
    lock_path = root / "uv.lock"
    if lock_path.is_file():
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        entry = next((p for p in lock.get("package", []) if p.get("name") == package), None)
        if entry is None:
            problems.append(f"uv.lock has no {package} entry")
        else:
            if entry.get("version") != version:
                problems.append(f"uv.lock locks {package} {entry.get('version')}, pin says {version}")
            hashes = {w.get("hash", "").removeprefix("sha256:") for w in entry.get("wheels", [])}
            if digest not in hashes:
                problems.append(f"Digest mismatch: uv.lock wheel hash {sorted(hashes)} does not include the pinned digest")
    else:
        problems.append("uv.lock is missing")

    # 5. installed distribution
    try:
        from importlib.metadata import PackageNotFoundError, version as dist_version

        try:
            installed = dist_version(package)
            if installed != version:
                problems.append(f"installed {package} {installed} != pinned {version}")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover
        pass

    # optional: reproducible rebuild from source
    if rebuild:
        with tempfile.TemporaryDirectory() as tmp:
            envv = {**os.environ, "SOURCE_DATE_EPOCH": str(pin.get("source_date_epoch", 315532800))}
            subprocess.run(["uv", "build", "--wheel", "--out-dir", tmp, "-q"], cwd=root / "contracts" / "python", env=envv, check=True)
            built = next(Path(tmp).glob("*.whl"))
            if _sha256(built) != digest:
                problems.append(f"Digest mismatch: rebuilding contracts/python gives {_sha256(built)}; re-pin after a contract change")

    # 0.x is beta-only
    if env in ("gamma", "prod") and version.startswith("0."):
        problems.append(f"contract version {version} is a 0.x pre-release and may be deployed to beta only; {env} requires 1.0.0 or later")
    return problems


def repin(root: Path = ROOT) -> str:
    """Rebuild, vendor and re-pin the contract wheel; returns the new digest."""
    pin_file = root / "contracts-pin.json"
    pin = json.loads(pin_file.read_text(encoding="utf-8"))
    version = (root / "contracts" / "VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory() as tmp:
        envv = {**os.environ, "SOURCE_DATE_EPOCH": str(pin.get("source_date_epoch", 315532800))}
        subprocess.run(["uv", "build", "--wheel", "--out-dir", tmp, "-q"], cwd=root / "contracts" / "python", env=envv, check=True)
        built = next(Path(tmp).glob("*.whl"))
        dest_dir = root / "vendor" / "finplan-contracts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob("*.whl"):
            old.unlink()
        dest = dest_dir / built.name
        dest.write_bytes(built.read_bytes())
    digest = _sha256(dest)
    pin.update(version=version, sha256=digest, artifact=dest.relative_to(root).as_posix())
    pin_file.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    py = root / "pyproject.toml"
    text = re.sub(r'"finplan-contracts==[^"]+"', f'"finplan-contracts=={version}"', py.read_text(encoding="utf-8"))
    text = re.sub(r'(finplan-contracts = \{ path = ")[^"]+(" \})', rf"\g<1>{dest.relative_to(root).as_posix()}\g<2>", text)
    py.write_text(text, encoding="utf-8")
    subprocess.run(["uv", "lock", "--upgrade-package", "finplan-contracts"], cwd=root, check=True)
    return digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--pin", type=Path, help="pin file (default: <root>/contracts-pin.json)")
    ap.add_argument("--rebuild", action="store_true", help="also rebuild the wheel from contracts/python and compare")
    ap.add_argument("--env", choices=["beta", "gamma", "prod"], help="deployment target (0.x pins are beta-only)")
    ap.add_argument("--repin", action="store_true", help="rebuild and re-pin the contract wheel (deliberate action, not a build step)")
    args = ap.parse_args(argv)
    if args.repin:
        print(f"re-pinned finplan-contracts: sha256 {repin(args.root)}; run 'uv sync' next")
        return 0
    try:
        problems = check(args.root, rebuild=args.rebuild, env=args.env, pin_path=args.pin)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print("PASS: finplan-contracts pin verified (version, wheel digest, pyproject, uv.lock)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
