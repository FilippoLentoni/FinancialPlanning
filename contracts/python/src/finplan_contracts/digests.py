"""Reproducible contract build outputs and their SHA-256 digests (``finplan-conformance digest``; CS-04 unit).

One version number (``contracts/VERSION``) produces:

* ``finplan_contracts-<v>-py3-none-any.whl`` and ``finplan_contracts-<v>.tar.gz``
  (Python, built with ``uv build``; hatchling writes sorted entries with fixed
  timestamps taken from ``SOURCE_DATE_EPOCH``);
* ``finplan-contracts-<v>.tgz`` (npm, ``npm pack`` of ``contracts/typescript``; npm
  writes sorted entries with a fixed mtime). Skipped with a recorded reason when
  ``contracts/typescript`` or ``npm`` is absent;
* ``finplan-contracts-schemas-<v>.tar.gz``, the raw schema/fixture tarball built here:
  entries under ``finplan-contracts-<v>/`` in sorted order, every mtime set to
  ``SOURCE_DATE_EPOCH``, uid/gid 0, empty owner names, modes normalized to
  0644/0755, and a gzip header with that mtime and no file name.

``SOURCE_DATE_EPOCH`` comes from the environment when set (CI sets it to the commit
time), otherwise :data:`DEFAULT_SOURCE_DATE_EPOCH`. The same tree built twice gives
byte-identical artifacts, so the digests in the release metadata are stable.

The build writes ``release-metadata.json`` next to the artifacts: package, version,
``source_date_epoch``, optional ``source_commit``, and per artifact its kind, file
name, size and ``sha256``; skipped artifacts are listed with the reason. Consumers pin
the version plus the digest; ``digest verify`` fails a build whose downloaded
artifact differs from the pinned digest ("Digest mismatch").
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .schemas import contracts_root

#: 1980-01-01T00:00:00Z: the earliest timestamp a zip (wheel) entry can carry.
DEFAULT_SOURCE_DATE_EPOCH = 315532800
#: Contract data shipped in the raw tarball (and bundled in the wheel and npm package).
RAW_PARTS = ("VERSION", "README.md", "core", "finance", "domains", "fixtures", "conformance", "ownership", "templates")
RAW_EXCLUDE_NAMES = frozenset({"__pycache__", ".DS_Store", "node_modules"})
CHUNK = 1 << 20


@dataclass
class Artifact:
    kind: str
    file: str
    sha256: str
    size: int


@dataclass
class ReleaseMetadata:
    package: str
    version: str
    source_date_epoch: int
    artifacts: list[Artifact] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    source_commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "package": self.package,
            "version": self.version,
            "source_date_epoch": self.source_date_epoch,
            "artifacts": [asdict(a) for a in sorted(self.artifacts, key=lambda a: (a.kind, a.file))],
            "skipped": sorted(self.skipped, key=lambda s: s["kind"]),
        }
        if self.source_commit:
            d["source_commit"] = self.source_commit
        return d


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def source_date_epoch(value: int | str | None = None) -> int:
    if value is not None:
        return int(value)
    env = os.environ.get("SOURCE_DATE_EPOCH")
    return int(env) if env and env.strip().isdigit() else DEFAULT_SOURCE_DATE_EPOCH


# -------------------------------------------------------------- raw tarball
def _raw_entries(root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for part in RAW_PARTS:
        p = root / part
        if p.is_file():
            entries.append((part, p))
        elif p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in RAW_EXCLUDE_NAMES and not d.startswith("."))
                d = Path(dirpath)
                entries.append((d.relative_to(root).as_posix(), d))
                for name in sorted(filenames):
                    if name in RAW_EXCLUDE_NAMES or name.startswith("."):
                        continue
                    f = d / name
                    entries.append((f.relative_to(root).as_posix(), f))
    return sorted(entries, key=lambda e: e[0])


def build_raw_tarball(out_dir: str | Path, root: str | Path | None = None, epoch: int | None = None) -> Path:
    """Build the reproducible raw schema/fixture tarball; returns its path."""
    root = contracts_root(root)
    epoch = source_date_epoch(epoch)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    prefix = f"finplan-contracts-{version}"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"finplan-contracts-schemas-{version}.tar.gz"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        top = tarfile.TarInfo(prefix)
        top.type, top.mode = tarfile.DIRTYPE, 0o755
        _normalize(top, epoch)
        tf.addfile(top)
        for arcname, path in _raw_entries(root):
            info = tarfile.TarInfo(f"{prefix}/{arcname}")
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                _normalize(info, epoch)
                tf.addfile(info)
            else:
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o755 if os.access(path, os.X_OK) and path.suffix in ("", ".sh", ".py") and data.startswith(b"#!") else 0o644
                _normalize(info, epoch)
                tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    with open(target, "wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=epoch, compresslevel=9) as gz:
            gz.write(raw)
    return target


def _normalize(info: tarfile.TarInfo, epoch: int) -> None:
    info.mtime = epoch
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.pax_headers = {}


# -------------------------------------------------------- python and npm
def build_python(out_dir: str | Path, root: str | Path | None = None, epoch: int | None = None, offline: bool = False, sdist: bool = True) -> list[Path]:
    root = contracts_root(root)
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv not found on PATH")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(source_date_epoch(epoch)), PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory(prefix="finplan-pybuild-") as tmp:
        cmd = [uv, "build", "--wheel", "--out-dir", tmp]
        if sdist:
            cmd.insert(3, "--sdist")
        if offline:
            cmd.append("--offline")
        subprocess.run(cmd, cwd=root / "python", env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        produced = []
        for p in sorted(Path(tmp).iterdir()):
            if p.suffix in (".whl", ".gz"):
                dest = out / p.name
                shutil.copyfile(p, dest)
                produced.append(dest)
    return produced


def build_npm(out_dir: str | Path, root: str | Path | None = None, offline: bool = False) -> Path:
    root = contracts_root(root)
    pkg = root / "typescript"
    if not (pkg / "package.json").is_file():
        raise FileNotFoundError(f"{pkg} has no package.json")
    npm = shutil.which("npm")
    if npm is None:
        raise FileNotFoundError("npm not found on PATH")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [npm, "pack", "--pack-destination", str(out.resolve()), "--json"]
    if offline:
        cmd.append("--offline")
    res = subprocess.run(cmd, cwd=pkg, check=True, capture_output=True, text=True)
    try:
        name = json.loads(res.stdout[res.stdout.index("[") :])[0]["filename"]
    except (ValueError, KeyError, IndexError):
        name = sorted(out.glob("*.tgz"))[-1].name
    return out / name.replace("@", "").replace("/", "-")


# -------------------------------------------------------------------- build
def build_all(
    out_dir: str | Path,
    root: str | Path | None = None,
    epoch: int | None = None,
    python: bool = True,
    npm: bool = True,
    offline: bool = False,
    source_commit: str | None = None,
) -> ReleaseMetadata:
    root = contracts_root(root)
    epoch = source_date_epoch(epoch)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = ReleaseMetadata(package="finplan-contracts", version=version, source_date_epoch=epoch, source_commit=source_commit or os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION"))

    raw = build_raw_tarball(out, root, epoch)
    meta.artifacts.append(Artifact("schema-tarball", raw.name, sha256_file(raw), raw.stat().st_size))
    if python:
        try:
            for p in build_python(out, root, epoch, offline=offline):
                kind = "python-wheel" if p.suffix == ".whl" else "python-sdist"
                meta.artifacts.append(Artifact(kind, p.name, sha256_file(p), p.stat().st_size))
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            meta.skipped.append({"kind": "python", "reason": _reason(exc)})
    else:
        meta.skipped.append({"kind": "python", "reason": "disabled (--no-python)"})
    if npm:
        if not (root / "typescript" / "package.json").is_file():
            meta.skipped.append({"kind": "npm-tarball", "reason": "contracts/typescript does not exist"})
        else:
            try:
                t = build_npm(out, root, offline=offline)
                meta.artifacts.append(Artifact("npm-tarball", t.name, sha256_file(t), t.stat().st_size))
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                meta.skipped.append({"kind": "npm-tarball", "reason": _reason(exc)})
    else:
        meta.skipped.append({"kind": "npm-tarball", "reason": "disabled (--no-npm)"})
    (out / "release-metadata.json").write_text(json.dumps(meta.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def _reason(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        tail = (exc.stderr or b"")
        tail = tail.decode(errors="replace") if isinstance(tail, bytes) else str(tail)
        return f"build command failed (exit {exc.returncode}): {tail.strip().splitlines()[-1] if tail.strip() else ''}"
    return str(exc)


def verify(path: str | Path, expected: str) -> bool:
    exp = expected.lower().removeprefix("sha256:")
    return sha256_file(path) == exp


# ----------------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="finplan-conformance digest", description="Reproducible contract build outputs and SHA-256 digests (CS-04).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build all artifacts and write release-metadata.json")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--root", help="contracts root (default: discovered)")
    b.add_argument("--source-date-epoch", type=int, help="override SOURCE_DATE_EPOCH")
    b.add_argument("--source-commit", help="source commit recorded in the metadata")
    b.add_argument("--no-python", action="store_true")
    b.add_argument("--no-npm", action="store_true")
    b.add_argument("--offline", action="store_true", help="pass --offline to uv and npm")
    t = sub.add_parser("tarball", help="build only the raw schema/fixture tarball")
    t.add_argument("--out", type=Path, required=True)
    t.add_argument("--root")
    t.add_argument("--source-date-epoch", type=int)
    f = sub.add_parser("file", help="print the SHA-256 of files")
    f.add_argument("files", nargs="+", type=Path)
    v = sub.add_parser("verify", help="fail when a file's SHA-256 differs from the pinned digest")
    v.add_argument("file", type=Path)
    v.add_argument("--sha256", required=True, help="pinned digest (hex, optionally 'sha256:'-prefixed)")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        meta = build_all(args.out, args.root, args.source_date_epoch, not args.no_python, not args.no_npm, args.offline, args.source_commit)
        print(json.dumps(meta.to_dict(), indent=2, sort_keys=True))
        return 0 if not any(s["kind"] == "python" for s in meta.skipped if not args.no_python) else 1
    if args.cmd == "tarball":
        p = build_raw_tarball(args.out, args.root, args.source_date_epoch)
        print(f"{sha256_file(p)}  {p}")
        return 0
    if args.cmd == "file":
        for p in args.files:
            print(f"{sha256_file(p)}  {p}")
        return 0
    if args.cmd == "verify":
        ok = verify(args.file, args.sha256)
        print(f"{'PASS' if ok else 'FAIL'}: {args.file} sha256 {'matches' if ok else 'differs from'} the pinned digest")
        return 0 if ok else 1
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
