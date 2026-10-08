"""OWN-03 / ENV-08 identifier and secret leak scan, positive and negative fixtures, whole-repo run, CS-09 fixture part.

The fixture texts in fixtures/leak/cases.json carry {{TOKEN}} placeholders so the
repository itself stays clean; the look-alike values below are assembled from pieces
at run time for the same reason.
"""

import json
from pathlib import Path

import pytest

from finplan_contracts import conformance, leak_scan
from finplan_contracts.leak_scan import DEFAULT_EXCLUDE_DIRS, scan_paths, scan_text

from conftest import CONTRACTS

CASES = json.loads((Path(__file__).parent / "fixtures" / "leak" / "cases.json").read_text())
REPO_ROOT = CONTRACTS.parent

TOKENS = {
    "ACCOUNT_ID": "4821" + "3907" + "5516",
    "ACCOUNT_DASHED": "-".join(["4821", "3907", "5516"]),
    "BUCKET": "-".join(["finplan", "prod", "raw-input"]),
    "API_ID": "a1b2c3" + "d4e5",
    "AWS_KEY_ID": "AK" + "IA" + "QQQQ7777ZZZZ2X3Y",
    "AWS_SECRET": "a1B2" * 10,
    "APIKEY": "api" + "key_" + "Zx9" * 6,
    "APIKEY_PREFIX": "api" + "key_",
    "PEM": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "OPENAI": "sk" + "-proj-" + "A1b2C3d4" * 3,
    "GITHUB": "gh" + "p_" + "A1b2C3" * 6,
    "BEARER": "eyJ" + "hbGciOi" + "JIUzI1NiJ9abc123",
    "SECRET_VALUE": "S3cr" + "etV4lue" + "9x7Q2",
}


def materialize(text: str) -> str:
    for k, v in TOKENS.items():
        text = text.replace("{{" + k + "}}", v)
    assert "{{" not in text, text
    return text


def test_every_token_is_defined():
    assert set(CASES["tokens"]) == set(TOKENS)


@pytest.mark.parametrize("case", CASES["positive"], ids=[c["name"] for c in CASES["positive"]])
def test_positive_fixture_is_flagged(case):
    findings = scan_text(materialize(case["text"]), case["name"])
    assert {f.rule for f in findings} == set(case["rules"]), [str(f) for f in findings]


@pytest.mark.parametrize("case", CASES["negative"], ids=[c["name"] for c in CASES["negative"]])
def test_negative_fixture_passes(case):
    findings = scan_text(case["text"], case["name"])
    assert findings == [], [str(f) for f in findings]


@pytest.mark.parametrize("case", CASES["positive"], ids=[c["name"] for c in CASES["positive"]])
def test_committed_fixture_texts_are_clean(case):
    """The committed (placeholder) form never trips the scan."""
    assert scan_text(case["text"], case["name"]) == []


def test_findings_are_masked():
    findings = scan_text(f"key = {TOKENS['APIKEY']}\naccount {TOKENS['ACCOUNT_ID']}")
    assert findings and all(TOKENS["APIKEY"] not in str(f) and TOKENS["ACCOUNT_ID"] not in str(f) for f in findings)


def test_env08_jev_secret_ref_holds_only_the_name():
    """ENV-08: the secret-ref parameter carries the secret name; a value in its place is flagged."""
    name_only = {"name": "/finplan/shared/financemodel/secret-ref/jev-api-key", "value": "finplan/shared/financemodel/jev-api-key"}
    assert scan_text(json.dumps(name_only)) == []
    with_value = dict(name_only, value=TOKENS["APIKEY"])
    assert [f.rule for f in scan_text(json.dumps(with_value))] == ["apikey"]
    generic = dict(name_only, value=TOKENS["SECRET_VALUE"])
    assert [f.rule for f in scan_text(json.dumps({"api_key": generic["value"]}))] == ["secret-assignment"]


