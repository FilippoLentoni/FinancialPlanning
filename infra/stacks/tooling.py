"""Account-level tooling stack (environment ``shared``; OPS-owned; tasks 9.1, 9.2, 10.1, 10.5).

One stack, ``finplan-shared-financialplanning-tooling``, deployed **only** by the authenticated
bootstrap (``scripts/bootstrap.py``; design P10, contracts D11/D12). It never deploys through the
pipeline it defines. Contents:

* **Permission boundaries** (contract templates, design D5): ``finplan-<env>-permission-boundary``
  and ``finplan-<env>-research-permission-boundary`` for beta, gamma and prod, and
  ``finplan-shared-permission-boundary``. Every platform stack applies its environment boundary by
  name (:class:`infra.stacks.common.PlatformStack`); every account-level role in this stack
  carries the shared boundary, and the per-environment pipeline roles (deploy, CloudFormation
  execution and stage role of one environment) carry that environment's tag and boundary
  (contracts D13, ENV-21; :mod:`infra.stacks.pipeline`). The documents come from
  :mod:`finplan_contracts.boundaries` (never copied) and are CloudFormation-ready (``Fn::Sub``
  with the partition, region and account pseudo parameters).
* **Project budget** (9.1; COST-01): one AWS Budgets ``COST`` budget whose limit is the SSM
  parameter ``/finplan/shared/financialplanning/config/cost-ceiling-usd`` (resolved by
  CloudFormation at deploy time; the bootstrap writes the default from ``config/shared.json``
  when absent). Notifications at 50%, 80% and 100% of ACTUAL spend and 100% of FORECASTED spend
  go to the SNS topic ``budget-alert-topic``. A human supplies the e-mail subscriber as the
  ``NotificationEmail`` stack parameter at bootstrap time (local untracked configuration); it is
  never committed. ``ScopeBudgetToProjectTag=true`` limits the budget to the ``project`` cost
  allocation tag once a human has activated it; until then the budget covers the whole account
  (design P9).
* **Enforcement action** (9.2; COST-02): at 100% of ACTUAL spend the Budgets action attaches the
  deny policy ``finplan-budget-enforcement-deny`` (:func:`finplan_contracts.budget.enforcement_deny_policy`:
  billable compute, Bedrock invocations, pipeline executions and builds; reads untouched) to the
  roles this stack creates (pipeline, build, deploy and stage roles) plus the role names given in
  ``AdditionalEnforcedRoleNames``. The bootstrap fills that parameter from the published
  ``/finplan/<env>/<repo>/config/budget-enforced-role-names`` values (contract D4). Approval model
  ``AUTOMATIC``; every boundary forbids detaching the deny policy, so only a human removes it.
  The bootstrap/admin identity is never in the list.
* **Budget-state writer** (9.2): an SNS-subscribed Lambda (inline code from
  :mod:`finplan_platform.handlers.budget_state`) that sets
  ``/finplan/shared/financialplanning/config/budget-state`` to ``enforced`` on an ACTUAL alert at
  or above the budgeted amount or an executed budget action. Its role may write that one
  parameter only (:func:`finplan_contracts.iam.budget_state_writer_policy`). Ingestion and
  FinanceModel pre-flight checks read the flag (``core/budget.py``, COST-05). Its log group
  ``/aws/lambda/<function>`` is declared explicitly (:func:`add_log_group`: 30-day retention,
  deleted with the stack), as are the CodeBuild project log groups of :mod:`infra.stacks.pipeline`.

Two account-level stacks, both deployed only by the bootstrap (never by the pipeline):

* ``finplan-shared-financialplanning-pipeline-store`` (:class:`StoreStack`): the pipeline store
  bucket (CodePipeline artifacts, content-addressed CDK file assets under ``assets/``, the release
  ledger under ``releases/``, the staged tooling template under ``bootstrap/``). Versioned;
  pipeline artifacts, ``assets/`` and ``bootstrap/`` expire after 30 days, the build cache after
  14, noncurrent versions after 7, and incomplete multipart uploads are aborted after 7. The
  ledger under ``releases/`` never expires. Retained on stack deletion (``docs/bootstrap.md``,
  "Teardown"). It is small and has no assets; it uses :class:`aws_cdk.LegacyStackSynthesizer`, so the
  CLI deploys it inline with the operator's credentials, no staging bucket and no role.
* ``finplan-shared-financialplanning-tooling`` (:class:`ToolingStack`): everything above plus the
  pipeline (:mod:`infra.stacks.pipeline` adds it to this stack). Its template exceeds the
  51,200-byte inline limit, so it uses a :class:`aws_cdk.CliCredentialsStackSynthesizer` that stages
  the template in the store under ``bootstrap/`` with the operator's CLI credentials. Neither stack
  needs the CDK bootstrap (``CDKToolkit``) stack, whose roles would carry no finplan permission
  boundary.

Environment stacks deployed by the pipeline use :func:`deployment_synthesizer`: file assets go to
the pipeline store bucket under ``assets/`` (published by the build stage,
``scripts/publish_assets.py``) and no bootstrap-version rule is emitted. ``scripts/synth.py``
synthesizes with it; ``infra/app.py`` (plain ``npx aws-cdk@2 synth``) keeps the CDK default for
local work.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import jsii
from aws_cdk import Aws, CfnCondition, CfnParameter, Duration, Fn, Tags
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_ssm as ssm
from constructs import Construct, IConstruct
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import budget as contract_budget
from finplan_contracts import iam as contract_iam
from finplan_contracts import ssm as contract_ssm

__all__ = [
    "ASSET_PREFIX",
    "BUDGET_NAME",
    "IMAGE_REPOSITORY_NAME",
    "PIPELINE_NAME",
    "PIPELINE_STORE_STEM",
    "REPO",
    "STORE_CONSTRUCT_ID",
    "STORE_STACK_NAME",
    "TOOLING_CONSTRUCT_ID",
    "TOOLING_STACK_NAME",
    "StoreStack",
    "ToolingStack",
    "add_log_group",
    "add_to_app",
    "budget_state_writer_source",
    "deployment_synthesizer",
    "get_tooling_stack",
    "pipeline_store_bucket_name",
    "shared_name",
    "tooling_synthesizer",
]

REPO = "financialplanning"
TOOLING_CONSTRUCT_ID = "Tooling"
TOOLING_STACK_NAME = f"finplan-shared-{REPO}-tooling"
STORE_CONSTRUCT_ID = "PipelineStore"
STORE_STACK_NAME = f"finplan-shared-{REPO}-pipeline-store"
#: Equals the contract bootstrap default ``finplan-shared-<repo>-pipeline`` (BootstrapConfig).
PIPELINE_NAME = f"finplan-shared-{REPO}-pipeline"
BUDGET_NAME = f"finplan-shared-{REPO}-project-budget"
PIPELINE_STORE_STEM = "pipeline-store"
ASSET_PREFIX = "assets/"
#: Where the CLI stages the tooling template at bootstrap (it exceeds the 51,200-byte inline limit).
BOOTSTRAP_PREFIX = "bootstrap/"
RELEASES_PREFIX = "releases/"
CACHE_PREFIX = "cache/"
#: Container-image assets (only if the ingestion function must become an image, task 6.17). The
#: repository is NOT created yet: the ownership matrix has no FinancialPlanning image-repository
#: row (reported contract gap). The zip form is the default and needs no image assets.
IMAGE_REPOSITORY_NAME = f"finplan-shared-{REPO}-images"
WRITER_FUNCTION_LOGICAL = "budget-state-writer"
REPO_ROOT = Path(__file__).resolve().parents[2]
WRITER_SOURCE = REPO_ROOT / "platform" / "finplan_platform" / "handlers" / "budget_state.py"
#: CloudFormation inline (ZipFile) code limit.
INLINE_CODE_LIMIT = 4096
#: Retention of every tooling log group (budget-state writer, CodeBuild projects).
LOG_RETENTION = logs.RetentionDays.ONE_MONTH
#: Lifecycle of the pipeline store (StoreStack).
STORE_PREFIX_EXPIRY_DAYS = 30
STORE_NONCURRENT_EXPIRY_DAYS = 7
STORE_ABORT_MULTIPART_DAYS = 7


def shared_name(logical: str, suffix: str | None = None) -> str:
    """``finplan-shared-financialplanning-<logical>[-<suffix>]`` (contract naming convention)."""
    return contract_boundaries.resource_name(contract_ssm.SHARED, REPO, logical, suffix)


def pipeline_store_bucket_name(account: str = Aws.ACCOUNT_ID) -> str:
    """The pipeline artifact/asset/release store (``finplan-shared-financialplanning-pipeline-store-<account>``)."""
    return f"{shared_name(PIPELINE_STORE_STEM)}-{account}"


def _env_stack_synthesizer() -> cdk.CliCredentialsStackSynthesizer:
    return cdk.CliCredentialsStackSynthesizer(
        file_assets_bucket_name=pipeline_store_bucket_name("${AWS::AccountId}"),
        bucket_prefix=ASSET_PREFIX,
        image_assets_repository_name=IMAGE_REPOSITORY_NAME,
        qualifier="finplan",
    )


@jsii.implements(cdk.IReusableStackSynthesizer)
class _PerStackSynthesizer:
    """App-level default that binds a **fresh** synthesizer to every stack.

    A single synthesizer instance passed as ``App(default_stack_synthesizer=...)`` shares one
    asset-manifest builder across stacks in this CDK version (every stack's ``*.assets.json`` then
    lists the templates of all earlier stacks, with paths relative to the wrong assembly).
    """

    def reusable_bind(self, stack: cdk.Stack) -> cdk.IBoundStackSynthesizer:
        synth = _env_stack_synthesizer()
        synth.bind(stack)
        return synth


def deployment_synthesizer() -> cdk.IReusableStackSynthesizer:
    """Synthesizer for the environment stacks the pipeline deploys (see module docstring).

    Assets are addressed by content hash under ``assets/`` in the pipeline store; templates carry
    no ``BootstrapVersion`` rule; no role ARN is baked in (the pipeline's CloudFormation actions use
    the scoped deploy roles).
    """
    return _PerStackSynthesizer()


def tooling_synthesizer() -> cdk.CliCredentialsStackSynthesizer:
    """The tooling stack's template is staged in the store (``bootstrap/``) with CLI credentials."""
    return cdk.CliCredentialsStackSynthesizer(
        file_assets_bucket_name=pipeline_store_bucket_name("${AWS::AccountId}"),
        bucket_prefix=BOOTSTRAP_PREFIX,
        image_assets_repository_name=IMAGE_REPOSITORY_NAME,
        qualifier="finplan",
    )


