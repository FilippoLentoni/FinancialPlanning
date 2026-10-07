"""CDK assertion tests on the synthesized storage and metadata stacks.

STO-01 (buckets per environment, tags, SSM names), STO-02 (SSE-KMS, TLS), STO-05 (versioning),
STO-06 (lifecycle from configuration), MDS-07 (on-demand tables, PITR, KMS, tags), plus the
contract checks the build stage runs on these templates (role boundaries, cost tags).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template
from finplan_contracts.boundaries import check_role_boundaries

from finplan_platform.core.config import BUCKET_ROLES, load_config
from finplan_platform.core.repository import TABLES, table_name
from infra.stacks.storage import BUCKET_LOGICAL_ROLES

pytestmark = pytest.mark.synth
ENVS = ("beta", "gamma", "prod")
TAG_KEYS = {"project", "owner-repo", "environment", "logical-role"}


def _tmpl(foundation_synth: dict[str, Any], env: str, part: str) -> Template:
    return Template.from_stack(foundation_synth["stacks"][env][part])


def _resources(foundation_synth: dict[str, Any], env: str, part: str, typ: str) -> dict[str, Any]:
    t = foundation_synth["templates"][f"finplan-{env}-financialplanning-{part}"]
    return {k: v for k, v in t["Resources"].items() if v["Type"] == typ}


def _tags(resource: dict[str, Any]) -> dict[str, Any]:
    tags = resource.get("Properties", {}).get("Tags", {})
    if isinstance(tags, list):
        return {t["Key"]: t["Value"] for t in tags}
    return dict(tags)


# ------------------------------------------------------------------ STO-01
def test_six_buckets_per_environment_18_distinct(foundation_synth: dict[str, Any]) -> None:
    resolver = foundation_synth["resolver"]
    names = set()
    for env in ENVS:
        buckets = _resources(foundation_synth, env, "storage", "AWS::S3::Bucket")
        assert len(buckets) == 6
        for b in buckets.values():
            name = resolver.resolve(b["Properties"]["BucketName"], f"finplan-{env}-financialplanning-storage")
            assert name.startswith(f"finplan-{env}-financialplanning-") and name.endswith("-<account-id>")
            assert len(name.replace("<account-id>", "0" * 12)) <= 63
            names.add(name)
            tags = _tags(b)
            assert TAG_KEYS <= set(tags)
            assert tags["environment"] == env and tags["owner-repo"] == "financialplanning" and tags["project"] == "finplan"
            assert tags["logical-role"] in BUCKET_LOGICAL_ROLES.values() and tags["bucket-role"] in BUCKET_ROLES
    assert len(names) == 18


def test_bucket_names_published_only_as_ssm_parameters(foundation_synth: dict[str, Any]) -> None:
    for env in ENVS:
        t = _tmpl(foundation_synth, env, "storage")
        for role in BUCKET_ROLES:
            t.has_resource_properties("AWS::SSM::Parameter", {"Name": f"/finplan/{env}/financialplanning/config/bucket-{role}", "Type": "String", "Value": {"Ref": Match.string_like_regexp("Bucket")}})
        t.has_resource_properties("AWS::SSM::Parameter", {"Name": f"/finplan/{env}/financialplanning/config/run-staging-ref"})
        # bucket names never appear in outputs as literals (no stack outputs other than CDK cross-stack exports)
        outputs = foundation_synth["templates"][f"finplan-{env}-financialplanning-storage"].get("Outputs", {})
        assert all(k.startswith("ExportsOutput") for k in outputs)


def test_outputs_bucket_matches_contract_research_boundary_staging_pattern(foundation_synth: dict[str, Any]) -> None:
    """The FinanceModel research boundary allows writes only to finplan-<env>-financialplanning-run-staging-area*."""
    from finplan_contracts.boundaries import concrete_boundary, research_permission_boundary

    resolver = foundation_synth["resolver"]
    for env in ENVS:
        stack = f"finplan-{env}-financialplanning-storage"
        bucket = next(b for b in _resources(foundation_synth, env, "storage", "AWS::S3::Bucket").values() if _tags(b)["bucket-role"] == "outputs")
        name = resolver.resolve(bucket["Properties"]["BucketName"], stack)
        boundary = concrete_boundary(research_permission_boundary(env))
        allowed = next(s for s in boundary["Statement"] if s.get("Sid") == "DenyWritesOutsideResearchAndStaging")["NotResource"]
        import fnmatch

        assert any(fnmatch.fnmatch(f"arn:aws:s3:::{name}/staging/run_X/manifest.json", p) for p in allowed)


# ------------------------------------------------------------------ STO-02, STO-05
def test_encryption_public_access_ownership_versioning(foundation_synth: dict[str, Any]) -> None:
    for env in ENVS:
        t = _tmpl(foundation_synth, env, "storage")
        t.resource_count_is("AWS::KMS::Key", 1)
        t.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})
        t.has_resource_properties("AWS::KMS::Alias", {"AliasName": f"alias/finplan-{env}-financialplanning-platform-kms-key"})
        buckets = _resources(foundation_synth, env, "storage", "AWS::S3::Bucket")
        (key_lid,) = _resources(foundation_synth, env, "storage", "AWS::KMS::Key")
        for b in buckets.values():
            p = b["Properties"]
            rule = p["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
            assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
            assert rule["ServerSideEncryptionByDefault"]["KMSMasterKeyID"] == {"Fn::GetAtt": [key_lid, "Arn"]}
            assert rule["BucketKeyEnabled"] is True
            assert p["PublicAccessBlockConfiguration"] == {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
            assert p["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
            assert p["VersioningConfiguration"] == {"Status": "Enabled"}  # STO-05: protection only
        for pol in _resources(foundation_synth, env, "storage", "AWS::S3::BucketPolicy").values():
            sids = {s.get("Sid") for s in pol["Properties"]["PolicyDocument"]["Statement"]}
            assert {"DenyInsecureTransport", "DenyNonKmsEncryption", "DenyOtherKmsKey", "DenyOtherEnvironmentPrincipals", "DenyObjectAccessOutsideEnvironment"} <= sids


def test_write_once_policies_on_immutable_buckets(foundation_synth: dict[str, Any]) -> None:
    resolver = foundation_synth["resolver"]
    policies = resolver.bucket_policies()
    for name, doc in policies.items():
        sids = {s.get("Sid") for s in doc["Statement"]}
        role = next(r for r, stem in (("raw", "raw"), ("curated", "curated"), ("snapshots", "snapshots"), ("plans", "plans"), ("outputs", "run-staging-area"), ("reports", "reports")) if f"-financialplanning-{stem}-" in name)
        if role == "curated":
            assert "DenyPutWithoutIfNoneMatch" not in sids
        else:
            assert {"DenyPutWithoutIfNoneMatch", "DenyDeleteImmutableArtifacts"} <= sids


# ------------------------------------------------------------------ STO-06
def _rules(foundation_synth: dict[str, Any], env: str, role: str) -> list[dict[str, Any]]:
    b = next(b for b in _resources(foundation_synth, env, "storage", "AWS::S3::Bucket").values() if _tags(b)["bucket-role"] == role)
    return b["Properties"]["LifecycleConfiguration"]["Rules"]


@pytest.mark.parametrize("env", ENVS)
def test_lifecycle_rules_follow_configuration(foundation_synth: dict[str, Any], env: str) -> None:
    cfg = load_config(env)
    for role in BUCKET_ROLES:
        rules = _rules(foundation_synth, env, role)
        base = next(r for r in rules if r["Id"] == "abort-incomplete-multipart-and-noncurrent")
        assert base["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == cfg.retention["incomplete_multipart_days"]
        assert base["NoncurrentVersionExpiration"]["NoncurrentDays"] == cfg.retention["noncurrent_version_days"]
        expiry = [r for r in rules if r["Id"] == f"expire-{role}-after-retention"]
        days = cfg.retention_days(role)
        if days is None:
            assert expiry == [] and not any("ExpirationInDays" in r and "Prefix" not in r for r in rules)
        else:
            assert expiry[0]["ExpirationInDays"] == days and expiry[0]["Status"] == "Enabled"
    staging = next(r for r in _rules(foundation_synth, env, "outputs") if r["Id"] == "expire-uncommitted-staging")
    assert staging["Prefix"] == "staging/" and staging["ExpirationInDays"] == cfg.retention["staging_window_days"]


def test_beta_and_gamma_expire_prod_plans_do_not(foundation_synth: dict[str, Any]) -> None:
    assert next(r for r in _rules(foundation_synth, "beta", "plans") if r["Id"] == "expire-plans-after-retention")["ExpirationInDays"] == 14
    assert next(r for r in _rules(foundation_synth, "gamma", "plans") if r["Id"] == "expire-plans-after-retention")["ExpirationInDays"] == 30
    assert not [r for r in _rules(foundation_synth, "prod", "plans") if "ExpirationInDays" in r]


def test_prod_storage_is_retained(foundation_synth: dict[str, Any]) -> None:
    for lid, b in _resources(foundation_synth, "prod", "storage", "AWS::S3::Bucket").items():
        assert b["DeletionPolicy"] == "Retain", lid
    for b in _resources(foundation_synth, "beta", "storage", "AWS::S3::Bucket").values():
        assert b["DeletionPolicy"] == "Delete"
        assert "Custom::S3AutoDeleteObjects" not in json.dumps(foundation_synth["templates"])


# ------------------------------------------------------------------ MDS-07 (+3.1)
@pytest.mark.parametrize("env", ENVS)
def test_metadata_tables_on_demand_pitr_kms_tagged(foundation_synth: dict[str, Any], env: str) -> None:
    t = _tmpl(foundation_synth, env, "metadata")
    t.resource_count_is("AWS::DynamoDB::Table", len(TABLES))
    tables = _resources(foundation_synth, env, "metadata", "AWS::DynamoDB::Table")
    by_name = {v["Properties"]["TableName"]: v for v in tables.values()}
    assert set(by_name) == {table_name(env, logical) for logical in TABLES}
    for logical, spec in TABLES.items():
        p = by_name[table_name(env, logical)]["Properties"]
        assert p["BillingMode"] == "PAY_PER_REQUEST" and "ProvisionedThroughput" not in p
        assert p["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert p["SSESpecification"]["SSEEnabled"] is True and p["SSESpecification"]["SSEType"] == "KMS"
        assert "Fn::ImportValue" in p["SSESpecification"]["KMSMasterKeyId"]  # the environment platform key
        tags = _tags(by_name[table_name(env, logical)])
        assert tags["environment"] == env and tags["logical-role"] == spec.logical_role and TAG_KEYS <= set(tags)
        assert {s["Sid"] for s in p["ResourcePolicy"]["PolicyDocument"]["Statement"]} >= {"DenyDataAccessOutsidePlatform"}
        if spec.ttl_attribute:
            assert p["TimeToLiveSpecification"] == {"AttributeName": spec.ttl_attribute, "Enabled": True}
        gsi = {g["IndexName"] for g in p.get("GlobalSecondaryIndexes", [])}
        assert gsi == {ix.name for ix in spec.indexes}
        assert p["DeletionProtectionEnabled"] is (env == "prod")
    audit = by_name[table_name(env, "audit_event")]["Properties"]
    assert "DenyAuditMutation" in {s["Sid"] for s in audit["ResourcePolicy"]["PolicyDocument"]["Statement"]}
    # Regression: DynamoDB rejects stream actions in a table resource policy (first beta deploy failed),
    # and no table has a stream that would need them.
    stream_actions = {"dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"}
    for v in tables.values():
        p = v["Properties"]
        assert "StreamSpecification" not in p
        for s in p["ResourcePolicy"]["PolicyDocument"]["Statement"]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            assert not stream_actions & set(actions), s["Sid"]


def test_idempotency_ttl_and_retention_at_least_seven_days() -> None:
    for env in ENVS:
        assert load_config(env).metadata["idempotency_ttl_days"] >= 7
    assert TABLES["idempotency"].ttl_attribute == "expires_at"


# ------------------------------------------------------------------ build-stage contract checks on these templates
def test_every_role_has_its_environment_boundary_ENV_18(foundation_synth: dict[str, Any]) -> None:
    for env in ENVS:
        for part in ("storage", "metadata"):
            template = foundation_synth["resolver"].resolve(foundation_synth["templates"][f"finplan-{env}-financialplanning-{part}"], f"finplan-{env}-financialplanning-{part}")
            findings = check_role_boundaries(template, environment=env)
            assert findings == [], findings


def test_every_taggable_resource_carries_cost_tags_COST_03(foundation_synth: dict[str, Any]) -> None:
    untaggable = {"AWS::S3::BucketPolicy", "AWS::IAM::Policy", "AWS::Lambda::Permission", "AWS::CDK::Metadata", "AWS::KMS::Alias"}
    for name, t in foundation_synth["templates"].items():
        for lid, r in t["Resources"].items():
            if r["Type"] in untaggable:
                continue
            assert TAG_KEYS <= set(_tags(r)), f"{name}/{lid} ({r['Type']}) lacks {TAG_KEYS - set(_tags(r))}"


def test_no_always_on_or_provisioned_resources_COST_04(foundation_synth: dict[str, Any]) -> None:
    forbidden = {"AWS::EC2::Instance", "AWS::EC2::NatGateway", "AWS::SageMaker::Endpoint", "AWS::RDS::DBInstance", "AWS::ECS::Service", "AWS::EKS::Cluster"}
    for t in foundation_synth["templates"].values():
        for r in t["Resources"].values():
            assert r["Type"] not in forbidden
            if r["Type"] == "AWS::DynamoDB::Table":
                assert r["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_sweeper_role_is_named_for_the_delete_exemption(foundation_synth: dict[str, Any]) -> None:
    for env in ENVS:
        t = _tmpl(foundation_synth, env, "metadata")
        t.has_resource_properties("AWS::IAM::Role", {"RoleName": f"finplan-{env}-financialplanning-metadata-sweeper-role"})
        t.has_resource_properties("AWS::Lambda::Function", {"Handler": "finplan_platform.handlers.sweep.handler", "Runtime": "python3.12"})
        t.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "cron(0 8 * * ? *)"})
