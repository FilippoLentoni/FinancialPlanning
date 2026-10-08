"""Contract publish step (contracts task 7.3, CS-04 unit): publish once, never overwrite, verify digests.

A fake CodeArtifact client stands in for the registry and a fake runner for ``uv publish`` /
``npm publish``; nothing reaches AWS or a package index.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from scripts import publish_contracts as pc
from scripts.check_contracts_pin import ROOT

PIN = json.loads((ROOT / "contracts-pin.json").read_text())
VERSION = PIN["version"]
TOKEN = "synthetic-token-value"
REF = json.dumps({"domain": "finplan", "repository": "contracts", "region": "us-east-2", "formats": ["pypi", "npm"]})


class NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("not found")
        self.response = {"Error": {"Code": "ResourceNotFoundException"}}


class FakeSsm:
    def __init__(self, value: str | None = REF) -> None:
        self.value = value

    def get_parameter(self, Name: str) -> dict[str, Any]:
        assert Name == "/finplan/shared/financialplanning/contract/registry-ref"
        if self.value is None:
            raise NotFound()
        return {"Parameter": {"Value": self.value}}


class FakeCodeArtifact:
    """Versions keyed by (format, namespace, package, version) -> {asset name: bytes}."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str | None, str, str], dict[str, bytes]] = {}
        self.calls: list[str] = []

    def _key(self, kw: dict[str, Any]) -> tuple[str, str | None, str, str]:
        assert kw["domain"] == "finplan" and kw["repository"] == "contracts" and kw["domainOwner"] == "<account-id>"
        return (kw["format"], kw.get("namespace"), kw["package"], kw["packageVersion"])

    def describe_package_version(self, **kw: Any) -> dict[str, Any]:
        self.calls.append("describe")
        if self._key(kw) not in self.versions:
            raise NotFound()
        return {"packageVersion": {"status": "Published"}}

    def list_package_version_assets(self, **kw: Any) -> dict[str, Any]:
        import hashlib

        assets = self.versions[self._key(kw)]
        return {"assets": [{"name": n, "hashes": {"SHA-256": hashlib.sha256(b).hexdigest()}} for n, b in sorted(assets.items())]}

    def get_package_version_asset(self, **kw: Any) -> dict[str, Any]:
        return {"asset": io.BytesIO(self.versions[self._key(kw)][kw["asset"]])}

    def get_authorization_token(self, **kw: Any) -> dict[str, Any]:
        assert kw == {"domain": "finplan", "domainOwner": "<account-id>", "durationSeconds": pc.TOKEN_SECONDS}
        self.calls.append("token")
        return {"authorizationToken": TOKEN}

    def get_repository_endpoint(self, **kw: Any) -> dict[str, Any]:
        return {"repositoryEndpoint": f"https://finplan-owner.d.codeartifact.us-east-2.amazonaws.com/{kw['format']}/contracts/"}


class FakeRunner:
    """Records publish commands; a successful publish stores the artifact in the fake registry."""

    def __init__(self, ca: FakeCodeArtifact, rc: int = 0) -> None:
        self.ca, self.rc = ca, rc
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.npmrc: list[str] = []

    def __call__(self, cmd: list[str], **kw: Any) -> Any:
        env = dict(kw.get("env") or {})
        self.calls.append((list(cmd), env))
        if self.rc == 0:
            path = Path(cmd[-1]) if cmd[0] == "uv" else Path(cmd[2])
            if cmd[0] == "uv":
                self.ca.versions[("pypi", None, "finplan-contracts", VERSION)] = {path.name: path.read_bytes()}
            else:
                self.npmrc.append(Path(env["NPM_CONFIG_USERCONFIG"]).read_text())
                self.ca.versions[("npm", "finplan", "contracts", VERSION)] = {"package.tgz": path.read_bytes()}
        return type("P", (), {"returncode": self.rc})()


def _tgz(files: dict[str, bytes], mtime: int = 499162500) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), mtime
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


NPM_FILES = {"package/package.json": json.dumps({"name": "@finplan/contracts", "version": VERSION}).encode(), "package/dist/index.js": b"export {};\n"}


