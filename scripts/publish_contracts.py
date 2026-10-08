#!/usr/bin/env python3
"""Publish the contract package to the contract registry (contracts task 7.3; design D3, P10; CS-04).

Runs in the Build stage **after** ``scripts/build_stage.py`` succeeded, so every gate has passed and
the vendored wheel is proven to be the reproducible build of ``contracts/python`` (the
``contracts-pin`` gate rebuilds it with ``--rebuild``). Rollback builds skip it (nothing is rebuilt).

Inputs:

* the registry reference ``/finplan/shared/financialplanning/contract/registry-ref``
  (:func:`finplan_contracts.registry.parse_registry_ref`: domain, repository, region, formats),
  written by the tooling stack; the domain owner is the build's own account;
* the Python wheel: exactly the pinned artifact of ``contracts-pin.json`` (version + SHA-256), whose
  version must equal ``contracts/VERSION``;
* the npm tarball: ``npm pack`` of ``contracts/typescript`` (``@finplan/contracts``, same version).

Per package the step is **publish once, never overwrite**:

1. ``DescribePackageVersion``. Not found: publish (``uv publish`` for the wheel, ``npm publish`` for
   the tarball, each authenticated with a 15-minute CodeArtifact token passed through the
   environment or a temporary npm config, never on the command line or in the log), then verify
   that the registry now holds an asset with the local SHA-256.
2. Found: compare the stored asset with the local artifact. The wheel must have the identical
   SHA-256 (the pinned digest). The npm tarball must have the identical SHA-256 or, when ``npm pack``
   of the same sources differs only in archive metadata, the identical *content digest* (SHA-256 over
   the sorted ``(path, sha256)`` list of its files). Equal: nothing is published ("already published").
   Different, or the version holds other assets: the build **fails** (a published version is
   immutable; publish a new version instead).

The build role may publish to this one repository only and holds no delete, dispose or
status-change permission (:func:`finplan_contracts.registry.publish_policy`). Every AWS client and
the command runner are injected; the unit suite uses fakes and makes no AWS call.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finplan_contracts import registry as contract_registry  # noqa: E402

__all__ = [
    "TOKEN_SECONDS",
    "Outcome",
    "PackageSpec",
    "PublishRefused",
    "content_digest",
    "main",
    "npm_spec",
    "pack_npm",
    "publish_all",
    "publish_package",
    "registry_ref",
    "wheel_spec",
]

TOKEN_SECONDS = 900
NPM_ASSET = "package.tgz"


class PublishRefused(RuntimeError):
    """The publish step must fail the build (an existing version differs, or a precondition fails)."""


@dataclass(frozen=True)
class PackageSpec:
    fmt: str  # pypi | npm
    namespace: str | None
    package: str
    version: str
    path: Path

    @property
    def label(self) -> str:
        name = f"@{self.namespace}/{self.package}" if self.namespace else self.package
        return f"{self.fmt} {name} {self.version}"

    def coordinates(self) -> dict[str, str]:
        out = {"format": self.fmt, "package": self.package, "packageVersion": self.version}
        if self.namespace:
            out["namespace"] = self.namespace
        return out


@dataclass(frozen=True)
class Outcome:
    spec: PackageSpec
    action: str  # published | already-published
    sha256: str


# ===================================================================== digests
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def content_digest(tgz: bytes) -> str:
    """SHA-256 over the sorted ``(path, sha256)`` list of the regular files of an npm tarball."""
    entries: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile():
                fh = tf.extractfile(m)
                entries.append((m.name, _sha256_bytes(fh.read() if fh else b"")))
    return _sha256_bytes(json.dumps(sorted(entries), separators=(",", ":")).encode())


# ===================================================================== inputs
def registry_ref(ssm: Any) -> dict[str, Any]:
    try:
        value = ssm.get_parameter(Name=contract_registry.REGISTRY_REF_PARAMETER)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - client-specific not-found errors
        raise PublishRefused(f"the contract registry reference {contract_registry.REGISTRY_REF_PARAMETER} cannot be read ({type(exc).__name__}); is the tooling stack bootstrapped with the registry?") from None
    try:
        return contract_registry.parse_registry_ref(value)
    except ValueError as exc:
        raise PublishRefused(str(exc)) from None


def _contracts_version(root: Path) -> str:
    return (root / "contracts" / "VERSION").read_text(encoding="utf-8").strip()


def wheel_spec(root: Path) -> PackageSpec:
    """The pinned, vendored wheel (it must be the current contracts/VERSION and match its digest)."""
    pin = json.loads((root / "contracts-pin.json").read_text(encoding="utf-8"))
    version = _contracts_version(root)
    if pin["package"] != contract_registry.PYPI_PACKAGE:
        raise PublishRefused(f"contracts-pin.json pins {pin['package']}, not {contract_registry.PYPI_PACKAGE}")
    if pin["version"] != version:
        raise PublishRefused(f"contracts/VERSION is {version} but the platform pins {pin['version']}: re-pin (scripts/check_contracts_pin.py --repin) before publishing")
    wheel = root / pin["artifact"]
    if _sha256_file(wheel) != pin["sha256"]:
        raise PublishRefused(f"{wheel.name} does not match the pinned digest")
    return PackageSpec("pypi", None, contract_registry.PYPI_PACKAGE, version, wheel)


def pack_npm(root: Path, out_dir: Path, run: Callable[..., Any] = subprocess.run) -> Path:
    """``npm pack`` of contracts/typescript (installs and builds first when needed)."""
    ts = root / "contracts" / "typescript"
    npm = shutil.which("npm")
    if npm is None:
        raise PublishRefused("npm is not on PATH; cannot pack @finplan/contracts")
    if not (ts / "node_modules").is_dir():
        run([npm, "ci", "--no-audit", "--no-fund"], cwd=ts, check=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    run([npm, "pack", "--silent", "--pack-destination", str(out_dir)], cwd=ts, check=True, stdout=subprocess.DEVNULL)
    found = sorted(out_dir.glob("*.tgz"))
    if len(found) != 1:
        raise PublishRefused(f"npm pack produced {len(found)} tarballs")
    return found[0]


def npm_spec(root: Path, tgz: Path) -> PackageSpec:
    version = _contracts_version(root)
    with tarfile.open(tgz, "r:gz") as tf:
        member = tf.extractfile("package/package.json")
        pkg = json.loads(member.read() if member else b"{}")
    expected = f"@{contract_registry.NPM_NAMESPACE}/{contract_registry.NPM_PACKAGE}"
    if pkg.get("name") != expected or pkg.get("version") != version:
        raise PublishRefused(f"{tgz.name} is {pkg.get('name')}@{pkg.get('version')}, expected {expected}@{version}")
    return PackageSpec("npm", contract_registry.NPM_NAMESPACE, contract_registry.NPM_PACKAGE, version, tgz)


# ===================================================================== registry access
def _not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code == "ResourceNotFoundException" or type(exc).__name__ == "ResourceNotFoundException"


def _target(ref: Mapping[str, Any], owner: str) -> dict[str, str]:
    return {"domain": ref["domain"], "domainOwner": owner, "repository": ref["repository"]}


def _existing_assets(ca: Any, target: Mapping[str, str], spec: PackageSpec) -> list[dict[str, Any]] | None:
    """Assets of the version, or None when the version does not exist."""
    try:
        ca.describe_package_version(**target, **spec.coordinates())
    except Exception as exc:
        if _not_found(exc):
            return None
        raise
    assets: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = ca.list_package_version_assets(**target, **spec.coordinates(), **({"nextToken": token} if token else {}))
        assets += page.get("assets") or []
        token = page.get("nextToken")
        if not token:
            return assets


def _asset_sha(asset: Mapping[str, Any]) -> str | None:
    return (asset.get("hashes") or {}).get("SHA-256")


def _same_as_published(ca: Any, target: Mapping[str, str], spec: PackageSpec, assets: Sequence[Mapping[str, Any]]) -> str | None:
    """None when the published version equals the local artifact, else the reason it differs."""
    local = spec.path.read_bytes()
    sha = _sha256_bytes(local)
    if spec.fmt == "pypi":
        names = {a.get("name") for a in assets}
        match = next((a for a in assets if a.get("name") == spec.path.name), None)
        if match is None:
            return f"the published version holds {sorted(map(str, names))}, not {spec.path.name}"
        if _asset_sha(match) != sha:
            return f"the published {spec.path.name} has SHA-256 {_asset_sha(match)}, the pinned wheel {sha}"
        if len(assets) != 1:
            return f"the published version holds extra assets {sorted(map(str, names - {spec.path.name}))}"
        return None
    match = next((a for a in assets if a.get("name") == NPM_ASSET), None)
    if match is None:
        return f"the published version has no {NPM_ASSET}"
    if _asset_sha(match) == sha:
        return None
    resp = ca.get_package_version_asset(**target, **spec.coordinates(), asset=NPM_ASSET)
    remote = resp["asset"].read()
    if content_digest(remote) != content_digest(local):
        return f"the published {NPM_ASSET} has different contents (content digest {content_digest(remote)} != {content_digest(local)})"
    return None


def _token(ca: Any, ref: Mapping[str, Any], owner: str) -> str:
    return str(ca.get_authorization_token(domain=ref["domain"], domainOwner=owner, durationSeconds=TOKEN_SECONDS)["authorizationToken"])


def _endpoint(ca: Any, target: Mapping[str, str], fmt: str) -> str:
    return str(ca.get_repository_endpoint(**target, format=fmt)["repositoryEndpoint"])


def _upload(ca: Any, ref: Mapping[str, Any], owner: str, spec: PackageSpec, run: Callable[..., Any], environ: Mapping[str, str]) -> None:
    target = _target(ref, owner)
    endpoint = _endpoint(ca, target, spec.fmt)
    token = _token(ca, ref, owner)
    if spec.fmt == "pypi":
        env = {**environ, "UV_PUBLISH_URL": endpoint, "UV_PUBLISH_USERNAME": "aws", "UV_PUBLISH_PASSWORD": token}
        proc = run(["uv", "publish", "--no-progress", str(spec.path)], env=env, check=False)
    else:
        with tempfile.TemporaryDirectory(prefix="finplan-npmrc-") as tmp:
            npmrc = Path(tmp) / ".npmrc"
            auth_key = "//" + endpoint.split("://", 1)[-1]
            npmrc.write_text(f"registry={endpoint}\n{auth_key}:_authToken={token}\n", encoding="utf-8")
            npmrc.chmod(0o600)
            env = {**environ, "NPM_CONFIG_USERCONFIG": str(npmrc)}
            proc = run(["npm", "publish", str(spec.path), "--registry", endpoint, "--ignore-scripts"], env=env, check=False)
    if int(getattr(proc, "returncode", 1)) != 0:
        raise PublishRefused(f"publishing {spec.label} failed (exit {getattr(proc, 'returncode', '?')})")


def publish_package(ca: Any, ref: Mapping[str, Any], owner: str, spec: PackageSpec, *, run: Callable[..., Any] = subprocess.run, environ: Mapping[str, str] | None = None, log: Callable[[str], None] = print) -> Outcome:
    if spec.fmt not in ref["formats"]:
        raise PublishRefused(f"the registry reference does not list the {spec.fmt} format")
    target = _target(ref, owner)
    sha = _sha256_file(spec.path)
    assets = _existing_assets(ca, target, spec)
    if assets is not None:
        reason = _same_as_published(ca, target, spec, assets)
        if reason:
            raise PublishRefused(f"{spec.label} is already published and differs; published versions are immutable, publish a new version ({reason})")
        log(f"contract registry: {spec.label} already published (sha256 {sha}); nothing to do")
        return Outcome(spec, "already-published", sha)
    _upload(ca, ref, owner, spec, run, os.environ if environ is None else environ)
    after = _existing_assets(ca, target, spec)
    reason = "not found after publishing" if after is None else _same_as_published(ca, target, spec, after)
    if reason:
        raise PublishRefused(f"{spec.label} published but verification failed: {reason}")
    log(f"contract registry: published {spec.label} (sha256 {sha})")
    return Outcome(spec, "published", sha)


def publish_all(
    root: Path,
    *,
    ca: Any,
    ssm: Any,
    owner: str,
    npm: bool = True,
    run: Callable[..., Any] = subprocess.run,
    pack: Callable[[Path, Path], Path] | None = None,
    environ: Mapping[str, str] | None = None,
    log: Callable[[str], None] = print,
) -> list[Outcome]:
    """Publish the wheel and (``npm``) the npm tarball, each at most once (see module docstring)."""
    ref = registry_ref(ssm)
    outcomes = [publish_package(ca, ref, owner, wheel_spec(root), run=run, environ=environ, log=log)]
    if npm:
        with tempfile.TemporaryDirectory(prefix="finplan-npm-pack-") as tmp:
            tgz = (pack or (lambda r, o: pack_npm(r, o, run)))(root, Path(tmp))
            outcomes.append(publish_package(ca, ref, owner, npm_spec(root, tgz), run=run, environ=environ, log=log))
    return outcomes


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CodeBuild entry point (needs AWS)
    ap = argparse.ArgumentParser(description="Publish the contract package to the CodeArtifact contract registry (never overwrites).")
    ap.add_argument("--rollback-to", default="none", help="the build's rollback release ID; a rollback publishes nothing")
    ap.add_argument("--no-npm", action="store_true", help="publish the Python wheel only")
    args = ap.parse_args(argv)
    if args.rollback_to not in ("", "none"):
        print(f"contract registry: rollback build ({args.rollback_to}); nothing is published")
        return 0
    import boto3

    from scripts.publish_assets import account_from_build_arn

    session = boto3.session.Session()
    owner = account_from_build_arn(os.environ.get("CODEBUILD_BUILD_ARN")) or session.client("sts").get_caller_identity()["Account"]
    try:
        publish_all(ROOT, ca=session.client("codeartifact"), ssm=session.client("ssm"), owner=owner, npm=not args.no_npm)
    except (PublishRefused, subprocess.CalledProcessError) as exc:
        print(f"CONTRACT PUBLISH FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
