"""Plan lifecycle API stack, one per environment (task 4.1, 4.8; API-01; design P1, P4).

* **REST API** (API Gateway v1, regional) with **IAM (SigV4) authorization on every method**
  and a **resource policy** generated from :data:`finplan_platform.handlers.api.ROUTES`, the
  same table the handler's per-route check uses:

  - platform principals of this environment (``finplan-<env>-financialplanning-*``: the
    platform's own roles, the website-path role and the operator role) may call every route;
  - each consumer role class (FinanceLambdasTool ``reader``/``submitter``/``plan-writer``,
    FinanceModel job and job-API roles), identified by the role-name pattern in
    ``config/<env>.json`` ``consumer_principals``, is allowed exactly its routes and explicitly
    denied every other route (so a broad identity policy on the consumer side cannot widen it);
  - every principal outside those patterns, including every other environment's roles, is
    explicitly denied (single-account isolation, contracts D5).

  Gateway responses (missing/invalid credentials, access denied, throttling, defaults) return
  the contract error envelope with ``$context.requestId`` as ``correlation_id``.
* **plan-api Lambda** (Python 3.12, arm64, no VPC, no provisioned or reserved concurrency) with
  the platform role ``finplan-<env>-financialplanning-plan-api-handler-role`` (the bucket and
  table policies admit only ``finplan-<env>-financialplanning-*``). It also serves the routes
  the router delegates in-process (ingestion, staged-output acceptance, Excel), so it gets the
  storage, metadata and SSM read grants those need.
* **Output** ``/finplan/<env>/financialplanning/api/plan-endpoint`` (the invoke URL); nothing
  else about the API is published.

Hooks: ``add_to_stage`` stores ``ctx.extras["api"] = {"stack", "rest_api", "url", "function",
"role"}`` so the ingestion module can publish ``api/ingestion-endpoint`` from the same URL and
add grants to the role.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct
from finplan_platform.core.config import BUCKET_ROLES, EnvConfig
from finplan_platform.core.repository import TABLES, table_name
from finplan_platform.core.upgrade import current_version
from finplan_platform.handlers.api import CONSUMER_CONFIG_KEYS, FINANCEMODEL_CLASSES, ROUTES, TOOL_CLASSES, Route, automation_role_patterns, route_allowed

from .common import PlatformStack, StageContext, lambda_code, platform_role, resource_name, role_arn_pattern, ssm_name, tag_role

__all__ = ["CONSUMER_CLASSES", "STAGE_NAME", "ApiStack", "add_to_stage", "resource_policy_document", "route_resource"]

STAGE_NAME = "live"
#: consumer role classes that get a scoped allow + deny-everything-else pair
CONSUMER_CLASSES = TOOL_CLASSES + FINANCEMODEL_CLASSES
INVOKE = "execute-api:Invoke"


def route_resource(route: Route) -> str:
    """Resource-policy form of a route: ``execute-api:/<any stage>/<METHOD>/<path with * for parameters>``."""
    return f"execute-api:/*/{route.resource_pattern}"


def _sid(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text.title())


def compact_route_resources(resources: list[str]) -> list[str]:
    """Drop redundant descendants of existing terminal wildcards.

    IAM '*' already spans '/'. A pattern ending in '*' covers every resource
    whose literal pattern starts with that prefix, including nested routes.
    Use the same reduced union for Allow and NotResource so permissions stay
    identical. Explicit descendant denials remain separate statements.
    """
    unique = sorted(set(resources))
    return [resource for resource in unique if not any(parent != resource and parent.endswith("*") and resource.startswith(parent[:-1]) for parent in unique)]


def resource_policy_document(cfg: EnvConfig, arn_for: Callable[[str], str]) -> dict[str, Any]:
    """The API resource policy as a plain IAM document (pure; also used by the policy simulation).

    ``arn_for(role_name_pattern)`` renders a role ARN pattern (CDK: account/partition tokens;
    tests: placeholders).
    """
    env = cfg.env
    platform = arn_for(f"finplan-{env}-financialplanning-*")
    everyone = {"AWS": "*"}
    statements: list[dict[str, Any]] = [
        {
            "Sid": "AllowPlatformPrincipalsAllRoutes",
            "Effect": "Allow",
            "Principal": everyone,
            "Action": INVOKE,
            "Resource": "execute-api:/*",
            "Condition": {"ArnLike": {"aws:PrincipalArn": platform}},
        }
    ]
    allowed_patterns = [platform]
    for cls in CONSUMER_CLASSES:
        pattern = arn_for(cfg.principal_pattern(CONSUMER_CONFIG_KEYS[cls]))
        allowed_patterns.append(pattern)
        resources = compact_route_resources([route_resource(r) for r in ROUTES if route_allowed(r, cls)])
        cond = {"ArnLike": {"aws:PrincipalArn": pattern}}
        statements.append({"Sid": f"Allow{_sid(cls)}Routes", "Effect": "Allow", "Principal": everyone, "Action": INVOKE, "Resource": resources[0] if len(resources) == 1 else resources, "Condition": cond})
        statements.append({"Sid": f"Deny{_sid(cls)}OtherRoutes", "Effect": "Deny", "Principal": everyone, "Action": INVOKE, "NotResource": resources[0] if len(resources) == 1 else resources, "Condition": cond})
        # IAM '*' spans '/': an allowed parent-ID read can otherwise include known
        # descendant routes. Deny only disallowed descendants, never their parents.
        allowed_routes = [r for r in ROUTES if route_allowed(r, cls)]
        descendants = sorted({route_resource(r) for r in ROUTES if not route_allowed(r, cls) and any(r.method == parent.method and r.path.startswith(parent.path + "/") for parent in allowed_routes)})
        if descendants:
            statements.append({"Sid": f"Deny{_sid(cls)}ExcludedDescendants", "Effect": "Deny", "Principal": everyone, "Action": INVOKE, "Resource": descendants[0] if len(descendants) == 1 else descendants, "Condition": cond})
    # The website belongs to the broad platform pattern but does not administer paper state.
    operator_routes = [route_resource(r) for r in ROUTES if r.operator_only]
    if operator_routes:
        statements.append({"Sid": "DenyWebsitePaperStateWrites", "Effect": "Deny", "Principal": everyone, "Action": INVOKE, "Resource": operator_routes[0] if len(operator_routes) == 1 else operator_routes, "Condition": {"ArnLike": {"aws:PrincipalArn": arn_for(cfg.principal_pattern("website_backend"))}}})
    # daily-recommendation-trigger (DLY-06): the platform automation roles never publish
    (publish,) = {route_resource(r) for r in ROUTES if r.operation == "publish_plan_version"}
    statements.append(
        {
            "Sid": "DenyAutomationPublish",
            "Effect": "Deny",
            "Principal": everyone,
            "Action": INVOKE,
            "Resource": publish,
            "Condition": {"ArnLike": {"aws:PrincipalArn": [arn_for(p) for p in automation_role_patterns(env)]}},
        }
    )
    statements.append(
        {
            "Sid": "DenyPrincipalsOutsideThisEnvironment",
            "Effect": "Deny",
            "Principal": everyone,
            "Action": INVOKE,
            "Resource": "execute-api:/*",
            "Condition": {"ArnNotLike": {"aws:PrincipalArn": allowed_patterns}},
        }
    )
    return {"Version": "2012-10-17", "Statement": statements}


def _envelope_template(code: str, message: str, retryable: bool = False, details: dict[str, Any] | None = None) -> str:
    body = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details or {},
        "correlation_id": "$context.requestId",
        "contract_version": current_version(),
    }
    return json.dumps(body, separators=(",", ":"))


#: API Gateway's own rejections, as contract envelopes (API-01 "rejected with UNAUTHORIZED or FORBIDDEN").
GATEWAY_RESPONSES: dict[str, tuple[str, str, bool, dict[str, Any] | None]] = {
    "MISSING_AUTHENTICATION_TOKEN": ("UNAUTHORIZED", "the request is not signed with valid IAM credentials, or the route does not exist", False, None),
    "UNAUTHORIZED": ("UNAUTHORIZED", "the caller could not be authenticated", False, None),
    "INVALID_SIGNATURE": ("UNAUTHORIZED", "the request signature is not valid", False, None),
    "EXPIRED_TOKEN": ("UNAUTHORIZED", "the security token has expired", False, None),
    "ACCESS_DENIED": ("FORBIDDEN", "the caller is not allowed to call this route", False, None),
    "THROTTLED": ("RATE_LIMITED", "the API throttled the request; retry later", True, None),
    "DEFAULT_4XX": ("VALIDATION_FAILED", "the request was rejected by the API gateway", False, {"pointer": ""}),
    "DEFAULT_5XX": ("INTERNAL", "internal error", False, None),
}


class ApiStack(PlatformStack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, storage: Any, metadata: Any, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, cfg=cfg, description=f"FinancialPlanning plan lifecycle API ({cfg.env})", **kwargs)
        env = cfg.env

        # ------------------------------------------------------------ handler role and grants
        self.role = platform_role(
            self,
            "PlanApiHandlerRole",
            cfg=cfg,
            logical="plan-api-handler",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="plan-api Lambda: plan lifecycle operations plus in-process ingestion, staged-output and Excel routes",
        )
        storage.grant_artifacts(self.role, write=BUCKET_ROLES, tag=("snapshots",))  # write grants include the reads
        # Metadata access, written compactly by table-name pattern (the per-table grant helper
        # overflows the role's inline policy size with nine tables plus indexes). Same rights as
        # ``metadata.grant_metadata(write=...)``: reads, PutItem, ConditionCheckItem, UpdateItem
        # except on the append-only audit table, and the environment key.
        def table_arn(name: str) -> str:
            return self.format_arn(service="dynamodb", resource="table", resource_name=name)

        self.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="MetadataReadAndInsert",
                actions=["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable", "dynamodb:ConditionCheckItem", "dynamodb:PutItem"],
                resources=[table_arn(f"finplan-{env}-financialplanning-*")],
            )
        )
        self.role.add_to_principal_policy(
            iam.PolicyStatement(sid="MetadataConditionalUpdate", actions=["dynamodb:UpdateItem"], resources=[table_arn(table_name(env, t)) for t in TABLES if t != "audit_event"])
        )
        metadata.storage.key.grant_encrypt_decrypt(self.role)
        self.node.add_dependency(metadata)  # tables exist before the API that uses them
        def ssm_arn(path: str) -> str:
            return self.format_arn(service="ssm", resource="parameter", resource_name=path.lstrip("/"))

        self.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadOwnAndSharedConfig",
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    ssm_arn(f"/finplan/{env}/financialplanning/*"),
                    ssm_arn("/finplan/shared/financialplanning/config/budget-state"),
                    ssm_arn(f"/finplan/{env}/financemodel/model/registry-ref"),
                ],
            )
        )
        # Staged-output acceptance (STG-03) verifies run lineage through FinanceModel's registry
        # route (registry-ref = <job-endpoint>/v1/registry), SigV4-signed with this role. FinanceModel's
        # API resource policy admits this role; this is the matching identity grant, read route only.
        self.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="FinanceModelRegistryLineage",
                actions=["execute-api:Invoke"],
                resources=[self.format_arn(service="execute-api", resource="*", resource_name="*/GET/v1/registry/lineage/*")],
            )
        )

        # ------------------------------------------------------------ function
        fn_name = resource_name(env, "plan-api")
        self.log_group = logs.LogGroup(
            self,
            "PlanApiLogs",
            log_group_name=f"/aws/lambda/{fn_name}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.RETAIN if env == "prod" else RemovalPolicy.DESTROY,
        )
        tag_role(self.log_group, "plan-api-handler")
        # The explicit role gets no AWSLambdaBasicExecutionRole and CDK grants nothing for an
        # explicit ``log_group``: without this the function could not create a log stream and its
        # log group stayed empty (docs/pipeline.md "Source-only Lambda bundle", logging finding).
        self.role.add_to_principal_policy(
            iam.PolicyStatement(sid="OwnLogStreams", actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[self.log_group.log_group_arn])
        )
        self.function = lambda_.Function(
            self,
            "PlanApiFunction",
            function_name=fn_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="finplan_platform.handlers.api.handler",
            code=lambda_code("plan-api"),
            role=self.role,
            memory_size=256,
            timeout=Duration.seconds(29),
            log_group=self.log_group,
            environment={
                "FINPLAN_ENV": env,
                "FINPLAN_CONFIG_DIR": "/var/task/config",
                **storage.bucket_env(),
                **metadata.table_env(),
            },
            description="FinancialPlanning plan lifecycle API handler",
        )
        tag_role(self.function, "plan-api-handler")

        # ------------------------------------------------------------ REST API
        self.rest_api = apigw.RestApi(
            self,
            "PlanApi",
            rest_api_name=resource_name(env, "plan-api"),
            description=f"FinancialPlanning plan lifecycle API ({env}); IAM (SigV4) auth on every method",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            cloud_watch_role=False,
            deploy=True,
            deploy_options=apigw.StageOptions(stage_name=STAGE_NAME, throttling_rate_limit=10, throttling_burst_limit=20, metrics_enabled=False, tracing_enabled=False),
            default_method_options=apigw.MethodOptions(authorization_type=apigw.AuthorizationType.IAM),
            policy=iam.PolicyDocument.from_json(resource_policy_document(cfg, role_arn_pattern)),
        )
        tag_role(self.rest_api, "plan-api")
        integration = apigw.LambdaIntegration(self.function, proxy=True, allow_test_invoke=False)
        resources: dict[str, apigw.IResource] = {"": self.rest_api.root}
        for route in ROUTES:
            res = self._resource(resources, route.path)
            res.add_method(route.method, integration, authorization_type=apigw.AuthorizationType.IAM)
        for rtype, (code, message, retryable, details) in GATEWAY_RESPONSES.items():
            self.rest_api.add_gateway_response(
                f"Gw{_sid(rtype.lower())}",
                type=getattr(apigw.ResponseType, rtype.replace("_4XX", "_4_XX").replace("_5XX", "_5_XX")),
                templates={"application/json": _envelope_template(code, message, retryable, details)},
                response_headers={"Content-Type": "'application/json'"},
            )

        # ------------------------------------------------------------ published reference
        self.url = self.rest_api.url
        self.endpoint_param = ssm.StringParameter(
            self,
            "PlanEndpoint",
            parameter_name=ssm_name(env, "api", "plan-endpoint"),
            string_value=self.rest_api.url,
            description="Plan lifecycle API invoke URL (IAM/SigV4); consumers resolve it at deploy or run time",
        )
        tag_role(self.endpoint_param, "plan-api")

    @staticmethod
    def _resource(cache: dict[str, apigw.IResource], path: str) -> apigw.IResource:
        parts = [p for p in path.split("/") if p]
        cur = ""
        for part in parts:
            parent = cache[cur]
            cur = f"{cur}/{part}"
            if cur not in cache:
                cache[cur] = parent.add_resource(part)
        return cache[cur]


def add_to_stage(stage: cdk.Stage, ctx: StageContext) -> None:
    env = ctx.cfg.env
    stack = ApiStack(stage, "Api", cfg=ctx.cfg, storage=ctx.storage, metadata=ctx.metadata, stack_name=f"finplan-{env}-financialplanning-api")
    ctx.extras["api"] = {"stack": stack, "rest_api": stack.rest_api, "url": stack.url, "function": stack.function, "role": stack.role}
