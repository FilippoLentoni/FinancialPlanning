"""The FinancialPlanning pipeline (task 10.1; spec platform-pipeline; contracts D6 pipeline standard).

Added to the account-level tooling stack (:mod:`infra.stacks.tooling`), so it is created only by
the authenticated bootstrap. CodePipeline **V2** plus CodeBuild, stages in the contract order:

1. **Source**: CodeConnections source on ``main``; the connection is the SSM parameter
   ``/finplan/shared/financialplanning/config/codeconnection-ref`` written by the bootstrap and
   resolved by CloudFormation (never a literal ARN). The commit ID is exported as
   ``#{SourceVariables.CommitId}``.
2. **Build**: one CodeBuild project runs ``scripts/build_stage.py``: the build-stage gates
   (unit, contract conformance, ownership, leak scan, copied-id, live-permission scan, boundary,
   pipeline-structure, cost and configuration checks), ``cdk synth`` once
   (``scripts/synth.py`` with :func:`infra.stacks.tooling.deployment_synthesizer`), asset
   publishing to the pipeline store, the artifact digest and a new ``release_id``. Its single
   output artifact ``BuildOutput`` is the only input of every later stage. With the pipeline
   variable ``rollback_to_release_id`` set to a recorded release, the build stage instead fetches
   that release's stored ``BuildOutput`` from the store (no rebuild) and marks it as a rollback.
3. **Beta**, 4. **Gamma**: CloudFormation deploy actions (one per environment stack, in
   dependency order) under the scoped deploy role ``finplan-shared-financialplanning-deploy-role-<env>``
   with the CloudFormation execution role ``...-deploy-role-<env>-exec``; then ``PublishManifest``
   (release manifest, ``current-release-id``, published outputs) and the environment tests
   (integration-beta, gamma), both by the per-environment stage project.
5. **Approval**: one Manual approval action and nothing else.
6. **Prod**: deploy, publish the manifest (with ``approved_by``/``approved_at`` read from the
   approval action), smoke tests on the synthetic smoke portfolio.

A failing action stops promotion (CodePipeline semantics), so a gamma failure stops before the
approval and prod keeps its release. Until the bootstrap's source-stage dry run has passed, the
inbound transition into Build is created **disabled** (``SourceDryRunPassed=false``), so a fresh
pipeline never runs past Source on its own.

Roles (all are in the budget action's deny list). Account-level, tagged ``environment=shared``
with ``finplan-shared-permission-boundary``: ``pipeline-role`` (CodePipeline) and
``pipeline-build-project-role`` (build). Per environment, tagged with that environment and bounded
by ``finplan-<env>-permission-boundary`` (contracts D13 "Per-environment pipeline roles", ENV-21;
matrix row ``pipeline-environment-roles-financialplanning``): ``deploy-role-<env>`` (deploy action
role), ``deploy-role-<env>-exec`` (CloudFormation execution role, scoped to
``finplan-<env>-financialplanning-*`` resources and to roles carrying
``finplan-<env>-permission-boundary``), and the stage role
``finplan-<env>-financialplanning-operator-pipeline-stage`` that publishes the manifest and runs
the tests (it matches the environment's configured operator principal, so the plan API admits it).

Each of the four CodeBuild projects (build plus one stage project per environment) logs to an
explicit ``/aws/codebuild/<project>`` log group with 30-day retention, deleted with the stack.

The check :func:`finplan_contracts.pipeline_check.check_pipeline_template` (ENV-09) runs on the
synthesized template in the unit suite and in the build stage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import Annotations, Aws, CfnCondition, CfnParameter, Duration, Fn, Tags
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import iam as contract_iam
from finplan_contracts import ssm as contract_ssm

from .tooling import (
    ASSET_PREFIX,
    PIPELINE_NAME,
    RELEASES_PREFIX,
    REPO,
    ToolingStack,
    add_log_group,
    get_tooling_stack,
    pipeline_store_bucket_name,
    shared_name,
    tag_role,
)

__all__ = [
    "BUILD_STAGE",
    "CONNECTION_PARAMETER",
    "DEFAULT_GITHUB_REPOSITORY",
    "ENV_SUITES",
    "NO_ROLLBACK",
    "ROLLBACK_VARIABLE",
    "SOURCE_STAGE",
    "STAGE_NAMES",
    "UV_VERSION",
    "PipelineResources",
    "add_pipeline",
    "add_to_app",
    "build_role_statements",
    "build_spec",
    "deploy_execution_statements",
    "deploy_role_name",
    "exec_role_name",
    "ordered_stacks",
    "stage_role_name",
    "stage_role_statements",
    "stage_spec",
    "template_path",
]

ROLLBACK_VARIABLE = "rollback_to_release_id"
NO_ROLLBACK = "none"
SOURCE_STAGE = "Source"
BUILD_STAGE = "Build"
STAGE_NAMES = (SOURCE_STAGE, BUILD_STAGE, "Beta", "Gamma", "Approval", "Prod")
ENV_SUITES = {"beta": "integration-beta", "gamma": "gamma", "prod": "smoke"}
CONNECTION_PARAMETER = contract_ssm.build(contract_ssm.SHARED, REPO, "config", "codeconnection-ref")
#: Public repository (contracts D11 records it); override with ``github_repository`` in config/shared.json.
DEFAULT_GITHUB_REPOSITORY = "FilippoLentoni/FinancialPlanning"
#: Pinned uv for CodeBuild (the local toolchain version).
UV_VERSION = "0.12.23"


# ===================================================================== names
def deploy_role_name(env: str) -> str:
    return shared_name("deploy-role", env)


def exec_role_name(env: str) -> str:
    return shared_name("deploy-role", f"{env}-exec")


def stage_role_name(env: str) -> str:
    """Matches the environment's operator principal pattern ``finplan-<env>-financialplanning-operator*``."""
    return contract_boundaries.resource_name(env, REPO, "operator", "pipeline-stage")


