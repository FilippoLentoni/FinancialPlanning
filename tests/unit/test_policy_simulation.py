"""IAM policy simulation over the synthesized resource policies (offline).

STO-02 (TLS, key), STO-03 (cross-env), STO-04 (delete and unconditional overwrite denied),
MDS-06 (no update/delete of audit items), MDS-07 (cross-environment table access). Since
2026-10-18 same-environment roles of other repositories are no longer restricted by these
resource policies (experimental project); only their own IAM policies apply.

Consumers are given a worst-case identity policy (``s3:*``, ``dynamodb:*``, ``kms:*`` on
``*``) so the denials shown come from the platform's resource policies and the contract
permission boundaries, not from the consumer's own restraint.
"""

from __future__ import annotations

from typing import Any

import pytest
from finplan_contracts.boundaries import env_permission_boundary, research_permission_boundary

from infra.policy_sim import Principal, SimResult, role_arn, simulate
from infra.stacks.policies import bucket_policy_statements

ACCT = "<account-id>"
WORST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:*", "dynamodb:*", "kms:*"], "Resource": "*"}]}


def bucket(env: str, stem: str) -> str:
    return f"finplan-{env}-financialplanning-{stem}-{ACCT}"


def obj(env: str, stem: str, key: str) -> str:
    return f"arn:aws:s3:::{bucket(env, stem)}/{key}"


def table(env: str, logical: str) -> str:
    return f"arn:aws:dynamodb:us-east-2:{ACCT}:table/finplan-{env}-financialplanning-{logical}"


def platform(env: str, logical: str) -> Principal:
    return Principal.role(f"finplan-{env}-financialplanning-{logical}-role", WORST, boundary=env_permission_boundary(env))


def fm_job(env: str) -> Principal:
    return Principal.role(f"finplan-{env}-financemodel-job-execution-role", WORST, boundary=research_permission_boundary(env))


def tool(env: str, cls: str) -> Principal:
    return Principal.role(f"finplan-{env}-financelambdastool-tool-role-{cls}", WORST, boundary=env_permission_boundary(env))


def fm_lambda(env: str) -> Principal:
    # FinanceModel Lambda roles carry the general environment boundary (the research
    # job boundary keeps its own metadata-table deny, owned by finplan_contracts).
    return Principal.role(f"finplan-{env}-financemodel-job-api-handler-forward-role", WORST, boundary=env_permission_boundary(env))


def operator(env: str) -> Principal:
    return Principal.role(f"finplan-{env}-financialplanning-operator", WORST)


@pytest.fixture(scope="module")
def policies(foundation_synth: dict[str, Any]) -> dict[str, Any]:
    r = foundation_synth["resolver"]
    return {"buckets": r.bucket_policies(), "tables": r.table_policies(), "resolver": r}


def s3sim(policies: dict[str, Any], action: str, env: str, stem: str, key: str | None, who: Principal, **ctx: Any) -> SimResult:
    resource = obj(env, stem, key) if key is not None else f"arn:aws:s3:::{bucket(env, stem)}"
    return simulate(action, resource, who, resource_policy=policies["buckets"][bucket(env, stem)], context=ctx)


def ddbsim(policies: dict[str, Any], action: str, env: str, logical: str, who: Principal) -> SimResult:
    return simulate(action, table(env, logical), who, resource_policy=policies["tables"][f"finplan-{env}-financialplanning-{logical}"])


APPROVED = {"s3:ExistingObjectTag/snapshot-status": "approved"}


# ------------------------------------------------------------------ STO-03 cross-environment
def test_gamma_principal_cannot_read_prod_bucket(policies: dict[str, Any]) -> None:
    g = platform("gamma", "plan-api-handler")
    assert not s3sim(policies, "s3:GetObject", "prod", "plans", "pl_A/pv_A/content.json", g)
    # the bucket policy alone denies it, even without the boundary
    unbounded = Principal(g.arn, (WORST,))
    res = s3sim(policies, "s3:GetObject", "prod", "plans", "pl_A/pv_A/content.json", unbounded)
    assert res.decision == "explicitDeny" and "DenyOtherEnvironmentPrincipals" in res.resource_denies


def test_same_env_platform_role_reads_plans(policies: dict[str, Any]) -> None:
    assert s3sim(policies, "s3:GetObject", "beta", "plans", "pl_A/pv_A/content.json", platform("beta", "plan-api-handler"))