def budget_state_writer_source() -> str:
    src = WRITER_SOURCE.read_text(encoding="utf-8")
    if len(src.encode("utf-8")) > INLINE_CODE_LIMIT:
        raise ValueError(f"{WRITER_SOURCE.name} exceeds the {INLINE_CODE_LIMIT}-byte inline code limit")
    return src


def _arn(service: str, resource: str, *, region: bool = True) -> str:
    """ARN from CloudFormation pseudo parameters (no account literal ever appears in a file)."""
    partition, account = Aws.PARTITION, Aws.ACCOUNT_ID
    reg = Aws.REGION if region else ""
    return f"arn:{partition}:{service}:{reg}:{account}:{resource}"


def tag_role(construct: IConstruct, logical_role: str) -> None:
    Tags.of(construct).add("logical-role", logical_role)


def _metadata_role(resource: cdk.CfnResource, logical_role: str) -> None:
    """Ownership attribution for types that cannot carry tags (matrix lookup by metadata)."""
    resource.add_metadata("logical-role", logical_role)


def _cost_tags(logical_role: str) -> dict[str, str]:
    """Contract cost-allocation tags for an account-level resource (explicit where CDK cannot tag)."""
    return contract_ssm.cost_allocation_tags(REPO, contract_ssm.SHARED, logical_role)


