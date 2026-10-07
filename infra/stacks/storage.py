"""Platform storage stack, one per environment (tasks 2.1, 2.2, 2.4, 2.5; design P2).

* One customer-managed KMS key per environment (rotation on), alias
  ``alias/finplan-<env>-financialplanning-platform-kms-key``. PQ-1 (SSE-S3 in beta) is open;
  KMS is used in every environment meanwhile.
* Six buckets (``raw``, ``curated``, ``snapshots``, ``plans``, ``outputs``, ``reports``):
  SSE-KMS with the environment key and S3 Bucket Keys, Block Public Access, BucketOwnerEnforced,
  versioning (protection only, STO-05), lifecycle rules from configuration (STO-06) and the
  resource policies of :mod:`infra.stacks.policies`.
* Physical names: ``finplan-<env>-financialplanning-<stem>-<account-id>`` (``<account-id>`` is
  the ``AWS::AccountId`` pseudo parameter, resolved at deploy time; no literal in any file).
  The ``outputs`` bucket's stem is ``run-staging-area`` because the contract research
  permission boundary (``finplan-<env>-research-permission-boundary``) lets FinanceModel job
  roles write only to ``finplan-<env>-financialplanning-run-staging-area*``; the run-output
  staging area is the ``staging/`` prefix of that bucket.
* Bucket names are published only as SSM parameters
  ``/finplan/<env>/financialplanning/config/bucket-<role>``, plus ``config/run-staging-ref``
  (the staging prefix FinanceModel workers write under).
* Removal: prod buckets and key are retained; beta and gamma are destroyed with the stack once
  lifecycle expiry has emptied them (no auto-delete custom resource: it would need delete rights
  the write-once policies deny).
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Aws, Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from finplan_platform.core.config import BUCKET_ROLES, EnvConfig

from .common import PlatformStack, role_arn_pattern, ssm_name, tag_role
from .policies import bucket_policy_statements, kms_key_policy_statements

__all__ = ["StorageStack", "BUCKET_STEMS", "BUCKET_LOGICAL_ROLES", "bucket_physical_name"]

#: bucket role -> physical name stem
BUCKET_STEMS = {"raw": "raw", "curated": "curated", "snapshots": "snapshots", "plans": "plans", "outputs": "run-staging-area", "reports": "reports"}
#: bucket role -> ownership-matrix ``logical-role`` tag
BUCKET_LOGICAL_ROLES = {
    "raw": "raw-input-bucket",
    "curated": "curated-input-bucket",
    "snapshots": "snapshot-artifact-bucket",
    "plans": "plan-artifact-bucket",
    "outputs": "output-artifact-bucket",
    "reports": "report-artifact-bucket",
}
STAGING_PREFIX = "staging/"


def bucket_physical_name(env: str, role: str, account: str = Aws.ACCOUNT_ID) -> str:
    return f"finplan-{env}-financialplanning-{BUCKET_STEMS[role]}-{account}"


def _lifecycle_rules(cfg: EnvConfig, role: str) -> list[s3.LifecycleRule]:
    ret = cfg.retention
    rules = [
        s3.LifecycleRule(
            id="abort-incomplete-multipart-and-noncurrent",
            abort_incomplete_multipart_upload_after=Duration.days(int(ret["incomplete_multipart_days"])),
            noncurrent_version_expiration=Duration.days(int(ret["noncurrent_version_days"])),
        )
    ]
    days = cfg.retention_days(role)
    if days is not None:
        rules.append(s3.LifecycleRule(id=f"expire-{role}-after-retention", expiration=Duration.days(int(days))))
    if role == "outputs":
        rules.append(s3.LifecycleRule(id="expire-uncommitted-staging", prefix=STAGING_PREFIX, expiration=Duration.days(int(ret["staging_window_days"]))))
    if role == "raw":
        # unclaimed workbook uploads (never committed) are short-lived
        rules.append(s3.LifecycleRule(id="expire-incoming-uploads", prefix="uploads/incoming/", expiration=Duration.days(1)))
    return rules


class StorageStack(PlatformStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, cfg=cfg, description=f"FinancialPlanning platform storage ({cfg.env}): KMS key, six artifact buckets, SSM bucket references", **kwargs)
        env = cfg.env
        prod = env == "prod"
        removal = RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY

        self.key = kms.Key(
            self,
            "PlatformKey",
            alias=f"alias/finplan-{env}-financialplanning-platform-kms-key",
            description=f"finplan {env} platform key (S3 artifacts, DynamoDB metadata)",
            enable_key_rotation=True,
            removal_policy=removal,
            pending_window=Duration.days(30 if prod else 7),
        )
        tag_role(self.key, "platform-kms-key")
        for stmt in kms_key_policy_statements(env=env, arn_for=role_arn_pattern):
            self.key.add_to_resource_policy(iam.PolicyStatement.from_json(stmt))

        partition, account = Aws.PARTITION, Aws.ACCOUNT_ID
        account_root = f"arn:{partition}:iam::{account}:root"
        consumers = {k: str(v["role_name_pattern"]) for k, v in cfg.consumer_principals.items()}
        self.buckets: dict[str, s3.Bucket] = {}
        self.bucket_parameters: dict[str, ssm.StringParameter] = {}
        for role in BUCKET_ROLES:
            bucket = s3.Bucket(
                self,
                f"Bucket{role.capitalize()}",
                bucket_name=bucket_physical_name(env, role),
                encryption=s3.BucketEncryption.KMS,
                encryption_key=self.key,
                bucket_key_enabled=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                versioned=True,
                enforce_ssl=False,  # the TLS deny comes from policies.bucket_policy_statements (one source)
                lifecycle_rules=_lifecycle_rules(cfg, role),
                removal_policy=removal,
                auto_delete_objects=False,
            )
            tag_role(bucket, BUCKET_LOGICAL_ROLES[role])
            cdk.Tags.of(bucket).add("bucket-role", role)
            for stmt in bucket_policy_statements(
                env=env,
                role=role,
                bucket_arn=bucket.bucket_arn,
                key_arn=self.key.key_arn,
                account_root=account_root,
                arn_for=role_arn_pattern,
                consumers=consumers,
            ):
                bucket.add_to_resource_policy(iam.PolicyStatement.from_json(stmt))
            # BucketPolicy is not taggable and references both the bucket and the key, so the
            # ownership check cannot infer one role from references: declare it in Metadata.
            assert bucket.policy is not None
            bucket.policy.node.default_child.add_metadata("logical-role", BUCKET_LOGICAL_ROLES[role])  # type: ignore[union-attr]
            self.buckets[role] = bucket
            param = ssm.StringParameter(
                self,
                f"BucketParam{role.capitalize()}",
                parameter_name=ssm_name(env, "config", f"bucket-{role}"),
                string_value=bucket.bucket_name,
                description=f"Name of the {env} platform {role} bucket",
            )
            tag_role(param, BUCKET_LOGICAL_ROLES[role])
            self.bucket_parameters[role] = param

        self.run_staging_ref = ssm.StringParameter(
            self,
            "RunStagingRef",
            parameter_name=ssm_name(env, "config", "run-staging-ref"),
            string_value=f"s3://{self.buckets['outputs'].bucket_name}/{STAGING_PREFIX}",
            description="Run-output staging reference: FinanceModel workers write <ref><run_id>/... with manifest.json last",
        )
        tag_role(self.run_staging_ref, "run-staging-area")

    # ------------------------------------------------------------------ grants for other stacks
    def grant_artifacts(self, grantee: iam.IGrantable, *, read: tuple[str, ...] = (), write: tuple[str, ...] = (), tag: tuple[str, ...] = ()) -> None:
        """Identity grants for a platform role (bucket policies still apply on top)."""
        for role in read:
            self.buckets[role].grant_read(grantee)
        for role in write:
            b = self.buckets[role]
            grantee.grant_principal.add_to_principal_policy(iam.PolicyStatement(actions=["s3:PutObject"], resources=[b.arn_for_objects("*")]))
            b.grant_read(grantee)
        for role in tag:
            b = self.buckets[role]
            grantee.grant_principal.add_to_principal_policy(iam.PolicyStatement(actions=["s3:PutObjectTagging", "s3:GetObjectTagging"], resources=[b.arn_for_objects("*")]))
        if write:
            self.key.grant_encrypt_decrypt(grantee)
        elif read:
            self.key.grant_decrypt(grantee)

    def bucket_env(self) -> dict[str, str]:
        """Lambda environment variables naming the buckets and key (deploy-time values only)."""
        out = {f"FINPLAN_BUCKET_{r.upper()}": b.bucket_name for r, b in self.buckets.items()}
        out["FINPLAN_KMS_KEY_ARN"] = self.key.key_arn
        return out
