"""Task 1.1: one version file; Python and npm packages carry the same version."""

import json
import re

import finplan_contracts

from conftest import CONTRACTS

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")


def test_single_version_file_is_semver():
    version = (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()
    assert SEMVER.match(version)


def test_python_version_matches_version_file():
    assert finplan_contracts.__version__ == (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()


def test_npm_package_version_matches_version_file():
    pkg = json.loads((CONTRACTS / "typescript" / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "@finplan/contracts"
    assert pkg["version"] == (CONTRACTS / "VERSION").read_text(encoding="utf-8").strip()


def test_pyproject_reads_version_from_version_file():
    text = (CONTRACTS / "python" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "finplan-contracts"' in text
    assert 'dynamic = ["version"]' in text
    assert 'path = "../VERSION"' in text


def test_layout_exists():
    for part in ("core/v1", "finance/v1", "fixtures", "ownership", "conformance", "python", "typescript", "domains"):
        assert (CONTRACTS / part).is_dir(), part
