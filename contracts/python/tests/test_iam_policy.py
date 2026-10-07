"""Task 8.3 / 12.2: SSM IAM policy fragments, checked with the offline policy evaluator (ENV-07 simulation)."""

from __future__ import annotations

import pytest

from finplan_contracts.iam import EXPLICIT_DENY, IMPLICIT_DENY, Request, budget_state_writer_policy, evaluate, is_allowed, ssm_access_policy, to_cfn

from infra_helpers import concrete, ssm_arn


@pytest.fixture
def fm_gamma():
    return concrete(ssm_access_policy("financemodel", "gamma"))


def test_own_segment_write_allowed(fm_gamma):
    assert is_allowed("ssm:PutParameter", ssm_arn("/finplan/gamma/financemodel/api/job-endpoint"), [fm_gamma])


def test_cross_repo_write_denied(fm_gamma):
    r = evaluate(Request("ssm:PutParameter", ssm_arn("/finplan/gamma/financialplanning/config/run-staging-ref")), [fm_gamma])
    assert r.decision == EXPLICIT_DENY


def test_cross_env_write_denied(fm_gamma):
    assert not is_allowed("ssm:PutParameter", ssm_arn("/finplan/prod/financemodel/api/job-endpoint"), [fm_gamma])


def test_shared_write_needs_bootstrap_role(fm_gamma):
    assert not is_allowed("ssm:PutParameter", ssm_arn("/finplan/shared/financemodel/secret-ref/jev-api-key"), [fm_gamma])
    boot = concrete(ssm_access_policy("financemodel", "gamma", shared_writes=True))
    assert is_allowed("ssm:PutParameter", ssm_arn("/finplan/shared/financemodel/secret-ref/jev-api-key"), [boot])
    assert not is_allowed("ssm:PutParameter", ssm_arn("/finplan/shared/financialplanning/config/budget-state"), [boot])


def test_reads_same_env_and_shared_only(fm_gamma):
    assert is_allowed("ssm:GetParameter", ssm_arn("/finplan/gamma/financelambdastool/lambda/role-submitter-arn"), [fm_gamma])
    assert is_allowed("ssm:GetParameter", ssm_arn("/finplan/shared/financemodel/secret-ref/jev-api-key"), [fm_gamma])
    assert is_allowed("ssm:GetParametersByPath", ssm_arn("/finplan/gamma"), [fm_gamma])
    r = evaluate(Request("ssm:GetParameter", ssm_arn("/finplan/prod/financialplanning/api/plan-endpoint")), [fm_gamma])
    assert r.decision == IMPLICIT_DENY


def test_explicit_deny_beats_a_broad_allow(fm_gamma):
    broad = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ssm:*", "Resource": "*"}]}
    assert not is_allowed("ssm:PutParameter", ssm_arn("/finplan/gamma/financeagent/agent/gateway-principal-ref"), [broad, fm_gamma])


def test_budget_state_writer_is_the_only_shared_runtime_writer():
    pol = concrete(budget_state_writer_policy())
    assert is_allowed("ssm:PutParameter", ssm_arn("/finplan/shared/financialplanning/config/budget-state"), [pol])
    for path in (
        "/finplan/shared/financialplanning/config/budget-allocation",
        "/finplan/shared/financialplanning/config/cost-ceiling-usd",
        "/finplan/shared/financemodel/secret-ref/jev-api-key",
        "/finplan/beta/financialplanning/config/run-staging-ref",
    ):
        assert not is_allowed("ssm:PutParameter", ssm_arn(path), [pol]), path
    assert is_allowed("ssm:GetParameter", ssm_arn("/finplan/shared/financialplanning/config/budget-allocation"), [pol])
    # a runtime role with the normal per-env deploy fragment cannot write shared at all
    deploy = concrete(ssm_access_policy("financialplanning", "prod"))
    assert not is_allowed("ssm:PutParameter", ssm_arn("/finplan/shared/financialplanning/config/budget-state"), [deploy])


def test_to_cfn_wraps_pseudo_parameters_and_no_account_literals():
    import json
    import re

    doc = to_cfn(ssm_access_policy("financeagent", "beta"))
    text = json.dumps(doc)
    assert "Fn::Sub" in text and "${AWS::AccountId}" in text
    assert not re.search(r"\d{12}", text)


def test_evaluator_condition_semantics():
    pol = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b-<sfx>/*", "Condition": {"StringEquals": {"aws:ResourceTag/environment": "${aws:PrincipalTag/environment}"}}}]}
    ok = Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k", {"aws:ResourceTag/environment": "beta", "aws:PrincipalTag/environment": "beta"})
    bad = Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k", {"aws:ResourceTag/environment": "prod", "aws:PrincipalTag/environment": "beta"})
    missing = Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k", {"aws:PrincipalTag/environment": "beta"})
    assert evaluate(ok, [pol]).allowed and not evaluate(bad, [pol]).allowed and not evaluate(missing, [pol]).allowed
    deny = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}, {"Effect": "Deny", "Action": "*", "Resource": "*", "Condition": {"StringNotEqualsIfExists": {"aws:ResourceTag/environment": ["beta", "shared"]}}}]}
    assert not evaluate(Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k", {}), [deny]).allowed  # IfExists: missing key -> true -> deny
    assert evaluate(Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k", {"aws:ResourceTag/environment": "shared"}), [deny]).allowed
    na = {"Statement": [{"Effect": "Allow", "NotAction": "iam:*", "NotResource": "arn:aws:s3:::secret-<sfx>/*"}]}
    assert evaluate(Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k"), [na]).allowed
    assert not evaluate(Request("iam:CreateRole", "*"), [na]).allowed
    assert not evaluate(Request("s3:GetObject", "arn:aws:s3:::secret-<sfx>/k"), [na]).allowed
    # boundary must also allow
    boundary = {"Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
    allow_all = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    assert evaluate(Request("s3:GetObject", "arn:aws:s3:::b-<sfx>/k"), [allow_all], boundary).allowed
    assert not evaluate(Request("dynamodb:GetItem", "arn:aws:dynamodb:us-east-2:<account-id>:table/t"), [allow_all], boundary).allowed