def add_log_group(scope: Construct, cid: str, name: str, logical_role: str) -> logs.LogGroup:
    """An explicit log group: 30-day retention, deleted with the stack, tagged with its owner's logical role.

    Declaring the group (instead of letting the service create it on first write) bounds the
    retention and lets the stack remove it. The ``logical-role`` tag attributes it to the owning
    matrix row (the group's name references no template resource, so it cannot be parent-attributed).
    """
    group = logs.LogGroup(scope, cid, log_group_name=name, retention=LOG_RETENTION, removal_policy=cdk.RemovalPolicy.DESTROY)
    tag_role(group, logical_role)
    return group


def _shared_tags(stack: cdk.Stack) -> None:
    base = contract_ssm.cost_allocation_tags(REPO, contract_ssm.SHARED, "placeholder")
    for key in ("project", "owner-repo", "environment"):
        Tags.of(stack).add(key, base[key])


class StoreStack(cdk.Stack):
    """The pipeline store bucket (see module docstring)."""

    def __init__(self, scope: Construct, construct_id: str, *, shared: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(
            scope,
            construct_id,
            stack_name=STORE_STACK_NAME,
            env=cdk.Environment(region=str(shared["region"])),
            # Not BootstraplessSynthesizer: in this CDK version it still writes the default
            # cdk-hnb659fds deploy/exec role ARNs into the manifest, which needs CDKToolkit.
            # Not CliCredentialsStackSynthesizer: it stages the template in a cdk-<qualifier>-assets
            # bucket that does not exist. The legacy synthesizer sends this small, asset-free
            # template inline with the operator's credentials and references no role.
            synthesizer=cdk.LegacyStackSynthesizer(),
            termination_protection=True,
            description="FinancialPlanning pipeline store (environment shared): pipeline artifacts, content-addressed CDK assets, release ledger. Deployed only by the authenticated bootstrap.",
            **kwargs,
        )
        _shared_tags(self)
        self.bucket = s3.Bucket(
            self,
            "Store",
            bucket_name=pipeline_store_bucket_name(),
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            versioned=True,
            # Retained on stack deletion (it holds the release ledger); docs/bootstrap.md "Teardown"
            # describes emptying (every version) and deleting it by hand.
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                # CodePipeline stores artifacts under the first 20 characters of the pipeline name
                s3.LifecycleRule(id="pipeline-artifacts", prefix=PIPELINE_NAME[:20] + "/", expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="build-cache", prefix=CACHE_PREFIX, expiration=Duration.days(14)),
                # content-addressed CDK file assets (republished by every build) and the staged tooling template
                s3.LifecycleRule(id="assets", prefix=ASSET_PREFIX, expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="bootstrap", prefix=BOOTSTRAP_PREFIX, expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="noncurrent", noncurrent_version_expiration=Duration.days(STORE_NONCURRENT_EXPIRY_DAYS), abort_incomplete_multipart_upload_after=Duration.days(STORE_ABORT_MULTIPART_DAYS)),
            ],
        )
        tag_role(self.bucket, "pipeline-artifact-bucket")