def test_own03_whole_repository_passes():
    """OWN-03 / tasks 1.4, 10.4, 14.2: the scan passes over this whole repository.

    Untracked build products (git-ignored) are excluded as the platform's leak-scan gate does: a
    release synth leaves third-party Lambda dependency bundles in ``.build/`` for its assembly.
    """
    count, findings = scan_paths([REPO_ROOT], exclude_dirs=DEFAULT_EXCLUDE_DIRS | {".build", "build-output", "cdk.out.bootstrap"})
    assert count > 100
    assert findings == [], [str(f) for f in findings[:20]]


def test_scan_fails_on_committed_identifier(tmp_path):
    repo = tmp_path / "repo"
    (repo / "infra").mkdir(parents=True)
    (repo / "infra" / "stack.py").write_text(f'ROLE = "arn:aws:iam::{TOKENS["ACCOUNT_ID"]}:role/x"\n')
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text(f'"{TOKENS["ACCOUNT_ID"]}"\n')
    count, findings = scan_paths([repo])
    assert [(f.path, f.rule) for f in findings] == [("infra/stack.py", "arn")]
    assert leak_scan.main([str(repo)]) == 1


def test_allow_file_and_cli_allow(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "doc.md").write_text(f"see s3://{TOKENS['BUCKET']}/x\n")
    assert leak_scan.main([str(repo)]) == 1
    capsys.readouterr()
    pattern = "s3://" + TOKENS["BUCKET"]
    assert leak_scan.main([str(repo), "--allow", pattern]) == 0
    capsys.readouterr()
    (repo / leak_scan.ALLOW_FILE).write_text(f"# documented public sample\n{pattern}\n")
    assert leak_scan.main([str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_files_from_list(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text(f"id {TOKENS['ACCOUNT_ID']}\n")
    b = tmp_path / "b.txt"
    b.write_text("clean\n")
    listing = tmp_path / "files.txt"
    listing.write_text(f"{b}\n")
    assert leak_scan.main(["--files-from", str(listing)]) == 0
    listing.write_text(f"{a}\n{b}\n")
    assert leak_scan.main(["--files-from", str(listing)]) == 1


def test_self_declared_fake_tokens_and_short_names_pass():
    assert scan_text('key = "sk' + '-synthetic-not-a-real-key-0000"') == []
    assert scan_text('Resource: "arn:aws:s3:::b/k"') == []  # 'b' is not a valid bucket name
    assert [f.rule for f in scan_text('Resource: "arn:aws:s3:::' + TOKENS["BUCKET"] + '/k"')] == ["s3-bucket"]


def test_binary_files_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01" + TOKENS["ACCOUNT_ID"].encode())
    assert scan_paths([tmp_path])[1] == []


# ------------------------------------------------------------- CS-09 hygiene
def test_cs09_all_contract_fixtures_are_synthetic_and_clean():
    """CS-09: every fixture is flagged synthetic (or a listed exception) and passes the leak scan."""
    root = CONTRACTS
    assert conformance.check_synthetic_flags(root) == []
    files = conformance.fixture_files(root)
    assert len(files) >= 300
    assert leak_scan.fixture_check(root, files) == []


def test_cs09_leak_in_fixture_fails_the_suite(tmp_path):
    import shutil

    root = tmp_path / "contracts"
    root.mkdir()
    for part in ("VERSION", "core", "finance", "domains", "fixtures", "conformance"):
        src = CONTRACTS / part
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / part)
    path = root / "fixtures" / "caller" / "valid" / "hosted-agent.json"
    doc = json.loads(path.read_text())
    doc["correlation_id"] = f"arn:aws:iam::{TOKENS['ACCOUNT_ID']}:role/leak"
    path.write_text(json.dumps(doc))
    report = conformance.run_suite(root)
    assert any(p.check == "CS-09" and p.subject == "caller/valid/hosted-agent.json" and "[arn]" in p.message for p in report.problems)