def _pack(files: dict[str, bytes] = NPM_FILES, mtime: int = 499162500) -> Any:
    def pack(root: Path, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"finplan-contracts-{VERSION}.tgz"
        p.write_bytes(_tgz(files, mtime))
        return p

    return pack


def _publish(ca: FakeCodeArtifact, runner: FakeRunner, **kw: Any) -> list[pc.Outcome]:
    logs: list[str] = []
    out = pc.publish_all(ROOT, ca=ca, ssm=kw.pop("ssm", FakeSsm()), owner="<account-id>", run=runner, pack=kw.pop("pack", _pack()), environ={}, log=logs.append, **kw)
    assert not any(TOKEN in line for line in logs)
    return out


def test_first_publish_uploads_both_packages_and_verifies() -> None:
    ca = FakeCodeArtifact()
    runner = FakeRunner(ca)
    outcomes = _publish(ca, runner)
    assert [(o.spec.fmt, o.action) for o in outcomes] == [("pypi", "published"), ("npm", "published")]
    assert outcomes[0].sha256 == PIN["sha256"]  # the pinned, vendored wheel
    (uv_cmd, uv_env), (npm_cmd, npm_env) = runner.calls
    assert uv_cmd[:2] == ["uv", "publish"] and uv_cmd[-1].endswith(f"finplan_contracts-{VERSION}-py3-none-any.whl")
    assert uv_env["UV_PUBLISH_USERNAME"] == "aws" and uv_env["UV_PUBLISH_PASSWORD"] == TOKEN and uv_env["UV_PUBLISH_URL"].endswith("/pypi/contracts/")
    assert npm_cmd[:2] == ["npm", "publish"] and "--ignore-scripts" in npm_cmd and npm_cmd[npm_cmd.index("--registry") + 1].endswith("/npm/contracts/")
    assert TOKEN not in " ".join(uv_cmd + npm_cmd)  # the token never appears on a command line
    assert runner.npmrc and f"//finplan-owner.d.codeartifact.us-east-2.amazonaws.com/npm/contracts/:_authToken={TOKEN}" in runner.npmrc[0]
    assert not Path(npm_env["NPM_CONFIG_USERCONFIG"]).exists()  # temporary npm config removed


def test_republishing_the_same_version_is_a_no_op_CS_04() -> None:
    ca = FakeCodeArtifact()
    _publish(ca, FakeRunner(ca))
    runner = FakeRunner(ca)
    ca.calls.clear()
    outcomes = _publish(ca, runner, pack=_pack(mtime=1))  # npm pack differs only in archive metadata
    assert [o.action for o in outcomes] == ["already-published", "already-published"]
    assert runner.calls == [] and ca.calls == ["describe", "describe"]  # no token, no upload


def test_a_different_wheel_under_a_published_version_is_refused_CS_04() -> None:
    ca = FakeCodeArtifact()
    ca.versions[("pypi", None, "finplan-contracts", VERSION)] = {f"finplan_contracts-{VERSION}-py3-none-any.whl": b"other bytes"}
    runner = FakeRunner(ca)
    with pytest.raises(pc.PublishRefused, match="immutable"):
        _publish(ca, runner)
    assert runner.calls == []


def test_extra_assets_under_a_published_version_are_refused() -> None:
    ca = FakeCodeArtifact()
    wheel = ROOT / PIN["artifact"]
    ca.versions[("pypi", None, "finplan-contracts", VERSION)] = {wheel.name: wheel.read_bytes(), f"finplan_contracts-{VERSION}.tar.gz": b"sdist"}
    with pytest.raises(pc.PublishRefused, match="extra assets"):
        _publish(ca, FakeRunner(ca))


def test_different_npm_contents_under_a_published_version_are_refused() -> None:
    ca = FakeCodeArtifact()
    _publish(ca, FakeRunner(ca))
    changed = {**NPM_FILES, "package/dist/index.js": b"export const x = 1;\n"}
    runner = FakeRunner(ca)
    with pytest.raises(pc.PublishRefused, match="different contents"):
        _publish(ca, runner, pack=_pack(changed))
    assert runner.calls == []


def test_a_failed_upload_fails_the_build() -> None:
    ca = FakeCodeArtifact()
    with pytest.raises(pc.PublishRefused, match="failed"):
        _publish(ca, FakeRunner(ca, rc=1))


def test_missing_registry_reference_fails_with_a_bootstrap_hint() -> None:
    ca = FakeCodeArtifact()
    with pytest.raises(pc.PublishRefused, match="bootstrapped with the registry"):
        _publish(ca, FakeRunner(ca), ssm=FakeSsm(None))
    with pytest.raises(pc.PublishRefused, match="registry-ref"):
        _publish(ca, FakeRunner(ca), ssm=FakeSsm(json.dumps({"domain": "finplan"})))


def test_publishes_only_the_pinned_wheel_of_the_current_version(tmp_path: Path) -> None:
    for name in ("contracts-pin.json",):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "VERSION").write_text(VERSION + "\n")
    wheel = tmp_path / PIN["artifact"]
    wheel.parent.mkdir(parents=True)
    shutil.copy(ROOT / PIN["artifact"], wheel)
    assert pc.wheel_spec(tmp_path).version == VERSION
    (tmp_path / "contracts" / "VERSION").write_text("9.9.9\n")
    with pytest.raises(pc.PublishRefused, match="re-pin"):
        pc.wheel_spec(tmp_path)
    (tmp_path / "contracts" / "VERSION").write_text(VERSION + "\n")
    wheel.write_bytes(b"tampered")
    with pytest.raises(pc.PublishRefused, match="pinned digest"):
        pc.wheel_spec(tmp_path)


def test_npm_tarball_must_be_the_contract_package_at_the_version(tmp_path: Path) -> None:
    tgz = _pack({"package/package.json": json.dumps({"name": "@finplan/contracts", "version": "0.0.1"}).encode()})(ROOT, tmp_path)
    with pytest.raises(pc.PublishRefused, match="expected @finplan/contracts"):
        pc.npm_spec(ROOT, tgz)


def test_content_digest_ignores_archive_metadata() -> None:
    assert pc.content_digest(_tgz(NPM_FILES, 1)) == pc.content_digest(_tgz(NPM_FILES, 2))
    assert pc.content_digest(_tgz(NPM_FILES)) != pc.content_digest(_tgz({**NPM_FILES, "package/x": b""}))


@pytest.mark.skipif(shutil.which("npm") is None, reason="needs npm (the build image installs nodejs 22)")
def test_real_npm_pack_is_the_contract_package_and_reproducible(tmp_path: Path) -> None:
    a = pc.pack_npm(ROOT, tmp_path / "a")
    b = pc.pack_npm(ROOT, tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
    spec = pc.npm_spec(ROOT, a)
    assert (spec.fmt, spec.namespace, spec.package, spec.version) == ("npm", "finplan", "contracts", VERSION)