def _arn(service: str, resource: str, *, region: bool = True, account: bool = True) -> str:
    """ARN from CloudFormation pseudo parameters (no account literal ever appears in a file)."""
    partition = Aws.PARTITION
    reg = Aws.REGION if region else ""
    acct = Aws.ACCOUNT_ID if account else ""
    return f"arn:{partition}:{service}:{reg}:{acct}:{resource}"


# ===================================================================== role policies
def deploy_execution_statements(env: str, store_bucket_arn: str) -> list[iam.PolicyStatement]:
    """CloudFormation execution role of ``env``: only ``finplan-<env>-financialplanning-*`` resources.

    Roles it creates must carry ``finplan-<env>-permission-boundary`` (and its own shared boundary
    already forbids creating roles without an allowed boundary).
    """
    prefix = f"finplan-{env}-{REPO}-"
    boundary_arn = _arn("iam", f"policy/{contract_boundaries.boundary_name(env)}", region=False)
    role_arn = _arn("iam", f"role/{prefix}*", region=False)
    ssm_doc = contract_iam.ssm_access_policy(REPO, env, partition=Aws.PARTITION, region=Aws.REGION, account=Aws.ACCOUNT_ID)
    stmts = [
        iam.PolicyStatement(sid="EnvBuckets", actions=["s3:*"], resources=[_arn("s3", f"{prefix}*", region=False, account=False), _arn("s3", f"{prefix}*/*", region=False, account=False)]),
        iam.PolicyStatement(sid="ReadPublishedAssets", actions=["s3:GetObject", "s3:GetObjectVersion"], resources=[f"{store_bucket_arn}/{ASSET_PREFIX}*"]),
        iam.PolicyStatement(sid="EnvKeyCreate", actions=["kms:CreateKey", "kms:TagResource"], resources=["*"], conditions={"StringEquals": {"aws:RequestTag/environment": env}}),
        iam.PolicyStatement(sid="EnvKeyManage", actions=["kms:*"], resources=[_arn("kms", "key/*")], conditions={"StringEquals": {"aws:ResourceTag/environment": env}}),
        iam.PolicyStatement(sid="EnvKeyAlias", actions=["kms:CreateAlias", "kms:DeleteAlias", "kms:UpdateAlias"], resources=[_arn("kms", f"alias/{prefix}*")]),
        iam.PolicyStatement(sid="KmsList", actions=["kms:ListAliases", "kms:ListKeys"], resources=["*"]),
        iam.PolicyStatement(sid="EnvTables", actions=["dynamodb:*"], resources=[_arn("dynamodb", f"table/{prefix}*"), _arn("dynamodb", f"table/{prefix}*/*")]),
        iam.PolicyStatement(sid="EnvFunctions", actions=["lambda:*"], resources=[_arn("lambda", f"function:{prefix}*")]),
        iam.PolicyStatement(sid="EnvRolesCreateWithBoundary", actions=["iam:CreateRole", "iam:PutRolePermissionsBoundary"], resources=[role_arn], conditions={"StringEquals": {"iam:PermissionsBoundary": boundary_arn}}),
        iam.PolicyStatement(
            sid="EnvRolesManage",
            actions=[
                "iam:GetRole",
                "iam:GetRolePolicy",
                "iam:ListRolePolicies",
                "iam:ListAttachedRolePolicies",
                "iam:ListRoleTags",
                "iam:DeleteRole",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:AttachRolePolicy",
                "iam:DetachRolePolicy",
                "iam:UpdateRole",
                "iam:UpdateRoleDescription",
                "iam:UpdateAssumeRolePolicy",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:PassRole",
            ],
            resources=[role_arn],
        ),
        iam.PolicyStatement(sid="RestApis", actions=["apigateway:*"], resources=[_arn("apigateway", p, account=False) for p in ("/restapis", "/restapis/*", "/tags/*")]),
        iam.PolicyStatement(sid="EnvSchedules", actions=["scheduler:*"], resources=[_arn("scheduler", f"schedule/*/{prefix}*")]),
        iam.PolicyStatement(sid="EnvQueues", actions=["sqs:*"], resources=[_arn("sqs", f"{prefix}*")]),
        iam.PolicyStatement(sid="EnvAlarms", actions=["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource"], resources=[_arn("cloudwatch", f"alarm:{prefix}*")]),
        iam.PolicyStatement(sid="EnvRules", actions=["events:*"], resources=[_arn("events", f"rule/{prefix}*")]),
        iam.PolicyStatement(sid="EnvLogGroups", actions=["logs:*"], resources=[_arn("logs", f"log-group:/aws/lambda/{prefix}*"), _arn("logs", f"log-group:/aws/lambda/{prefix}*:*"), _arn("logs", f"log-group:{prefix}*"), _arn("logs", f"log-group:{prefix}*:*")]),
        iam.PolicyStatement(sid="LogGroupsDescribe", actions=["logs:DescribeLogGroups"], resources=["*"]),
    ]
    for st in ssm_doc["Statement"]:  # contract SSM rules: write own env segment only, read env + shared
        stmts.append(iam.PolicyStatement.from_json(st))
    return stmts


