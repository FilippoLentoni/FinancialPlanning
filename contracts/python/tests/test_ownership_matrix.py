"""OWN-02 (baseline assignments, every D1 row, exactly one owner), 12.3, 13.1 and 14.1 rows."""

import re
from collections import Counter

import pytest
import yaml

from conftest import CONTRACTS

REPOS = {"financialplanning", "financemodel", "financelambdastool", "financeagent"}
D1_ROWS = {
    # FinancialPlanning
    "platform-input-buckets": "financialplanning", "platform-plan-artifact-buckets": "financialplanning",
    "run-output-staging-area": "financialplanning", "platform-kms-keys": "financialplanning",
    "platform-metadata-tables": "financialplanning", "plan-lifecycle-api": "financialplanning",
    "ingestion-service": "financialplanning", "daily-scheduler": "financialplanning", "contract-package": "financialplanning",
    "project-budget": "financialplanning", "cost-allocation-tag-keys": "financialplanning", "contract-registry": "financialplanning",
    "website": "financialplanning", "platform-metadata-sweeper": "financialplanning", "permission-boundaries": "financialplanning",
    # FinanceModel
    "research-workspace-storage": "financemodel", "sagemaker-job-definitions": "financemodel", "financemodel-ecr-repositories": "financemodel",
    "job-interface": "financemodel", "model-registry": "financemodel", "job-control-plane": "financemodel",
    "qwen-serving-lifecycle": "financemodel", "run-outputs-and-evidence": "financemodel", "jev-api-key-secret": "financemodel",
    # FinanceLambdasTool
    "mcp-adapter-lambdas": "financelambdastool", "tool-catalog": "financelambdastool",
    # FinanceAgent
    "agentcore-runtime-and-gateway": "financeagent", "financeagent-ecr-repository": "financeagent", "gateway-service-role": "financeagent",
    "oidc-identity-provider": "financeagent", "explanation-provider-config": "financeagent",
    # external
    "external-qwen-batch-stack": "external", "sagemaker-instance-quotas": "external", "bedrock-model-access": "external",
    "market-data-provider-source": "external", "github-codeconnection": "external",
    # pipelines, each repo for itself
    "pipeline-financialplanning": "financialplanning", "pipeline-financemodel": "financemodel",
    "pipeline-financelambdastool": "financelambdastool", "pipeline-financeagent": "financeagent",
    "pipeline-environment-roles-financialplanning": "financialplanning", "pipeline-environment-roles-financemodel": "financemodel",
    "pipeline-environment-roles-financelambdastool": "financelambdastool", "pipeline-environment-roles-financeagent": "financeagent",
}


