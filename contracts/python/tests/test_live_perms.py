"""ENV-05 live-financial permission scan with fixture policies (task 6.4)."""

import json
from pathlib import Path

import pytest

from finplan_contracts import live_perms
from finplan_contracts.live_perms import is_live_action, scan_document, scan_file, scan_paths

POLICIES = Path(__file__).parent / "fixtures" / "policies"
PASSING = sorted(POLICIES.glob("pass-*"))
FAILING = {
    "fail-allow-payment-action.json": {"live-action"},
    "fail-allow-trading-action.json": {"live-action"},
    "fail-allow-all.json": {"live-wildcard"},
    "fail-notaction.json": {"live-wildcard"},
    "fail-agentcore-wildcard.yaml": {"live-wildcard"},
    "fail-brokerage-secret-template.json": {"live-secret"},
    "fail-declares-wallet-secret.json": {"live-secret"},
}


def test_every_failing_fixture_is_listed():
    assert {p.name for p in POLICIES.glob("fail-*")} == set(FAILING)
    assert len(PASSING) == 3


@pytest.mark.parametrize("path", PASSING, ids=[p.name for p in PASSING])
def test_passing_policies(path):
    assert scan_file(path) == []


@pytest.mark.parametrize("name", sorted(FAILING))
def test_failing_policies(name):
    """ENV-05: a policy or secret reference granting live trading, payment or wallet access fails."""
    findings = scan_file(POLICIES / name)
    assert findings, name
    assert {f.rule for f in findings} == FAILING[name]


def test_brokerage_secret_template_reports_policy_and_reference():
    findings = scan_file(POLICIES / "fail-brokerage-secret-template.json")
    assert len(findings) == 2
    assert any("brokerage" in f.detail for f in findings) and any("coinbase" in f.detail for f in findings)


def test_boundary_deny_neutralizes_allow_all_in_same_document():
    doc = json.loads((POLICIES / "pass-boundary-allow-all-deny-live.json").read_text())
    assert scan_document(doc) == []
    doc["Statement"][1]["Condition"] = {"StringEquals": {"aws:RequestedRegion": "us-east-2"}}
    assert scan_document(doc), "a conditional deny does not neutralize the allow"


def test_action_classification():
    assert is_live_action("bedrock-agentcore:CreatePayment")
    assert is_live_action("payments:Anything") and is_live_action("payment-cryptography:CreateKey")
    assert is_live_action("brokerage:PlaceOrder") and is_live_action("exchange:Withdraw")
    for ok in ("s3:GetObject", "bedrock:InvokeModel", "sagemaker:CreateTrainingJob", "s3:PutBucketRequestPayment", "ssm:PutParameter", "secretsmanager:GetSecretValue"):
        assert not is_live_action(ok), ok


def test_scoped_wildcards_without_live_matches_pass():
    doc = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:Get*", "dynamodb:*", "bedrock-agentcore:Get*"], "Resource": "*"}]}
    assert scan_document(doc) == []
    doc["Statement"][0]["Action"].append("bedrock-agentcore:Create*")
    [f] = scan_document(doc)
    assert f.rule == "live-wildcard"


def test_oauth_token_exchange_secret_is_not_a_trading_exchange():
    doc = {"Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": "arn:aws:secretsmanager:us-east-2:<account-id>:secret:finplan/beta/financeagent/token-exchange-client-*"}]}
    assert scan_document(doc) == []


def test_scan_directory_and_cli(capsys):
    count, findings = scan_paths([POLICIES])
    assert count == len(PASSING) + len(FAILING)
    assert {Path(f.file).name for f in findings} == set(FAILING)
    assert live_perms.main([str(p) for p in PASSING]) == 0
    assert live_perms.main([str(POLICIES / "fail-allow-all.json"), "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["ok"] is False