def stage_role_statements(env: str, store_bucket_arn: str) -> list[iam.PolicyStatement]:
    """Manifest publisher and test runner of ``env`` (pipeline writer of the own env segment)."""
    own = f"/finplan/{env}/{REPO}"
    param = lambda p: _arn("ssm", f"parameter{p}")  # noqa: E731
    return [
        iam.PolicyStatement(sid="WriteReleaseKeys", actions=["ssm:PutParameter", "ssm:AddTagsToResource"], resources=[param(f"{own}/release/*"), param(f"{own}/config/budget-enforced-role-names"), param(f"{own}/config/smoke-portfolio-id")]),
        iam.PolicyStatement(sid="ReadEnvAndShared", actions=list(contract_iam.SSM_READ_ACTIONS), resources=[param(f"/finplan/{env}"), param(f"/finplan/{env}/*"), param("/finplan/shared"), param("/finplan/shared/*")]),
        iam.PolicyStatement(sid="ReleaseLedger", actions=["s3:PutObject", "s3:GetObject"], resources=[f"{store_bucket_arn}/{RELEASES_PREFIX}*"]),
        iam.PolicyStatement(sid="ApprovalRecord", actions=["codepipeline:ListActionExecutions", "codepipeline:GetPipelineExecution"], resources=[_arn("codepipeline", PIPELINE_NAME)]),
        iam.PolicyStatement(sid="CallEnvApi", actions=["execute-api:Invoke"], resources=[_arn("execute-api", "*/*/*/v1/*")]),
    ]