def test_same_env_consumer_roles_are_not_blocked_by_bucket_policies(policies: dict[str, Any]) -> None:
    for who in (tool("beta", "reader"), fm_job("beta"), Principal.role("finplan-beta-financeagent-runtime-role", WORST)):
        for stem in ("raw", "curated", "plans", "reports", "snapshots"):
            res = s3sim(policies, "s3:GetObject", "beta", stem, "x/y", who)
            assert res.decision != "explicitDeny", (who.arn, stem)
    # other environments stay isolated
    assert s3sim(policies, "s3:GetObject", "beta", "reports", "x/y", fm_job("gamma")).decision == "explicitDeny"


def test_model_job_keeps_snapshot_and_staging_grants(policies: dict[str, Any]) -> None:
    fm = fm_job("beta")
    assert s3sim(policies, "s3:GetObject", "beta", "snapshots", "snap_A/payload/observations.json", fm, **APPROVED)
    assert s3sim(policies, "s3:PutObject", "beta", "run-staging-area", "staging/run_01KDVDNAZ83BAMMYCEGWF33DPM/manifest.json", fm)
    assert not s3sim(policies, "s3:PutObject", "beta", "run-staging-area", "staging/run_X/manifest.json", fm_job("gamma"))


# ------------------------------------------------------------------ STO-04 write-once
@pytest.mark.parametrize("stem,key", [("snapshots", "snap_A/manifest.json"), ("plans", "pl_A/pv_A/content.json"), ("reports", "r/a"), ("run-staging-area", "accepted/run_A/x"), ("raw", "uploads/excel/abc.xlsx"), ("raw", "research-news/v1/deliveries/a.json"), ("curated", "research-news/v1/manifests/a.json")])
def test_application_role_cannot_delete_or_overwrite(policies: dict[str, Any], stem: str, key: str) -> None:
    api = platform("beta", "plan-api-handler")
    deny = s3sim(policies, "s3:DeleteObject", "beta", stem, key, api)
    assert deny.decision == "explicitDeny" and "DenyDeleteImmutableArtifacts" in deny.resource_denies
    assert not s3sim(policies, "s3:DeleteObjectVersion", "beta", stem, key, api)
    unconditional = s3sim(policies, "s3:PutObject", "beta", stem, key, api)
    assert unconditional.decision == "explicitDeny" and "DenyPutWithoutIfNoneMatch" in unconditional.resource_denies
    assert s3sim(policies, "s3:PutObject", "beta", stem, key, api, **{"s3:if-none-match": "*"})


def test_only_the_sweeper_deletes_and_only_in_plans_and_snapshots(policies: dict[str, Any]) -> None:
    resolver = policies["resolver"]
    sweeper_policies = tuple(resolver.role_policies("finplan-beta-financialplanning-metadata-sweeper-role"))
    sweeper = Principal(role_arn("finplan-beta-financialplanning-metadata-sweeper-role"), sweeper_policies, env_permission_boundary("beta"))
    assert s3sim(policies, "s3:DeleteObject", "beta", "plans", "pl_A/pv_A/content.json", sweeper)
    assert s3sim(policies, "s3:DeleteObject", "beta", "snapshots", "snap_A/manifest.json", sweeper)
    assert not s3sim(policies, "s3:DeleteObject", "beta", "reports", "r/a", sweeper)
    assert not s3sim(policies, "s3:DeleteObject", "beta", "run-staging-area", "accepted/run_A/x", sweeper)
    # the sweeper's own identity policy grants nothing in curated/raw
    assert not s3sim(policies, "s3:GetObject", "beta", "curated", "x", sweeper)


def test_curated_and_staging_are_not_write_once(policies: dict[str, Any]) -> None:
    ingest = platform("beta", "ingestion-handler")
    assert s3sim(policies, "s3:PutObject", "beta", "curated", "finance/etf-daily/SPY/2026-01-09/completed_daily/x.json", ingest)


