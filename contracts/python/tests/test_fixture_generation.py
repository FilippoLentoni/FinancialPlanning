"""6.1: fixtures and the case manifest are deterministic (regenerating changes nothing)."""

import filecmp
import shutil
import subprocess
import sys

from conftest import CONTRACTS


def test_regeneration_is_byte_identical(tmp_path):
    root = tmp_path / "contracts"
    root.mkdir()
    for part in ("VERSION", "core", "finance", "domains"):
        src = CONTRACTS / part
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / part)
    (root / "fixtures").mkdir()
    shutil.copytree(CONTRACTS / "fixtures" / "vectors", root / "fixtures" / "vectors")
    proc = subprocess.run([sys.executable, str(CONTRACTS / "python" / "scripts" / "gen_fixtures.py"), "--root", str(root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    cmp = filecmp.dircmp(CONTRACTS / "fixtures", root / "fixtures")

    def diffs(c):
        out = list(c.diff_files) + list(c.left_only) + list(c.right_only)
        for sub in c.subdirs.values():
            out += diffs(sub)
        return out

    assert diffs(cmp) == []
    assert (root / "conformance" / "cases.yaml").read_text() == (CONTRACTS / "conformance" / "cases.yaml").read_text()