class ToolingStack(cdk.Stack):
    """The account-level tooling stack (see module docstring)."""

    def __init__(self, scope: Construct, construct_id: str, *, shared: Mapping[str, Any], **kwargs: Any) -> None:
        super().__init__(
            scope,
            construct_id,
            stack_name=TOOLING_STACK_NAME,
            env=cdk.Environment(region=str(shared["region"])),
            synthesizer=tooling_synthesizer(),
            termination_protection=True,
            description="FinancialPlanning account-level tooling (environment shared): permission boundaries, project budget, alerts, enforcement action, budget-state writer and the platform pipeline. Deployed only by the authenticated bootstrap.",
            **kwargs,
        )
        self.shared = shared
        _shared_tags(self)
        #: role names the budget action denies (registered by the pipeline module).
        self.enforced_roles: list[iam.Role] = []
        self._boundaries()
        self._budget()

    # ------------------------------------------------------------ boundaries (design D5)
    def _managed_policy(self, cid: str, name: str, document: dict[str, Any], logical_role: str, description: str) -> iam.CfnManagedPolicy:
        pol = iam.CfnManagedPolicy(self, cid, managed_policy_name=name, policy_document=document, description=description[:1000])
        _metadata_role(pol, logical_role)
        return pol

    def _boundaries(self) -> None:
        self.boundary_policies: dict[str, iam.CfnManagedPolicy] = {}
        for env in contract_ssm.ENVIRONMENTS:
            cap = env.capitalize()
            self.boundary_policies[env] = self._managed_policy(
                f"{cap}PermissionBoundary",
                contract_boundaries.boundary_name(env),
                contract_boundaries.env_permission_boundary(env),
                "permission-boundary",
                f"Permission boundary of every {env} role a finplan pipeline creates (contracts D5).",
            )
            self.boundary_policies[f"research-{env}"] = self._managed_policy(
                f"{cap}ResearchPermissionBoundary",
                contract_boundaries.research_boundary_name(env),
                contract_boundaries.research_permission_boundary(env),
                "permission-boundary",
                f"Permission boundary of FinanceModel {env} research and job-execution roles (contracts D5, ENV-04).",
            )
        self.shared_boundary = self._managed_policy(
            "SharedPermissionBoundary",
            contract_boundaries.boundary_name(contract_ssm.SHARED),
            contract_boundaries.shared_permission_boundary(),
            "permission-boundary",
            "Permission boundary of account-level tooling roles (contracts D5).",
        )
        boundary = iam.ManagedPolicy.from_managed_policy_arn(self, "SharedBoundaryRef", self.shared_boundary.ref)
        iam.PermissionsBoundary.of(self).apply(boundary)

    # ------------------------------------------------------------ budget (9.1, 9.2)
    def _budget(self) -> None:
        self.notification_email = CfnParameter(
            self,
            "NotificationEmail",
            type="String",
            default="",
            description="Budget alert e-mail subscriber, supplied by a human at bootstrap from local untracked configuration (never committed). Empty: no e-mail subscription.",
        )
        self.additional_roles = CfnParameter(
            self,
            "AdditionalEnforcedRoleNames",
            type="CommaDelimitedList",
            default="",
            description="Role names published at /finplan/<env>/<repo>/config/budget-enforced-role-names; the bootstrap fills this list (role names only, never ARNs).",
        )
        self.scope_to_tag = CfnParameter(
            self,
            "ScopeBudgetToProjectTag",
            type="String",
            default="false",
            allowed_values=["false", "true"],
            description="true once a human has activated the 'project' cost-allocation tag; until then the budget covers the whole account (design P9).",
        )
        self.time_unit = CfnParameter(self, "BudgetTimeUnit", type="String", default="ANNUALLY", allowed_values=["MONTHLY", "QUARTERLY", "ANNUALLY"], description="Budget period (the ceiling is the project total).")
        has_email = CfnCondition(self, "HasNotificationEmail", expression=Fn.condition_not(Fn.condition_equals(self.notification_email.value_as_string, "")))
        self.has_additional = CfnCondition(self, "HasAdditionalEnforcedRoles", expression=Fn.condition_not(Fn.condition_equals(Fn.join("", self.additional_roles.value_as_list), "")))
        scoped = CfnCondition(self, "ScopeToProjectTag", expression=Fn.condition_equals(self.scope_to_tag.value_as_string, "true"))

        # the ceiling: resolved by CloudFormation from SSM at deploy time (no literal in any file)
        self.ceiling = ssm.StringParameter.value_for_string_parameter(self, contract_budget.CEILING_PARAMETER)

        self.topic = sns.Topic(self, "BudgetAlertTopic", topic_name=shared_name("budget-alert-topic"), display_name="finplan project budget alerts")
        tag_role(self.topic, "budget-alert-topic")
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsToPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            )
        )
        for child in self.topic.node.find_all():
            if isinstance(child, sns.CfnTopicPolicy):
                _metadata_role(child, "budget-alert-topic")
        email = sns.CfnSubscription(self, "BudgetAlertEmail", protocol="email", topic_arn=self.topic.topic_arn, endpoint=self.notification_email.value_as_string)
        email.cfn_options.condition = has_email
        _metadata_role(email, "budget-alert-topic")

        notifications = []
        for kind, threshold in [("ACTUAL", t) for t in contract_budget.ALERT_THRESHOLDS_PERCENT] + [("FORECASTED", 100)]:
            notifications.append(
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(notification_type=kind, comparison_operator="GREATER_THAN", threshold=threshold, threshold_type="PERCENTAGE"),
                    subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="SNS", address=self.topic.topic_arn)],
                )
            )
        self.budget = budgets.CfnBudget(
            self,
            "ProjectBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=BUDGET_NAME,
                budget_type="COST",
                time_unit=self.time_unit.value_as_string,
                budget_limit=budgets.CfnBudget.SpendProperty(amount=cdk.Token.as_number(self.ceiling), unit="USD"),
                cost_filters=Fn.condition_if(scoped.logical_id, {"TagKeyValue": [f"user:project${contract_ssm.PROJECT_TAG_VALUE}"]}, Aws.NO_VALUE),
            ),
            notifications_with_subscribers=notifications,
            resource_tags=[budgets.CfnBudget.ResourceTagProperty(key=k, value=v) for k, v in _cost_tags("project-budget").items()],
        )
        _metadata_role(self.budget, "project-budget")  # ResourceTags are not read by the ownership check
        # the topic policy must exist before Budgets validates the SNS subscriber
        for child in self.topic.node.find_all():
            if isinstance(child, sns.CfnTopicPolicy):
                self.budget.node.add_dependency(child)

        # deny policy attached by the action at 100% (contract document, never copied)
        self.deny_policy = self._managed_policy(
            "BudgetEnforcementDenyPolicy",
            contract_boundaries.BUDGET_DENY_POLICY_NAME,
            contract_budget.enforcement_deny_policy(),
            "budget-deny-policy",
            "Attached by the budget action at 100% of the ceiling; only a human removes it (ENV-19).",
        )
        self.action_role = iam.Role(
            self,
            "BudgetActionRole",
            role_name=shared_name("budget-action", "role"),
            assumed_by=iam.ServicePrincipal("budgets.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}}),
            description="AWS Budgets action execution role: attaches the budget deny policy only.",
            inline_policies={
                "apply-budget-deny-policy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="AttachTheDenyPolicyOnly",
                            actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                            resources=[_arn("iam", "role/*", region=False)],
                            conditions={"ArnEquals": {"iam:PolicyARN": self.deny_policy.ref}},
                        )
                    ]
                )
            },
        )
        tag_role(self.action_role, "budget-action")

        own = cdk.Lazy.list(_Producer(lambda: [r.role_name for r in self.enforced_roles]), omit_empty=False)
        roles = Fn.condition_if(
            self.has_additional.logical_id,
            Fn.split(",", Fn.join(",", [Fn.join(",", own), Fn.join(",", self.additional_roles.value_as_list)])),
            own,
        )
        self.action = budgets.CfnBudgetsAction(
            self,
            "BudgetEnforcementAction",
            budget_name=BUDGET_NAME,
            notification_type="ACTUAL",
            action_type="APPLY_IAM_POLICY",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(type="PERCENTAGE", value=contract_budget.ENFORCEMENT_THRESHOLD_PERCENT),
            approval_model="AUTOMATIC",
            execution_role_arn=self.action_role.role_arn,
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(policy_arn=self.deny_policy.ref, roles=cdk.Token.as_list(roles))
            ),
            subscribers=[budgets.CfnBudgetsAction.SubscriberProperty(type="SNS", address=self.topic.topic_arn)],
            resource_tags=[budgets.CfnBudgetsAction.ResourceTagProperty(key=k, value=v) for k, v in _cost_tags("budget-action").items()],
        )
        self.action.node.add_dependency(self.budget)
        _metadata_role(self.action, "budget-action")

        # budget-state writer (the single shared runtime SSM writer, contract D4)
        writer_doc = iam.PolicyDocument.from_json(contract_iam.budget_state_writer_policy(partition=Aws.PARTITION, region=Aws.REGION, account=Aws.ACCOUNT_ID))
        writer_doc.add_statements(
            iam.PolicyStatement(
                sid="OwnLogs",
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[_arn("logs", f"log-group:/aws/lambda/{shared_name(WRITER_FUNCTION_LOGICAL)}*")],
            )
        )
        self.writer_role = iam.Role(
            self,
            "BudgetStateWriterRole",
            role_name=shared_name(WRITER_FUNCTION_LOGICAL, "role"),
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Budget-state writer Lambda: writes /finplan/shared/financialplanning/config/budget-state only.",
            inline_policies={"budget-state-writer": writer_doc},
        )
        tag_role(self.writer_role, WRITER_FUNCTION_LOGICAL)
        self.writer_log_group = add_log_group(self, "BudgetStateWriterLogGroup", f"/aws/lambda/{shared_name(WRITER_FUNCTION_LOGICAL)}", WRITER_FUNCTION_LOGICAL)
        self.writer = lambda_.Function(
            self,
            "BudgetStateWriter",
            function_name=shared_name(WRITER_FUNCTION_LOGICAL),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_inline(budget_state_writer_source()),
            role=self.writer_role,
            log_group=self.writer_log_group,
            memory_size=128,
            timeout=Duration.seconds(30),
            environment={"FINPLAN_BUDGET_STATE_PARAMETER": contract_budget.STATE_PARAMETER},
            description="Sets the budget-state flag to enforced on an ACTUAL alert at the ceiling or an executed budget action (never clears it).",
        )
        tag_role(self.writer, WRITER_FUNCTION_LOGICAL)
        self.topic.add_subscription(subs.LambdaSubscription(self.writer))
        for child in self.node.find_all():  # the Lambda subscription lives under the function's scope
            if isinstance(child, sns.CfnSubscription) and child is not email:
                _metadata_role(child, "budget-alert-topic")

    # ------------------------------------------------------------ pipeline module hooks
    def register_enforced_role(self, role: iam.Role) -> None:
        """Add a role this stack creates to the budget action's deny list."""
        self.enforced_roles.append(role)
        self.action.node.add_dependency(role)

    def environment_boundary_arn(self, env: str) -> str:
        return self.boundary_policies[env].ref


@jsii.implements(cdk.IStableListProducer)
class _Producer:
    """``cdk.Lazy.list`` producer (jsii interface ``IStableListProducer``)."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def produce(self) -> list[str]:
        return list(self._fn())


def get_tooling_stack(app: cdk.App, shared: Mapping[str, Any]) -> ToolingStack:
    """The tooling stack (created with its store stack on first use)."""
    existing = app.node.try_find_child(TOOLING_CONSTRUCT_ID)
    if existing is not None:
        assert isinstance(existing, ToolingStack)
        return existing
    store = app.node.try_find_child(STORE_CONSTRUCT_ID) or StoreStack(app, STORE_CONSTRUCT_ID, shared=shared)
    tooling = ToolingStack(app, TOOLING_CONSTRUCT_ID, shared=shared)
    tooling.add_stack_dependency(store)  # the CLI stages the tooling template in the store
    return tooling


def add_to_app(app: cdk.App, shared: Mapping[str, Any], stages: Mapping[str, Any]) -> None:
    """``infra/app.py`` hook: the account-level tooling stack."""
    get_tooling_stack(app, shared)