def build_role_statements(store_bucket_arn: str) -> list[iam.PolicyStatement]:
    return [
        iam.PolicyStatement(sid="PublishAssetsAndReleases", actions=["s3:PutObject", "s3:GetObject"], resources=[f"{store_bucket_arn}/{ASSET_PREFIX}*", f"{store_bucket_arn}/{RELEASES_PREFIX}*"]),
        iam.PolicyStatement(sid="ListStore", actions=["s3:ListBucket"], resources=[store_bucket_arn], conditions={"StringLike": {"s3:prefix": [f"{ASSET_PREFIX}*", f"{RELEASES_PREFIX}*"]}}),
    ]


# ===================================================================== build specs
def _install() -> dict[str, Any]:
    return {"runtime-versions": {"python": "3.12", "nodejs": "22"}, "commands": [f'python3 -m pip install --quiet "uv=={UV_VERSION}"', "uv --version"]}


def build_spec() -> dict[str, Any]:
    return {
        "version": "0.2",
        # FINPLAN_RELEASE_BUILD=1: any synth in this project is a release synth, which refuses a
        # function without a dependency bundle (build_stage.py builds the arm64 bundles from uv.lock
        # natively on this ARM image before it synthesizes; docs/pipeline.md "Source-only Lambda bundle").
        "env": {"shell": "bash", "variables": {"SOURCE_DATE_EPOCH": "315532800", "UV_LINK_MODE": "copy", "CDK_DISABLE_VERSION_CHECK": "1", "FINPLAN_RELEASE_BUILD": "1"}},
        "phases": {
            "install": _install(),
            "build": {
                "commands": [
                    "uv sync --locked",
                    'uv run python scripts/build_stage.py --out build-output --source-commit "$SOURCE_COMMIT" --rollback-to "$ROLLBACK_TO_RELEASE_ID" --store "$FINPLAN_PIPELINE_STORE"',
                ]
            },
        },
        "artifacts": {"base-directory": "build-output", "files": ["**/*"]},
        "cache": {"paths": ["/root/.cache/uv/**/*", "/root/.npm/**/*"]},
    }


def stage_spec() -> dict[str, Any]:
    """Post-deploy actions: run from the BuildOutput artifact only (never the source, never synth)."""
    return {
        "version": "0.2",
        "env": {"shell": "bash", "variables": {"UV_LINK_MODE": "copy"}},
        "phases": {
            "install": _install(),
            "build": {
                "commands": [
                    "uv sync --locked",
                    'uv run python scripts/stage_runner.py "$FINPLAN_STAGE_ACTION" --env "$FINPLAN_ENV" --release-info release-info.json --pipeline-execution-id "$PIPELINE_EXECUTION_ID" --store "$FINPLAN_PIPELINE_STORE"',
                ]
            },
        },
        "cache": {"paths": ["/root/.cache/uv/**/*"]},
    }


# ===================================================================== stage stacks
def _stage_of(ctx: Any) -> cdk.Stage:
    stage = cdk.Stage.of(ctx.storage)
    if stage is None:  # pragma: no cover - storage always lives in a Stage
        raise ValueError("environment stacks must live in a cdk.Stage")
    return stage


def ordered_stacks(stage: cdk.Stage) -> list[cdk.Stack]:
    """The stage's stacks in dependency order (deploy order)."""
    stacks = [c for c in stage.node.children if isinstance(c, cdk.Stack)]
    ordered: list[cdk.Stack] = []
    # creation order (infra/app.py creates storage, metadata, then the optional modules, and a
    # stack can only reference stacks created before it), refined by explicit dependencies
    pending = list(stacks)
    while pending:
        progressed = False
        for st in list(pending):
            deps = [d for d in st.dependencies if d in stacks]
            if all(d in ordered for d in deps):
                ordered.append(st)
                pending.remove(st)
                progressed = True
        if not progressed:  # pragma: no cover - CDK rejects cycles earlier
            raise ValueError("cyclic stack dependencies in stage " + stage.node.id)
    return ordered


