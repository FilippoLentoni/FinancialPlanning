"""Contract package pin (task 1.2; CS-04 consumer case): exact version + digest, mismatch fails the build."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_contracts_pin import ROOT, check

PIN = json.loads((ROOT / "contracts-pin.json").read_text())


def _copy_root(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", "contracts-pin.json"):
        shutil.copy(ROOT / name, tmp_path / name)
    wheel = ROOT / PIN["artifact"]
    dest = tmp_path / PIN["artifact"]
    dest.parent.mkdir(parents=True)
    shutil.copy(wheel, dest)
    return tmp_path


def test_repository_pin_verifies() -> None:
    assert check(ROOT) == []


def test_installed_package_is_the_pinned_version() -> None:
    import finplan_contracts

    assert finplan_contracts.__version__ == PIN["version"]
    from finplan_contracts.schemas import contracts_root

    # validators read the schemas bundled in the pinned wheel, not a copy in this repo
    assert "site-packages" in str(contracts_root())


def test_digest_mismatch_fails_the_build(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    wheel = root / PIN["artifact"]
    data = bytearray(wheel.read_bytes())
    data[-10] ^= 0xFF  # one flipped byte
    wheel.write_bytes(bytes(data))
    problems = check(root)
    assert any(p.startswith("Digest mismatch") for p in problems)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_contracts_pin.py"), "--root", str(root)], capture_output=True, text=True)
    assert proc.returncode == 1 and "Digest mismatch" in proc.stdout


def test_wrong_pinned_digest_fails(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    pin = json.loads((root / "contracts-pin.json").read_text())
    pin["sha256"] = "0" * 64
    (root / "contracts-pin.json").write_text(json.dumps(pin))
    problems = check(root)
    assert any("Digest mismatch" in p for p in problems)


def test_version_range_is_rejected(tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    version = json.loads((root / "contracts-pin.json").read_text())["version"]
    text = (root / "pyproject.toml").read_text()
    assert f'"finplan-contracts=={version}"' in text
    py = text.replace(f'"finplan-contracts=={version}"', '"finplan-contracts>=0.1"')
    (root / "pyproject.toml").write_text(py)
    assert any("exactly" in p for p in check(root))


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_1x_pin_is_allowed_in_every_environment(env: str) -> None:
    """Contracts 1.0.0 (D16): the stable pin may be promoted to gamma and prod."""
    assert int(PIN["version"].split(".")[0]) >= 1
    assert check(ROOT, env=env) == []
    assert set(PIN["served_environments"]) == {"beta", "gamma", "prod"}


@pytest.mark.parametrize("env", ["gamma", "prod"])
def test_0x_pin_is_beta_only(env: str, tmp_path: Path) -> None:
    root = _copy_root(tmp_path)
    pin = json.loads((root / "contracts-pin.json").read_text())
    pin["version"] = "0.2.2"
    pin_path = root / "pin-0x.json"
    pin_path.write_text(json.dumps(pin))
    problems = check(root, env=env, pin_path=pin_path)
    assert any("beta only" in p for p in problems)
    assert not any("beta only" in p for p in check(root, env="beta", pin_path=pin_path))