@pytest.fixture(scope="module")
def matrix():
    return yaml.safe_load((CONTRACTS / "ownership" / "matrix.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(matrix):
    return {r["id"]: r for r in matrix["resources"]}


def test_every_d1_row_present_with_its_owner(rows):
    for rid, owner in D1_ROWS.items():
        assert rid in rows, rid
        assert rows[rid]["owner"] == owner, rid


def test_exactly_one_owner_per_resource(matrix):
    ids = Counter(r["id"] for r in matrix["resources"])
    assert all(n == 1 for n in ids.values()), ids
    for r in matrix["resources"]:
        assert isinstance(r["owner"], str) and r["owner"] in REPOS | {"external"}, r["id"]
        assert r["scope"] in {"environment", "shared", "external", "contract"}, r["id"]


def test_external_rows_are_never_declared(rows):
    for r in rows.values():
        if r["owner"] == "external":
            assert r["scope"] == "external" and r.get("iac_declared") is False, r["id"]


def test_plan_api_owner_and_consumers(rows):
    r = rows["plan-lifecycle-api"]
    assert r["owner"] == "financialplanning"
    assert {"financelambdastool", "website"} <= set(r["consumers"]) and any(c.startswith("financeagent") for c in r["consumers"])


def test_no_openai_row(matrix):
    text = yaml.safe_dump(matrix["resources"]).lower()
    assert "openai" not in text


def test_explanation_provider_is_bedrock(rows):
    r = rows["explanation-provider-config"]
    assert r["owner"] == "financeagent" and r["provider"] == "bedrock"
    assert "/finplan/<env>/financeagent/config/explanation-model-id" in r["reference"]
    assert not any("secret-ref" in ref for ref in r["reference"])


def test_bedrock_model_access_external_with_opus_profile(rows):
    r = rows["bedrock-model-access"]
    assert r["owner"] == "external" and r["model_id"] == "us.anthropic.claude-opus-5"


def test_jev_secret_row(rows):
    r = rows["jev-api-key-secret"]
    assert r["owner"] == "financemodel" and r["scope"] == "shared" and r["pre_existing"] is True and r["iac_declared"] is False
    assert r["consumers"] == ["financemodel-jev-job-roles"]
    assert "/finplan/shared/financemodel/secret-ref/jev-api-key" in r["reference"]


def test_codeconnection_reused_external(rows):
    r = rows["github-codeconnection"]
    assert r["owner"] == "external" and r["reused_existing"] is True
    assert "/finplan/shared/<repo>/config/codeconnection-ref" in r["reference"]


def test_market_data_provider_row(rows):
    """14.1 / OWN-02: yfinance source is external, sole consumer FinancialPlanning ingestion, no secret."""
    r = rows["market-data-provider-source"]
    assert r["owner"] == "external" and r["provider_library"] == "yfinance" and r["secret"] == "none"
    assert r["consumers"] == ["financialplanning-ingestion"]


def test_shared_rows_hold_no_environment_data_and_are_allowed_kinds(matrix, rows):
    """12.3 / ENV-16 (matrix side): only the allowed account-level kinds, none holding environment data."""
    shared = {rid for rid, r in rows.items() if r["scope"] == "shared"}
    assert shared == set(matrix["shared_rows"])
    assert set(matrix["shared_rows"].values()) <= set(matrix["shared_resource_kinds"])
    for rid in shared:
        assert rows[rid]["environment_data"] is False, rid


def test_logical_roles_unique_except_pipelines(rows):
    seen = Counter(role for rid, r in rows.items() if not rid.startswith("pipeline-") for role in r.get("logical_roles", []))
    assert all(n == 1 for n in seen.values()), [k for k, n in seen.items() if n > 1]
    # pipeline roles are shared only between the per-repository pipeline rows and environment-role rows
    pipeline_roles = {role for rid, r in rows.items() if rid.startswith("pipeline-") for role in r.get("logical_roles", [])}
    assert not pipeline_roles & set(seen)
    for repo in REPOS:
        shared_row, env_row = rows[f"pipeline-{repo}"], rows[f"pipeline-environment-roles-{repo}"]
        assert shared_row["scope"] == "shared" and env_row["scope"] == "environment"
        assert set(env_row["logical_roles"]) <= set(shared_row["logical_roles"]) and env_row["resource_types"] == ["AWS::IAM::Role"]


def test_platform_resource_kinds_listed(rows):
    """D13 (0.2.0): the platform rows list every type their constructs synthesize."""
    api = set(rows["plan-lifecycle-api"]["resource_types"])
    assert {"AWS::ApiGateway::Resource", "AWS::ApiGateway::Deployment", "AWS::ApiGateway::Stage", "AWS::ApiGateway::GatewayResponse", "AWS::IAM::Role", "AWS::Logs::LogGroup", "AWS::SSM::Parameter"} <= api
    assert {"AWS::SQS::Queue", "AWS::SQS::QueuePolicy", "AWS::CloudWatch::Alarm", "AWS::IAM::Role", "AWS::SSM::Parameter"} <= set(rows["daily-scheduler"]["resource_types"])
    assert {"AWS::IAM::Role", "AWS::SNS::TopicPolicy", "AWS::SNS::Subscription"} <= set(rows["project-budget"]["resource_types"])
    assert rows["permission-boundaries"]["scope"] == "shared" and rows["permission-boundaries"]["logical_roles"] == ["permission-boundary"]
    for repo in REPOS:
        assert "AWS::S3::BucketPolicy" in rows[f"pipeline-{repo}"]["resource_types"]


def test_identity_provider_final_and_references_registered(rows):
    """Decisions 15a/15b of 2026-10-07: FinanceAgent's per-environment Cognito user pool is final; the
    direct-test principal and the FinanceModel job-role references are registered keys."""
    from finplan_contracts.ssm import find_registered

    idp = rows["oidc-identity-provider"]
    assert idp["owner"] == "financeagent" and "provisional" not in idp
    for ref in ("/finplan/<env>/financeagent/agent/user-pool-ref", "/finplan/<env>/financeagent/agent/authorizer-metadata-ref"):
        assert ref in idp["reference"]
    assert any("/finplan/<env>/financelambdastool/config/direct-test-principal-name" in r for r in rows["mcp-adapter-lambdas"]["reference"])
    assert any("/finplan/<env>/financemodel/job/job-role-ref" in r for r in rows["sagemaker-job-definitions"]["reference"])
    assert any("/finplan/<env>/financemodel/job/job-api-role-ref" in r for r in rows["job-control-plane"]["reference"])
    for path in ("/finplan/beta/financeagent/agent/user-pool-ref", "/finplan/beta/financeagent/agent/authorizer-metadata-ref", "/finplan/beta/financeagent/secret-ref/ci-test-client",
                 "/finplan/beta/financelambdastool/config/direct-test-principal-name", "/finplan/beta/financemodel/job/job-role-ref", "/finplan/beta/financemodel/job/job-api-role-ref"):
        assert find_registered(path) is not None, path


def test_no_account_identifiers_in_matrix():
    """OWN-03 (matrix rows): no 12-digit account IDs, ARNs or bucket names."""
    text = (CONTRACTS / "ownership" / "matrix.yaml").read_text(encoding="utf-8")
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)
    assert "arn:aws" not in text and "s3://" not in text


def test_spec_baseline_assignments(rows):
    owners = {rid: r["owner"] for rid, r in rows.items()}
    assert owners["platform-metadata-tables"] == owners["plan-lifecycle-api"] == owners["ingestion-service"] == "financialplanning"
    assert owners["daily-scheduler"] == owners["contract-package"] == "financialplanning"
    assert owners["research-workspace-storage"] == owners["sagemaker-job-definitions"] == owners["model-registry"] == "financemodel"
    assert owners["mcp-adapter-lambdas"] == "financelambdastool"
    assert owners["agentcore-runtime-and-gateway"] == "financeagent"