def template_path(stage: cdk.Stage, stack: cdk.Stack) -> str:
    """Path of the stack template inside BuildOutput (``cdk.out/assembly-<Stage>/<file>``)."""
    return f"cdk.out/{stage.artifact_id}/{stack.template_file}"


# ===================================================================== construction
class PipelineResources:
    def __init__(self, tooling: ToolingStack) -> None:
        self.tooling = tooling
        self.pipeline: codepipeline.Pipeline | None = None
        self.store: s3.IBucket | None = None
        self.roles: dict[str, iam.Role] = {}
        self.projects: dict[str, codebuild.PipelineProject] = {}


def _role(scope: ToolingStack, cid: str, name: str, logical: str, principal: iam.IPrincipal, description: str, statements: list[iam.PolicyStatement] | None = None) -> iam.Role:
    role = iam.Role(scope, cid, role_name=name, assumed_by=principal, description=description)
    for st in statements or []:
        role.add_to_principal_policy(st)
    tag_role(role, logical)
    scope.register_enforced_role(role)
    return role


def _scope_to_environment(scope: ToolingStack, role: iam.Role, env: str) -> None:
    """A per-environment pipeline role: ``environment=<env>`` tag and that environment's boundary (ENV-21).

    The tooling stack applies the shared tag and boundary to everything it declares; these
    role-level settings override both (higher tag priority; the role's own boundary aspect runs
    after the stack's).
    """
    Tags.of(role).add("environment", env, priority=200)
    boundary = iam.ManagedPolicy.from_managed_policy_arn(role, "EnvBoundary", scope.environment_boundary_arn(env))
    iam.PermissionsBoundary.of(role).apply(boundary)


def _project_logging(scope: ToolingStack, cid: str, project_name: str) -> codebuild.LoggingOptions:
    """CloudWatch logging of a CodeBuild project into its explicit ``/aws/codebuild/<project>`` group (30 days, DESTROY).

    The name is the CodeBuild default, so the project role's log grant and the environment
    boundaries see the same ARN as before; the group is attributed to the pipeline row by its
    ``pipeline-build-project`` logical role.
    """
    group = add_log_group(scope, cid, f"/aws/codebuild/{project_name}", "pipeline-build-project")
    return codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=group))