# ------------------------------------------------------------------ STO-02
def test_non_tls_and_other_key_denied(policies: dict[str, Any], foundation_synth: dict[str, Any]) -> None:
    api = platform("beta", "plan-api-handler")
    insecure = s3sim(policies, "s3:GetObject", "beta", "plans", "pl_A/pv_A/content.json", api, **{"aws:SecureTransport": "false"})
    assert insecure.decision == "explicitDeny" and "DenyInsecureTransport" in insecure.resource_denies
    key_arn = next(
        s["Condition"]["StringNotEquals"]["s3:x-amz-server-side-encryption-aws-kms-key-id"]
        for s in policies["buckets"][bucket("beta", "plans")]["Statement"]
        if s.get("Sid") == "DenyOtherKmsKey"
    )
    assert key_arn.startswith("arn:aws:kms:us-east-2:<account-id>:key/")
    other = s3sim(policies, "s3:PutObject", "beta", "plans", "pl/pv/c.json", api, **{"s3:if-none-match": "*", "s3:x-amz-server-side-encryption": "aws:kms", "s3:x-amz-server-side-encryption-aws-kms-key-id": "arn:aws:kms:us-east-2:<account-id>:key/other"})
    assert other.decision == "explicitDeny" and "DenyOtherKmsKey" in other.resource_denies
    sse_s3 = s3sim(policies, "s3:PutObject", "beta", "plans", "pl/pv/c.json", api, **{"s3:if-none-match": "*", "s3:x-amz-server-side-encryption": "AES256"})
    assert sse_s3.decision == "explicitDeny"
    ok = s3sim(policies, "s3:PutObject", "beta", "plans", "pl/pv/c.json", api, **{"s3:if-none-match": "*", "s3:x-amz-server-side-encryption": "aws:kms", "s3:x-amz-server-side-encryption-aws-kms-key-id": key_arn})
    assert ok


# ------------------------------------------------------------------ MDS-07
@pytest.mark.parametrize("logical", ["portfolio", "plan", "plan_version", "publication", "execution", "snapshot_catalog", "staged_output", "idempotency", "audit_event"])
def test_same_env_consumer_roles_are_not_blocked_by_table_policies(policies: dict[str, Any], logical: str) -> None:
    name = logical.replace("_", "-")
    for who in (tool("beta", "reader"), fm_lambda("beta"), Principal.role("finplan-beta-financeagent-runtime-role", WORST)):
        for action in ("dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem"):
            assert ddbsim(policies, action, "beta", name, who).decision != "explicitDeny", (who.arn, action, logical)
    assert ddbsim(policies, "dynamodb:GetItem", "beta", name, platform("beta", "plan-api-handler"))


def test_gamma_platform_role_cannot_read_prod_metadata_MDS_07(policies: dict[str, Any]) -> None:
    g = platform("gamma", "plan-api-handler")
    assert not ddbsim(policies, "dynamodb:GetItem", "prod", "plan-version", g)
    unbounded = Principal(g.arn, (WORST,))
    res = ddbsim(policies, "dynamodb:GetItem", "prod", "plan-version", unbounded)
    assert res.decision == "explicitDeny" and "DenyOtherEnvironmentPrincipals" in res.resource_denies


# ------------------------------------------------------------------ MDS-06
def test_no_application_role_can_update_or_delete_audit_items_MDS_06(policies: dict[str, Any]) -> None:
    for logical in ("plan-api-handler", "ingestion-handler", "metadata-sweeper"):
        who = platform("beta", logical)
        for action in ("dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"):
            res = ddbsim(policies, action, "beta", "audit-event", who)
            assert res.decision == "explicitDeny" and "DenyAuditMutation" in res.resource_denies, (logical, action)
        assert ddbsim(policies, "dynamodb:PutItem", "beta", "audit-event", who)
    # the sweeper's real identity policy does not even grant UpdateItem on the audit table
    sweeper = Principal(role_arn("finplan-beta-financialplanning-metadata-sweeper-role"), tuple(policies["resolver"].role_policies("finplan-beta-financialplanning-metadata-sweeper-role")))
    assert not simulate("dynamodb:UpdateItem", table("beta", "audit-event"), sweeper)


# ------------------------------------------------------------------ builder used directly (no synth)
def test_policy_builder_matches_synthesized_statements(policies: dict[str, Any]) -> None:
    built = bucket_policy_statements(
        env="beta",
        role="snapshots",
        bucket_arn=f"arn:aws:s3:::{bucket('beta', 'snapshots')}",
        key_arn="arn:aws:kms:us-east-2:<account-id>:key/k",
        account_root="arn:aws:iam::<account-id>:root",
        arn_for=lambda p: f"arn:aws:iam::<account-id>:role/{p}",
        consumers={"operator": "finplan-beta-financialplanning-operator*", "financemodel_job": "finplan-beta-financemodel-job-execution*"},
    )
    synthesized = policies["buckets"][bucket("beta", "snapshots")]["Statement"]
    assert [s["Sid"] for s in built] == [s["Sid"] for s in synthesized]
