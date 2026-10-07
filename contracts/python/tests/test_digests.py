"""CS-04 unit: reproducible build outputs and SHA-256 digests in the release metadata (task 7.2).

Rebuilding the same tree must give identical digests. The raw tarball is always
checked; the wheel/sdist need uv (offline, cached build backend) and the npm tarball
needs npm with the TypeScript package's dev dependencies installed. The test skips
only the artifact whose tool is missing and says why.
"""

import gzip
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

import pytest

from finplan_contracts import digests
from finplan_contracts.digests import build_all, build_raw_tarball, sha256_file, verify

from conftest import CONTRACTS


@pytest.fixture()
def tree(tmp_path):
    """A private copy of the contract data (the raw tarball's inputs)."""
    root = tmp_path / "contracts"
    root.mkdir()
    for part in ("VERSION", "core", "finance", "domains", "fixtures", "conformance", "ownership"):
        src = CONTRACTS / part
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / part)
    return root


def test_raw_tarball_is_reproducible(tree, tmp_path):
    a = build_raw_tarball(tmp_path / "a", tree, epoch=1700000000)
    b = build_raw_tarball(tmp_path / "b", tree, epoch=1700000000)
    assert a.name == b.name == f"finplan-contracts-schemas-{(tree / 'VERSION').read_text().strip()}.tar.gz"
    assert sha256_file(a) == sha256_file(b)


def test_raw_tarball_ignores_file_mtimes_and_owners(tree, tmp_path):
    first = sha256_file(build_raw_tarball(tmp_path / "a", tree, epoch=1700000000))
    later = time.time() + 3600
    for p in tree.rglob("*"):
        os.utime(p, (later, later))
    assert sha256_file(build_raw_tarball(tmp_path / "b", tree, epoch=1700000000)) == first


def test_raw_tarball_changes_with_content_and_epoch(tree, tmp_path):
    base = sha256_file(build_raw_tarball(tmp_path / "a", tree, epoch=1700000000))
    assert sha256_file(build_raw_tarball(tmp_path / "b", tree, epoch=1700000001)) != base
    (tree / "fixtures" / "README.md").write_text((tree / "fixtures" / "README.md").read_text() + "\n")
    assert sha256_file(build_raw_tarball(tmp_path / "c", tree, epoch=1700000000)) != base


def test_raw_tarball_layout_is_normalized(tree, tmp_path):
    path = build_raw_tarball(tmp_path / "a", tree, epoch=1700000000)
    with gzip.open(path) as gz:
        assert gz.read(1)  # valid gzip
    raw = path.read_bytes()
    assert int.from_bytes(raw[4:8], "little") == 1700000000  # gzip header mtime
    with tarfile.open(path) as tf:
        members = tf.getmembers()
    names = [m.name for m in members]
    assert names[0] == f"finplan-contracts-{(tree / 'VERSION').read_text().strip()}"
    assert names[1:] == sorted(names[1:])
    assert all(m.mtime == 1700000000 and m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" for m in members)
    assert any(n.endswith("/core/v1/plan.json") for n in names) and any("/fixtures/" in n for n in names)
    assert not any("__pycache__" in n for n in names)


def test_source_date_epoch_from_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1234567890")
    assert digests.source_date_epoch() == 1234567890
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert digests.source_date_epoch() == digests.DEFAULT_SOURCE_DATE_EPOCH


def test_verify_detects_digest_mismatch(tree, tmp_path, capsys):
    """CS-04 "Digest mismatch": a downloaded artifact whose digest differs from the pin fails."""
    path = build_raw_tarball(tmp_path / "a", tree)
    good = sha256_file(path)
    assert verify(path, good) and verify(path, "sha256:" + good)
    assert not verify(path, "0" * 64)
    assert digests.main(["verify", str(path), "--sha256", good]) == 0
    assert digests.main(["verify", str(path), "--sha256", "f" * 64]) == 1
    assert "FAIL" in capsys.readouterr().out


def _have_uv() -> bool:
    return shutil.which("uv") is not None


def _have_npm() -> bool:
    return shutil.which("npm") is not None and (CONTRACTS / "typescript" / "node_modules" / "typescript").is_dir()


@pytest.mark.skipif(not _have_uv(), reason="uv not on PATH")
def test_full_build_twice_gives_identical_digests(tmp_path):
    """CS-04 unit: rebuilding the same tree gives identical digests for every artifact, recorded in the release metadata."""
    npm = _have_npm()
    m1 = build_all(tmp_path / "one", epoch=1700000000, npm=npm, offline=True, source_commit="synthetic-commit")
    m2 = build_all(tmp_path / "two", epoch=1700000000, npm=npm, offline=True, source_commit="synthetic-commit")
    kinds = {a.kind for a in m1.artifacts}
    assert {"schema-tarball", "python-wheel", "python-sdist"} <= kinds, m1.skipped
    if npm:
        assert "npm-tarball" in kinds, m1.skipped
    assert m1.to_dict() == m2.to_dict()
    meta = json.loads((tmp_path / "one" / "release-metadata.json").read_text())
    assert meta == json.loads((tmp_path / "two" / "release-metadata.json").read_text())
    assert meta["version"] == (CONTRACTS / "VERSION").read_text().strip() and meta["source_commit"] == "synthetic-commit"
    for art in meta["artifacts"]:
        assert len(art["sha256"]) == 64 and sha256_file(tmp_path / "one" / art["file"]) == art["sha256"]
        assert art["file"].find(meta["version"]) != -1


def test_metadata_records_skipped_artifacts(tmp_path):
    meta = build_all(tmp_path / "out", epoch=1700000000, python=False, npm=False)
    assert [a.kind for a in meta.artifacts] == ["schema-tarball"]
    assert {s["kind"] for s in meta.skipped} == {"python", "npm-tarball"}


def test_npm_skipped_when_typescript_package_absent(tree, tmp_path):
    meta = build_all(tmp_path / "out", root=tree, epoch=1700000000, python=False)
    assert {"kind": "npm-tarball", "reason": "contracts/typescript does not exist"} in meta.skipped


def test_cli_tarball_and_file(tree, tmp_path, capsys):
    assert digests.main(["tarball", "--out", str(tmp_path / "t"), "--root", str(tree), "--source-date-epoch", "1700000000"]) == 0
    line = capsys.readouterr().out.strip()
    digest, path = line.split("  ", 1)
    assert sha256_file(Path(path)) == digest
    assert digests.main(["file", path]) == 0
    assert digest in capsys.readouterr().out