def add_pipeline(tooling: ToolingStack, stages: Mapping[str, Any], shared: Mapping[str, Any]) -> PipelineResources:
    res = PipelineResources(tooling)
    st = tooling
    github = str(shared.get("github_repository") or DEFAULT_GITHUB_REPOSITORY)
    owner, repo_name = github.split("/", 1)

    dry_run_passed = CfnParameter(st, "SourceDryRunPassed", type="String", default="false", allowed_values=["false", "true"], description="true once the bootstrap's source-stage dry run fetched main; until then the transition into Build is disabled.")
    dry_run_cond = CfnCondition(st, "SourceDryRunPassedCondition", expression=Fn.condition_equals(dry_run_passed.value_as_string, "true"))

    # ------------------------------------------------------------ store (StoreStack, deployed first)
    store = s3.Bucket.from_bucket_name(st, "Store", pipeline_store_bucket_name())
    res.store = store

    # ------------------------------------------------------------ roles
    pipeline_role = _role(st, "PipelineRole", shared_name("pipeline", "role"), "pipeline-role", iam.ServicePrincipal("codepipeline.amazonaws.com"), "CodePipeline service role of the platform pipeline")
    build_role = _role(st, "BuildRole", shared_name("pipeline-build-project", "role"), "pipeline-role", iam.ServicePrincipal("codebuild.amazonaws.com"), "Build stage: gates, synth, asset publishing, release packaging", build_role_statements(store.bucket_arn))
    res.roles.update(pipeline=pipeline_role, build=build_role)

    # ------------------------------------------------------------ projects
    env_common = {"FINPLAN_PIPELINE_STORE": codebuild.BuildEnvironmentVariable(value=store.bucket_name)}
    build_env = codebuild.BuildEnvironment(build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0, compute_type=codebuild.ComputeType.SMALL, privileged=False)
    build_project_name = shared_name("pipeline-build-project")
    build_project = codebuild.PipelineProject(
        st,
        "BuildProject",
        project_name=build_project_name,
        role=build_role,
        environment=build_env,
        environment_variables=env_common,
        build_spec=codebuild.BuildSpec.from_object(build_spec()),
        timeout=Duration.minutes(30),
        cache=codebuild.Cache.local(codebuild.LocalCacheMode.CUSTOM),
        logging=_project_logging(st, "BuildProjectLogGroup", build_project_name),
        description="Build stage: gates, cdk synth, assets, digest, release_id",
    )
    tag_role(build_project, "pipeline-build-project")
    res.projects["build"] = build_project

    # ------------------------------------------------------------ pipeline
    source_output = codepipeline.Artifact("SourceOutput")
    build_output = codepipeline.Artifact("BuildOutput")
    connection_arn = ssm.StringParameter.value_for_string_parameter(st, CONNECTION_PARAMETER)
    source = actions.CodeStarConnectionsSourceAction(
        action_name="Source",
        owner=owner,
        repo=repo_name,
        branch="main",
        connection_arn=connection_arn,
        output=source_output,
        trigger_on_push=True,
        variables_namespace="SourceVariables",
    )
    build = actions.CodeBuildAction(
        action_name="BuildAndTest",
        project=build_project,
        input=source_output,
        outputs=[build_output],
        type=actions.CodeBuildActionType.BUILD,
        environment_variables={
            "SOURCE_COMMIT": codebuild.BuildEnvironmentVariable(value=source.variables.commit_id),
            "ROLLBACK_TO_RELEASE_ID": codebuild.BuildEnvironmentVariable(value=f"#{{variables.{ROLLBACK_VARIABLE}}}"),
        },
    )
    pipeline = codepipeline.Pipeline(
        st,
        "Pipeline",
        pipeline_name=PIPELINE_NAME,
        pipeline_type=codepipeline.PipelineType.V2,
        artifact_bucket=store,
        role=pipeline_role,
        cross_account_keys=False,
        restart_execution_on_update=False,
        use_pipeline_role_for_actions=True,
        variables=[codepipeline.Variable(variable_name=ROLLBACK_VARIABLE, default_value=NO_ROLLBACK, description="Set to a recorded release_id to redeploy its stored artifacts without rebuilding (contracts D6).")],
        stages=[
            codepipeline.StageProps(stage_name=SOURCE_STAGE, actions=[source]),
            codepipeline.StageProps(stage_name=BUILD_STAGE, actions=[build]),
        ],
    )
    tag_role(pipeline, "pipeline")
    res.pipeline = pipeline

    for env in ("beta", "gamma", "prod"):
        if env == "prod":
            pipeline.add_stage(
                stage_name="Approval",
                actions=[actions.ManualApprovalAction(action_name="ApproveProd", additional_information="Approve promotion of this release to prod after the gamma tests passed. The approver and time are recorded in the prod release manifest.")],
            )
        _add_env_stage(res, env, stages[env], build_output, env_common, build_env)

    cfn = pipeline.node.default_child
    assert isinstance(cfn, codepipeline.CfnPipeline)
    cfn.add_property_override(
        "DisableInboundStageTransitions",
        Fn.condition_if(dry_run_cond.logical_id, Aws.NO_VALUE, [{"StageName": BUILD_STAGE, "Reason": "finplan bootstrap: the source-stage dry run has not passed yet"}]),
    )
    return res


