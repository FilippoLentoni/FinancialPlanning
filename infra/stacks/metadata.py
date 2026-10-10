"""Platform metadata stack, one per environment (tasks 3.1, 3.4, 3.5, 2.4; design P3).

* Nine on-demand DynamoDB tables from :data:`finplan_platform.core.repository.TABLES` (the same
  specification the repository, the fake and moto use): ``portfolio``, ``plan``,
  ``plan_version``, ``publication``, ``execution``, ``snapshot_catalog``, ``staged_output``,
  ``idempotency`` (TTL attribute ``expires_at``; records live 8 days, at least the contract's 7)
  and ``audit_event``. Each has point-in-time recovery, encryption with the environment platform
  key, the contract tags and a resource policy (:func:`infra.stacks.policies.table_policy_statements`):
  item-level access only for this environment's platform roles, and no update/delete of audit
  items by anyone.
* The daily sweeper function (orphan sweep, MDS-02; expired-snapshot catalog sweep, STO-06) with
  its explicitly named role ``finplan-<env>-financialplanning-metadata-sweeper-role``, the only
  principal the bucket policies let delete in ``plans``/``snapshots``. It is triggered by a daily
  EventBridge rule (UTC; the time of day does not matter for a sweep).
* Removal: prod tables are retained and deletion-protected.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from finplan_platform.core.config import EnvConfig
from finplan_platform.core.repository import TABLES, table_name

from .common import PlatformStack, lambda_code, platform_role, resource_name, role_arn_pattern, tag_role
from .policies import table_policy_statements

__all__ = ["MetadataStack"]


class MetadataStack(PlatformStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, storage: Any, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, cfg=cfg, description=f"FinancialPlanning platform metadata ({cfg.env}): DynamoDB tables and daily sweeps", **kwargs)
        env = cfg.env
        prod = env == "prod"
        self.storage = storage
        self.tables: dict[str, dynamodb.Table] = {}
        partition, region, account = cdk.Aws.PARTITION, cdk.Aws.REGION, cdk.Aws.ACCOUNT_ID
        for logical, spec in TABLES.items():
            if env != "beta" and logical in ("portfolio_decision", "portfolio_history", "activity_event"):
                continue
            name = table_name(env, logical)
            account_policy = iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement.from_json(s)
                    for s in table_policy_statements(env=env, logical=logical, table_arn=f"arn:{partition}:dynamodb:{region}:{account}:table/{name}", arn_for=role_arn_pattern)
                ]
            )
            table = dynamodb.Table(
                self,
                f"Table{''.join(p.capitalize() for p in logical.split('_'))}",
                table_name=name,
                partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=spec.sort_key, type=dynamodb.AttributeType.STRING) if spec.sort_key else None,
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
                encryption_key=storage.key,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=bool(cfg.metadata["point_in_time_recovery"])),
                time_to_live_attribute=spec.ttl_attribute,
                deletion_protection=prod,
                removal_policy=RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY,
                resource_policy=account_policy,
            )
            for ix in spec.indexes:
                table.add_global_secondary_index(
                    index_name=ix.name,
                    partition_key=dynamodb.Attribute(name=ix.partition_key, type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name=ix.sort_key, type=dynamodb.AttributeType.STRING) if ix.sort_key else None,
                    projection_type=dynamodb.ProjectionType.ALL,
                )
            tag_role(table, spec.logical_role)
            self.tables[logical] = table

        # ---------------------------------------------------------------- daily sweeper
        role = platform_role(
            self,
            "SweeperRole",
            cfg=cfg,
            logical="metadata-sweeper",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"finplan {env} orphan and expired-snapshot sweeper",
        )
        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        log_group = logs.LogGroup(self, "SweeperLogs", log_group_name=f"/aws/lambda/{resource_name(env, 'metadata-sweeper')}", retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        tag_role(log_group, "metadata-sweeper")
        self.sweeper = lambda_.Function(
            self,
            "Sweeper",
            function_name=resource_name(env, "metadata-sweeper"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="finplan_platform.handlers.sweep.handler",
            code=lambda_code("sweeper"),
            role=role,
            timeout=Duration.minutes(5),
            memory_size=256,
            log_group=log_group,
            environment={
                "FINPLAN_ENV": env,
                "FINPLAN_BUCKET_PLANS": storage.buckets["plans"].bucket_name,
                "FINPLAN_BUCKET_SNAPSHOTS": storage.buckets["snapshots"].bucket_name,
                "FINPLAN_ORPHAN_GRACE_HOURS": str(cfg.metadata["orphan_grace_hours"]),
                "FINPLAN_SNAPSHOT_RETENTION_DAYS": "" if cfg.retention_days("snapshots") is None else str(cfg.retention_days("snapshots")),
            },
            description="Daily orphan-artifact sweep (24 h grace) and expired-snapshot catalog sweep",
        )
        tag_role(self.sweeper, "metadata-sweeper")
        for logical in ("plan_version", "snapshot_catalog"):
            self.tables[logical].grant_read_data(role)
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"],
                resources=[self.tables["snapshot_catalog"].table_arn],
            )
        )
        role.add_to_principal_policy(iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[self.tables["audit_event"].table_arn]))
        for b in ("plans", "snapshots"):
            bucket = storage.buckets[b]
            role.add_to_principal_policy(iam.PolicyStatement(actions=["s3:ListBucket"], resources=[bucket.bucket_arn]))
            role.add_to_principal_policy(iam.PolicyStatement(actions=["s3:GetObject", "s3:DeleteObject"], resources=[bucket.arn_for_objects("*")]))
        storage.key.grant_encrypt_decrypt(role)
        rule = events.Rule(
            self,
            "DailySweep",
            rule_name=resource_name(env, "metadata-sweep"),
            schedule=events.Schedule.cron(minute="0", hour="8"),
            description="Daily metadata sweep (08:00 UTC)",
        )
        rule.add_target(targets.LambdaFunction(self.sweeper, retry_attempts=2))
        tag_role(rule, "metadata-sweeper")

    # ------------------------------------------------------------------ grants for other stacks
    def grant_metadata(self, grantee: iam.IGrantable, *, read: tuple[str, ...] = (), write: tuple[str, ...] = ()) -> None:
        """Identity grants for a platform role. Writes are PutItem/UpdateItem/ConditionCheckItem
        (what ``TransactWriteItems`` needs); never DeleteItem; audit events are PutItem only."""
        for logical in read:
            self.tables[logical].grant_read_data(grantee)
        for logical in write:
            t = self.tables[logical]
            actions = ["dynamodb:PutItem", "dynamodb:ConditionCheckItem"]
            if logical != "audit_event":
                actions.append("dynamodb:UpdateItem")
            grantee.grant_principal.add_to_principal_policy(iam.PolicyStatement(actions=actions, resources=[t.table_arn]))
            t.grant_read_data(grantee)
        if read or write:
            self.storage.key.grant_encrypt_decrypt(grantee)

    def table_env(self) -> dict[str, str]:
        return {"FINPLAN_ENV": self.cfg.env}
