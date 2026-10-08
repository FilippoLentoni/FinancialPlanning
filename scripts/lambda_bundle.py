#!/usr/bin/env python3
"""Lambda code bundles of the platform functions (tasks 6.17, 10.1; docs/pipeline.md
"Source-only Lambda bundle").

A bundle is the directory a zip Lambda runs from (``/var/task``). Per function it contains:

* the runtime dependency closure exported from ``uv.lock`` (``uv export --frozen --no-dev
  --no-emit-project`` with the function's extras), installed with ``--require-hashes`` and
  ``--only-binary :all:`` for **CPython 3.12 on arm64** (``aarch64-manylinux_2_28``: the Lambda
  ``python3.12`` runtime is Amazon Linux 2023, glibc 2.34; ``manylinux2014`` is not enough since
  numpy 2.5 ships only ``manylinux_2_28`` aarch64 wheels). That closure includes the pinned,
  vendored ``finplan-contracts`` wheel (its SHA-256 is the ``contracts-pin.json`` digest and is
  re-checked here), ``jsonschema``, ``rfc8785``, ``openpyxl``, ``defusedxml`` and the pinned
  ``boto3``/``botocore`` (bundled rather than taken from the runtime, so the code runs on the
  locked versions);
* the platform package ``finplan_platform`` (with its shipped calendar data, the build-time
  ``exchange_calendars`` XNYS calendar among them);
* ``config/`` (``FINPLAN_CONFIG_DIR=/var/task/config``);
* ``bundle-manifest.json``: function, handler, extras, target platform, file count, unzipped size.

Functions and extras (:data:`FUNCTIONS`): ``ingestion`` and ``plan-api`` get the ``providers``
extra (``yfinance``, ``exchange-calendars``); the plan API needs it too because it runs
``POST /v1/ingestions`` in process. ``sweeper`` gets the base dependencies only.

Before the 2026-10-07 fix the deployed functions were the bare ``platform/`` source tree
(~150 KB) and failed at init with ``No module named 'finplan_contracts'``. The build stage now
builds the bundles before ``cdk synth`` and synthesizes in release mode
(:func:`infra.stacks.common.lambda_code` refuses source-only code there).

Usage::

    uv run python scripts/lambda_bundle.py --out .build/lambda-bundles            # arm64 release bundles
    uv run python scripts/lambda_bundle.py --out /tmp/b --local --import-check    # host platform + import check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

__all__ = [
    "FUNCTIONS",
    "LAMBDA_PLATFORM",
    "LAMBDA_UNZIPPED_LIMIT",
    "MANIFEST",
    "REQUIRED_MODULES",
    "BundleError",
    "BundleSpec",
    "build_all",
    "build_bundle",
    "bundle_size",
    "foreign_binaries",
    "import_check",
    "verify_bundle",
]

#: uv ``--python-platform`` of the Lambda runtime (python3.12, arm64, Amazon Linux 2023).
LAMBDA_PLATFORM = "aarch64-manylinux_2_28"
PYTHON_VERSION = "3.12"
#: Lambda limit for the unzipped deployment package (function code plus layers).
LAMBDA_UNZIPPED_LIMIT = 250 * 1024 * 1024
MANIFEST = "bundle-manifest.json"
#: Top-level entries every bundle must carry (the init failure was a missing ``finplan_contracts``).
REQUIRED_MODULES = ("finplan_platform", "finplan_contracts", "jsonschema", "rfc8785", "referencing", "openpyxl", "defusedxml", "boto3", "config")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleSpec:
    handler: str
    extras: tuple[str, ...] = ()
    #: extra top-level entries this function's bundle must carry
    requires: tuple[str, ...] = ()

    @property
    def module(self) -> str:
        return self.handler.rsplit(".", 1)[0]


FUNCTIONS: dict[str, BundleSpec] = {
    "plan-api": BundleSpec("finplan_platform.handlers.api.handler", ("providers",), ("yfinance", "exchange_calendars")),
    "ingestion": BundleSpec("finplan_platform.handlers.ingest.handler", ("providers",), ("yfinance", "exchange_calendars", "pandas", "numpy")),
    "sweeper": BundleSpec("finplan_platform.handlers.sweep.handler"),
}


# ===================================================================== building
def _uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise BundleError("uv is required to build Lambda bundles (it reads the pinned closure from uv.lock)")
    return uv


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise BundleError(f"{' '.join(cmd[:4])} ... failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-2000:]}")
    return proc.stdout


def export_requirements(root: Path, extras: Iterable[str], dest: Path) -> Path:
    """The locked runtime closure (exact pins and hashes) as a requirements file."""
    cmd = [_uv(), "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--quiet", "-o", str(dest)]
    for extra in extras:
        cmd += ["--extra", extra]
    _run(cmd, root)
    return dest


def _check_contracts_wheel(root: Path) -> None:
    pin = json.loads((root / "contracts-pin.json").read_text(encoding="utf-8"))
    wheel = root / pin["artifact"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        raise BundleError(f"vendored contract wheel {wheel.name} digest {digest} != contracts-pin.json {pin['sha256']}")


def bundle_size(path: Path) -> tuple[int, int]:
    """(unzipped bytes, file count) of a bundle directory."""
    total = files = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
            files += 1
    return total, files


def build_bundle(
    root: Path,
    dest: Path,
    *,
    function: str,
    spec: BundleSpec | None = None,
    python_platform: str | None = LAMBDA_PLATFORM,
    python: str | None = None,
    limit: int = LAMBDA_UNZIPPED_LIMIT,
) -> dict[str, Any]:
    """Build one function bundle into ``dest`` (must not exist). ``python_platform=None`` builds
    for the interpreter ``python`` (default: this one) instead of the Lambda target."""
    spec = spec or FUNCTIONS[function]
    if dest.exists():
        raise BundleError(f"{dest} already exists")
    _check_contracts_wheel(root)
    dest.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        req = export_requirements(root, spec.extras, Path(tmp) / "requirements.txt")
        cmd = [_uv(), "pip", "install", "--quiet", "--target", str(dest), "--no-deps", "--require-hashes", "--only-binary", ":all:", "--no-config", "-r", str(req)]
        if python_platform:
            cmd += ["--python-platform", python_platform, "--python-version", PYTHON_VERSION]
        cmd += ["--python", python or sys.executable]
        _run(cmd, root)  # cwd=root: the vendored contract wheel is a path relative to the project
    shutil.rmtree(dest / "bin", ignore_errors=True)  # console scripts: not used by Lambda
    # installer records of the path-installed contract wheel: the build host's absolute path and an
    # install timestamp would make the asset hash differ per build (and leak the build path)
    for record in (*dest.glob("*.dist-info/direct_url.json"), *dest.glob("*.dist-info/uv_cache.json")):
        record.unlink()
    (dest / ".lock").unlink(missing_ok=True)  # uv's target-directory lock file
    for cache in list(dest.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    shutil.copytree(root / "platform" / "finplan_platform", dest / "finplan_platform", ignore=_IGNORE)
    shutil.copytree(root / "config", dest / "config", ignore=_IGNORE)
    size, files = bundle_size(dest)
    manifest = {
        "function": function,
        "handler": spec.handler,
        "extras": list(spec.extras),
        "python_version": PYTHON_VERSION,
        "python_platform": python_platform or "host",
        "files": files,
        "unzipped_bytes": size,
        "limit_unzipped_bytes": limit,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    problems = verify_bundle(dest, spec, limit=limit)
    if problems:
        raise BundleError(f"bundle {function}: " + "; ".join(problems))
    return manifest


def build_all(root: Path, out: Path, *, functions: Mapping[str, BundleSpec] = FUNCTIONS, python_platform: str | None = LAMBDA_PLATFORM, python: str | None = None) -> dict[str, dict[str, Any]]:
    """One bundle per function under ``out/<function>``. Functions with the same extras get
    byte-identical bundles (installed once, then copied; the manifest lists every function of the
    group), so the cloud assembly holds one code asset per distinct bundle."""
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, ...], list[str]] = {}
    for name, spec in functions.items():
        groups.setdefault(spec.extras, []).append(name)
    result: dict[str, dict[str, Any]] = {}
    for extras, names in groups.items():
        first = out / names[0]
        for name in names:
            shutil.rmtree(out / name, ignore_errors=True)
        manifest = build_bundle(root, first, function=names[0], spec=functions[names[0]], python_platform=python_platform, python=python)
        manifest.pop("function")
        manifest.pop("handler")
        manifest["functions"] = {n: functions[n].handler for n in names}
        (first / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        for name in names:
            if name != names[0]:
                shutil.copytree(first, out / name, symlinks=True)
            problems = verify_bundle(out / name, functions[name])
            if problems:
                raise BundleError(f"bundle {name}: " + "; ".join(problems))
            result[name] = manifest
    return result


# ===================================================================== checks
def verify_bundle(path: Path, spec: BundleSpec | None = None, *, limit: int = LAMBDA_UNZIPPED_LIMIT) -> list[str]:
    """Structural problems of a bundle directory (empty list: deployable)."""
    problems: list[str] = []
    if not (path / MANIFEST).is_file():
        problems.append(f"{MANIFEST} missing (not a build-stage bundle; a source-only package is not deployable)")
    for name in REQUIRED_MODULES + (spec.requires if spec else ()):
        if not ((path / name).is_dir() or (path / f"{name}.py").is_file()):
            problems.append(f"{name} missing")
    if spec is not None:
        handler_file = path / (spec.module.replace(".", "/") + ".py")
        if not handler_file.is_file():
            problems.append(f"handler module {spec.module} missing")
    calendars = path / "finplan_platform" / "data" / "calendars"
    if not any(calendars.glob("xnys-exchange_calendars-*.json")):
        problems.append("the build-time XNYS calendar (finplan_platform/data/calendars) is missing")
    if not (path / "config" / "shared.json").is_file():
        problems.append("config/shared.json missing")
    size, _ = bundle_size(path)
    if size > limit:
        problems.append(f"{size // (1024 * 1024)} MiB unzipped exceeds the Lambda limit of {limit // (1024 * 1024)} MiB")
    return problems


#: ELF ``e_machine`` of arm64 (the Lambda functions' architecture).
ELF_AARCH64 = 0xB7


def foreign_binaries(path: Path, machine: int = ELF_AARCH64) -> list[str]:
    """Shared objects in the bundle that are not built for ``machine`` (default arm64)."""
    bad: list[str] = []
    for p in sorted(path.rglob("*.so*")):
        if not p.is_file() or p.is_symlink():
            continue
        with p.open("rb") as fh:
            head = fh.read(20)
        if head[:4] != b"\x7fELF":
            continue
        order = "little" if head[5] == 1 else "big"
        if int.from_bytes(head[18:20], order) != machine:
            bad.append(p.relative_to(path).as_posix())
    return bad


def import_check(path: Path, spec: BundleSpec, *, python: str | None = None, env: Mapping[str, str] | None = None) -> None:
    """Import the handler module in a fresh interpreter whose only non-stdlib path is the bundle.

    ``-I -S -B``: no bytecode written into the bundle (``/var/task`` is read-only too), no user
    site, no site-packages, no ``PYTHON*`` variables, so nothing from the build
    host's environment can satisfy an import the bundle lacks. Only meaningful for a bundle built
    for the running platform (``python_platform=None``).
    """
    code = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {str(path)!r})\n"
        f"mod = importlib.import_module({spec.module!r})\n"
        f"assert callable(getattr(mod, {spec.handler.rsplit('.', 1)[1]!r}))\n"
        "import finplan_contracts, jsonschema, rfc8785\n"
        f"for extra in {list(spec.requires)!r}: importlib.import_module(extra)\n"
        f"assert all(m.__file__.startswith({str(path)!r}) for m in (mod, finplan_contracts, jsonschema, rfc8785))\n"
        "print('ok', mod.__name__)\n"
    )
    run_env = {"PATH": os.environ.get("PATH", ""), "FINPLAN_CONFIG_DIR": str(path / "config"), **(env or {})}
    proc = subprocess.run([python or sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, env=run_env, cwd=path, check=False)
    if proc.returncode != 0:
        raise BundleError(f"{spec.module} does not import from the bundle alone: {(proc.stdout + proc.stderr)[-2000:]}")


# ===================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the platform Lambda bundles from uv.lock.")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--local", action="store_true", help="build for this interpreter's platform instead of Lambda arm64")
    ap.add_argument("--import-check", action="store_true", help="import every handler with only the bundle on sys.path (needs --local)")
    ap.add_argument("--function", action="append", choices=sorted(FUNCTIONS), help="only these functions")
    args = ap.parse_args(argv)
    if args.import_check and not args.local:
        ap.error("--import-check needs --local (arm64 extension modules do not load on another platform)")
    functions = {k: v for k, v in FUNCTIONS.items() if not args.function or k in args.function}
    try:
        result = build_all(args.root, args.out, functions=functions, python_platform=None if args.local else LAMBDA_PLATFORM)
        if args.import_check:
            for name, spec in functions.items():
                import_check(args.out / name, spec)
    except BundleError as exc:
        print(f"BUNDLE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: asdict(FUNCTIONS[k]) | {"unzipped_mib": round(v["unzipped_bytes"] / 2**20, 1), "files": v["files"]} for k, v in result.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