def _add_env_stage(res: PipelineResources, env: str, ctx: Any, build_output: codepipeline.Artifact, env_common: dict[str, codebuild.BuildEnvironmentVariable], build_env: codebuild.BuildEnvironment) -> None:
    st = res.tooling
    cap = env.capitalize()
    assert res.store is not None and res.pipeline is not None
    deploy_role = _role(st, f"DeployRole{cap}", deploy_role_name(env), "deploy-role", iam.ArnPrincipal(res.roles["pipeline"].role_arn), f"Scoped {env} deploy action role (CodePipeline assumes it)")
    exec_role = _role(st, f"DeployExecRole{cap}", exec_role_name(env), "deploy-role", iam.ServicePrincipal("cloudformation.amazonaws.com"), f"CloudFormation execution role for the {env} platform stacks", deploy_execution_statements(env, res.store.bucket_arn))
    stage_role = _role(st, f"StageRole{cap}", stage_role_name(env), "pipeline-role", iam.ServicePrincipal("codebuild.amazonaws.com"), f"{env} manifest publisher and test runner", stage_role_statements(env, res.store.bucket_arn))
    for role in (deploy_role, exec_role, stage_role):
        _scope_to_environment(st, role, env)
    res.roles.update({f"deploy-{env}": deploy_role, f"exec-{env}": exec_role, f"stage-{env}": stage_role})

    project_name = shared_name("pipeline-build-project", f"{env}-stage")
    project = codebuild.PipelineProject(
        st,
        f"StageProject{cap}",
        project_name=project_name,
        role=stage_role,
        environment=build_env,
        environment_variables={**env_common, "FINPLAN_ENV": codebuild.BuildEnvironmentVariable(value=env)},
        build_spec=codebuild.BuildSpec.from_object(stage_spec()),
        timeout=Duration.minutes(30),
        cache=codebuild.Cache.local(codebuild.LocalCacheMode.CUSTOM),
        logging=_project_logging(st, f"StageProject{cap}LogGroup", project_name),
        description=f"{env}: publish the release manifest and run the {ENV_SUITES[env]} suite",
    )
    tag_role(project, "pipeline-build-project")
    res.projects[env] = project

    stage = _stage_of(ctx)
    stage_actions: list[codepipeline.IAction] = []
    order = 0
    for order, stack in enumerate(ordered_stacks(stage), start=1):
        part = stack.node.id
        stage_actions.append(
            actions.CloudFormationCreateUpdateStackAction(
                action_name=f"Deploy{part}",
                stack_name=stack.stack_name,
                template_path=build_output.at_path(template_path(stage, stack)),
                admin_permissions=False,
                role=deploy_role,
                deployment_role=exec_role,
                cfn_capabilities=[cdk.CfnCapabilities.NAMED_IAM, cdk.CfnCapabilities.AUTO_EXPAND],
                replace_on_failure=False,
                run_order=order,
            )
        )
    execution_id = codebuild.BuildEnvironmentVariable(value="#{codepipeline.PipelineExecutionId}")
    stage_actions.append(
        actions.CodeBuildAction(
            action_name="PublishManifest",
            project=project,
            input=build_output,
            type=actions.CodeBuildActionType.BUILD,
            run_order=order + 1,
            environment_variables={"FINPLAN_STAGE_ACTION": codebuild.BuildEnvironmentVariable(value="publish"), "PIPELINE_EXECUTION_ID": execution_id},
        )
    )
    suite = ENV_SUITES[env]
    stage_actions.append(
        actions.CodeBuildAction(
            action_name="".join(p.capitalize() for p in suite.split("-")) + "Tests",
            project=project,
            input=build_output,
            type=actions.CodeBuildActionType.TEST,
            run_order=order + 2,
            environment_variables={"FINPLAN_STAGE_ACTION": codebuild.BuildEnvironmentVariable(value="tests"), "PIPELINE_EXECUTION_ID": execution_id},
        )
    )
    res.pipeline.add_stage(stage_name=cap, actions=stage_actions)


# ===================================================================== app hook
def add_to_app(app: cdk.App, shared: Mapping[str, Any], stages: Mapping[str, Any]) -> None:
    """``infra/app.py`` hook. The pipeline needs all three environment stages."""
    tooling = get_tooling_stack(app, shared)
    missing = [e for e in ("beta", "gamma", "prod") if e not in stages]
    if missing:
        Annotations.of(tooling).add_warning_v2("finplan:pipeline-skipped", f"pipeline not synthesized: environments {missing} are not selected (-c envs=...)")
        return
    add_pipeline(tooling, stages, shared)
