"""Plan lifecycle API Lambda adapter and router (API-owned; tasks 4.1-4.8, 5.1-5.4).

One router for every ``/v1`` route of the plan API (design P4). The handler is thin:

1. **Route** the API Gateway REST proxy event (``httpMethod`` + ``resource`` template, or the
   raw ``path`` for the local client) to a :class:`Route`. Route targets are imported lazily,
   so a module that is missing or broken fails only its own routes (``DEPENDENCY_UNAVAILABLE``),
   never the router.
2. **Authenticate**: the principal is the IAM identity API Gateway verified (SigV4;
   ``requestContext.identity.userArn``), normalized from an assumed-role session ARN to the
   role ARN, so idempotency scopes survive container and session changes. No identity ->
   ``UNAUTHORIZED``.
3. **Authorize per route** (defence in depth behind the API resource policy built from the
   same :data:`ROUTES` table in ``infra/stacks/api.py``): the principal's role class comes from
   the environment configuration's ``consumer_principals`` name patterns; platform roles
   (``finplan-<env>-financialplanning-*``, including the website-path and operator roles) may call
   every route; FinanceLambdasTool ``reader`` gets the GET routes, ``submitter`` adds
   ``POST /v1/ingestions``, ``plan-writer`` adds version create, validate and publish;
   FinanceModel's job role gets ``GET /v1/snapshots/*`` only and its job-API role also
   ``GET /v1/staged-outputs/*``. Anything else -> ``FORBIDDEN``.
4. **Contract version**: ``X-Finplan-Contract-Version`` header or body ``contract_version``;
   an unserved major -> ``UNSUPPORTED_CONTRACT_VERSION`` listing the served majors.
5. **Validate the request** (inside each operation, through the pinned contract validators),
   **run the operation**, **validate the response** against its contract (or platform) schema:
   a non-conformant response becomes ``INTERNAL`` and is never sent.
6. **Errors** are contract envelopes (``code``, ``message``, ``retryable``, ``details``,
   ``correlation_id``, ``contract_version``); no stack trace and no storage location is ever
   returned. The correlation ID comes from ``X-Correlation-Id`` (if it matches the contract
   pattern) or is minted, and is echoed in the ``X-Correlation-Id`` response header.

Transport headers (outside the hashed request body, so idempotency hashes stay stable):
``X-Correlation-Id``, ``X-Finplan-Contract-Version`` and ``X-Finplan-Caller`` (the contract
``core/v1/caller.json`` on-behalf-of block, recorded in audit events, never granting authority).

Routes owned by other modules are registered here and delegated by name (:data:`DELEGATES`):
ingestion (``POST /v1/ingestions`` -> ``core.ingestion.run_ingestion``), staged-output acceptance
and outcome read (``core.staging``), Excel export/import (``excel``). Their parameters are bound
by name (``ctx``, ``svc``/``services``, ``repo``, ``store``/``artifacts``, ``cfg``/``config``,
``request``/``body``, ``query`` and path parameters such as ``plan_id`` or ``run_id``).
"""

from __future__ import annotations

import base64
import fnmatch
import importlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from finplan_contracts.canonical import CanonicalizationError, loads_strict

from ..core.clock import Clock, SystemClock
from ..core.context import CORRELATION_ID_RE, Caller, OperationContext, new_correlation_id
from ..core.contract_io import (
    CREATE_PLAN_RESPONSE,
    CREATE_PORTFOLIO_RESPONSE,
    GET_STAGED_OUTPUT_RESPONSE,
    OBSERVATIONS_RESPONSE,
    RECORD_EXECUTION_RESPONSE,
    REFRESH_MARKET_DATA_RESPONSE,
    create_version_response_schema,
    require_valid_response,
    validate_document,
)
from ..core.errors import PlatformError
from ..core.ids import IdFactory
from ..core.services import Services
from ..core.snapshot_reads import SNAPSHOT_RESPONSE
from ..core.upgrade import CONTRACT_VERSION_HEADER, check_declared_version, current_version

log = logging.getLogger(__name__)

__all__ = [
    "CONSUMER_CONFIG_KEYS",
    "DELEGATES",
    "FINANCEMODEL_CLASSES",
    "FULL_ACCESS_CLASSES",
    "ROLE_CLASSES",
    "ROUTES",
    "TOOL_CLASSES",
    "ApiApp",
    "LocalClient",
    "Route",
    "handler",
    "http_status",
    "match_route",
    "normalize_principal",
    "resolve_role_class",
    "role_name_of",
    "route_allowed",
    "routes_for_class",
]

# ===================================================================== role classes
TOOL_CLASSES = ("reader", "submitter", "plan-writer")
FINANCEMODEL_CLASSES = ("financemodel-job", "financemodel-job-api")
FULL_ACCESS_CLASSES = ("platform", "website", "operator")
#: Platform automation roles (the daily recommendation trigger and the daily ingestion schedule):
#: snapshot and plan reads, staged-output acceptance and the trigger outcome read only. They are
#: never granted the publish route (daily-recommendation-trigger, DLY-06).
AUTOMATION_CLASS = "automation"
AUTOMATION_ROLE_SUFFIXES = ("daily-trigger", "daily-ingest-schedule")


def automation_role_patterns(env: str) -> tuple[str, ...]:
    return tuple(f"finplan-{env}-financialplanning-{s}*" for s in AUTOMATION_ROLE_SUFFIXES)
ROLE_CLASSES = FULL_ACCESS_CLASSES + TOOL_CLASSES + FINANCEMODEL_CLASSES
#: role class -> ``consumer_principals`` key in ``config/<env>.json``
CONSUMER_CONFIG_KEYS: dict[str, str] = {
    "website": "website_backend",
    "operator": "operator",
    "reader": "tool_reader",
    "submitter": "tool_submitter",
    "plan-writer": "tool_plan_writer",
    "financemodel-job": "financemodel_job",
    "financemodel-job-api": "financemodel_job_api",
}
_READERS = TOOL_CLASSES  # every tool class gets the GET routes


# ===================================================================== routes
@dataclass(frozen=True)
class Parts:
    """The parsed request handed to a route target."""

    path: dict[str, str]
    query: dict[str, Any]
    body: dict[str, Any]
    headers: dict[str, str]

    @property
    def request(self) -> dict[str, Any]:
        """Body with the path parameters merged in (a conflicting body value is rejected by the operation)."""
        return {**self.body, **{k: v for k, v in self.path.items() if k not in self.body}}


Invoke = Callable[[Any, OperationContext, Services, Parts], Any]


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    operation: str
    module: str
    invoke: Invoke
    grants: tuple[str, ...] = ()
    status: int = 200
    response_schema: Any = None
    query: Mapping[str, str] = field(default_factory=dict)
    owner: str = "api"
    operator_only: bool = False

    @property
    def write(self) -> bool:
        return self.method != "GET"

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile("^" + re.sub(r"\\\{([a-z_]+)\\\}", r"(?P<\1>[^/]+)", re.escape(self.path)) + "$")

    @property
    def resource_pattern(self) -> str:
        """``<METHOD>/<path with path parameters as *>``, the API resource-policy form."""
        return f"{self.method}{re.sub(r'{[a-z_]+}', '*', self.path)}"


def _q(name: str) -> Callable[[Parts], Any]:
    return lambda r: r.query.get(name)


def _delegate(operation: str) -> Invoke:
    def invoke(module: Any, ctx: OperationContext, svc: Services, r: Parts) -> Any:
        fn = _resolve_delegate(operation)
        return call_flexible(fn, ctx=ctx, svc=svc, request=r.request, path=r.path, query=r.query)

    return invoke


_PLANS = "finplan_platform.core.plans"
_VERSIONS = "finplan_platform.core.versions"
_VALIDATION = "finplan_platform.core.validation"
_PUBLICATION = "finplan_platform.core.publication"
_EXECUTION = "finplan_platform.core.execution"
_SNAPSHOTS = "finplan_platform.core.snapshot_reads"
_LIFECYCLE = "finplan_platform.core.portfolio_lifecycle"
BETA_LIFECYCLE_OPERATIONS = frozenset({"create_portfolio_decision", "get_portfolio_decision", "list_portfolio_decisions", "resolve_portfolio_decision", "get_portfolio_history", "list_portfolio_history", "list_market_snapshots", "record_activity_event", "list_activity_events"})


def _lazy_schema(target: str) -> Callable[[Mapping[str, Any]], Any]:
    """Response schema of a delegated route, imported when a response is checked (the owning
    module stays lazily imported, so a broken module fails only its own routes)."""

    def schema(_response: Mapping[str, Any]) -> Any:
        mod_name, _, attr = target.partition(":")
        return getattr(importlib.import_module(mod_name), attr)

    return schema


def _observation_query(r: Parts) -> dict[str, Any]:
    return {k: r.query.get(k) for k in ("instrument_ids", "start_date", "end_date", "page_size", "next_token")}


ROUTES: tuple[Route, ...] = (
    Route("POST", "/v1/portfolios/{portfolio_id}/decisions", "create_portfolio_decision", _LIFECYCLE, lambda m,c,s,r: m.create_decision(c,s,r.path["portfolio_id"],r.body), ("financemodel-job-api",), status=201, response_schema="api/portfolio-decision-response"),
    Route("GET", "/v1/portfolios/{portfolio_id}/decisions", "list_portfolio_decisions", _LIFECYCLE, lambda m,c,s,r: m.list_decisions(c,s,r.path["portfolio_id"],r.query), _READERS+("financemodel-job-api",), response_schema="api/list-portfolio-decisions-response", query={"page_size":"int","next_token":"str"}),
    Route("GET", "/v1/portfolio-decisions/{decision_id}", "get_portfolio_decision", _LIFECYCLE, lambda m,c,s,r: m.get_decision(c,s,r.path["decision_id"]), _READERS+("financemodel-job-api",), response_schema="api/portfolio-decision-response"),
    Route("POST", "/v1/portfolio-decisions/{decision_id}/resolution", "resolve_portfolio_decision", _LIFECYCLE, lambda m,c,s,r: m.resolve_decision(c,s,r.path["decision_id"],r.body), ("plan-writer",), response_schema="api/resolve-portfolio-decision-response"),
    Route("GET", "/v1/portfolios/{portfolio_id}/history", "list_portfolio_history", _LIFECYCLE, lambda m,c,s,r: m.list_history(c,s,r.path["portfolio_id"],r.query), _READERS+("financemodel-job-api",), response_schema="api/portfolio-history-response", query={"page_size":"int","next_token":"str"}),
    Route("GET", "/v1/portfolios/{portfolio_id}/history/{revision}", "get_portfolio_history", _LIFECYCLE, lambda m,c,s,r: m.get_history(c,s,r.path["portfolio_id"],r.path["revision"]), _READERS+("financemodel-job-api",), response_schema="api/portfolio-history-entry-response"),
    Route("GET", "/v1/snapshots", "list_market_snapshots", _SNAPSHOTS, lambda m,c,s,r: m.list_approved_snapshots(c,s,r.query), _READERS+FINANCEMODEL_CLASSES, response_schema="api/list-market-snapshots-response", query={"dataset_id":"str","page_size":"int","next_token":"str"}),
    Route("POST", "/v1/activity-events", "record_activity_event", _LIFECYCLE, lambda m,c,s,r: m.record_activity(c,s,r.body), _READERS+("financemodel-job-api",), status=201, response_schema="api/activity-event-response"),
    Route("GET", "/v1/activity-events", "list_activity_events", _LIFECYCLE, lambda m,c,s,r: m.list_activity(c,s,r.query), _READERS+("financemodel-job-api",), response_schema="api/list-activity-events-response", query={"portfolio_id":"str","session_id":"str","page_size":"int","next_token":"str"}),
    Route("POST", "/v1/portfolios", "create_portfolio", _PLANS, lambda m, c, s, r: m.create_portfolio(c, s, r.body), status=201, response_schema=CREATE_PORTFOLIO_RESPONSE),
    Route("GET", "/v1/portfolios/{portfolio_id}", "get_portfolio", _PLANS, lambda m, c, s, r: m.get_portfolio(c, s, r.path["portfolio_id"]), _READERS, response_schema="portfolio"),
    Route("GET", "/v1/portfolios/{portfolio_id}/state", "get_portfolio_state", _PLANS, lambda m, c, s, r: m.get_portfolio_state(c, s, r.path["portfolio_id"]), _READERS + ("financemodel-job-api",), response_schema="api/get-portfolio-state-response"),
    Route("PUT", "/v1/portfolios/{portfolio_id}/state", "put_portfolio_state", _PLANS, lambda m, c, s, r: m.put_portfolio_state(c, s, r.path["portfolio_id"], r.body), response_schema="api/get-portfolio-state-response", operator_only=True),
    Route("POST", "/v1/portfolios/{portfolio_id}/plans", "create_plan", _PLANS, lambda m, c, s, r: m.create_plan(c, s, r.path["portfolio_id"], r.body), status=201, response_schema=CREATE_PLAN_RESPONSE),
    Route("GET", "/v1/plans/{plan_id}", "get_plan", _PLANS, lambda m, c, s, r: m.get_plan(c, s, r.path["plan_id"]), _READERS + (AUTOMATION_CLASS, "financemodel-job-api"), response_schema="tools/get-plan-response"),
    Route(
        "GET",
        "/v1/plans/{plan_id}/versions",
        "list_plan_versions",
        _PLANS,
        lambda m, c, s, r: m.list_plan_versions(c, s, r.path["plan_id"], page_size=r.query.get("page_size"), next_token=r.query.get("next_token")),
        _READERS,
        response_schema="tools/list-plan-versions-response",
        query={"page_size": "int", "next_token": "str"},
    ),
    Route("POST", "/v1/plans/{plan_id}/versions", "create_plan_version", _VERSIONS, lambda m, c, s, r: m.create_version(c, s, r.path["plan_id"], r.body), ("plan-writer",), status=201, response_schema=create_version_response_schema),
    Route(
        "GET",
        "/v1/plan-versions/{plan_version_id}",
        "get_plan_version",
        _VERSIONS,
        lambda m, c, s, r: m.get_plan_version(c, s, r.path["plan_version_id"], download=bool(r.query.get("download"))),
        _READERS+("financemodel-job-api",),
        response_schema="tools/get-plan-version-response",
        query={"download": "bool"},
    ),
    Route("POST", "/v1/plan-versions/{plan_version_id}/validate", "validate_plan_version", _VALIDATION, lambda m, c, s, r: m.validate_plan_version(c, s, r.path["plan_version_id"], r.body), ("plan-writer",), response_schema="tools/validate-plan-version-response"),
    Route("POST", "/v1/plans/{plan_id}/publications", "publish_plan_version", _PUBLICATION, lambda m, c, s, r: m.publish(c, s, r.path["plan_id"], r.body), ("plan-writer",), status=201, response_schema="tools/publish-plan-version-response"),
    Route("GET", "/v1/plans/{plan_id}/publications", "list_publications", _PUBLICATION, lambda m,c,s,r: m.list_publications(c,s,r.path["plan_id"],page_size=r.query.get("page_size"),next_token=r.query.get("next_token")), _READERS, response_schema="tools/list-publications-response", query={"page_size":"int","next_token":"str"}),
    Route("GET", "/v1/publications/{publication_id}/executions", "list_executions", _EXECUTION, lambda m,c,s,r: m.list_executions(c,s,r.path["publication_id"],page_size=r.query.get("page_size"),next_token=r.query.get("next_token")), _READERS+("financemodel-job-api",), response_schema="tools/list-executions-response", query={"page_size":"int","next_token":"str"}),
    Route("GET", "/v1/publications/{publication_id}", "get_publication", _PUBLICATION, lambda m, c, s, r: m.get_publication(c, s, r.path["publication_id"]), _READERS+("financemodel-job-api",), response_schema="publication"),
    Route("POST", "/v1/publications/{publication_id}/executions", "record_execution", _EXECUTION, lambda m, c, s, r: m.record_execution(c, s, r.path["publication_id"], r.body), status=201, response_schema=RECORD_EXECUTION_RESPONSE),
    Route("GET", "/v1/executions/{execution_id}", "get_execution", _EXECUTION, lambda m, c, s, r: m.get_execution(c, s, r.path["execution_id"]), _READERS, response_schema="execution"),
    Route("GET", "/v1/snapshots/latest", "latest_approved_snapshot", _SNAPSHOTS, lambda m, c, s, r: m.latest_approved_snapshot(c, s, r.query), _READERS + FINANCEMODEL_CLASSES, response_schema=SNAPSHOT_RESPONSE, query={"dataset_id": "str"}),
    Route(
        "GET",
        "/v1/snapshots/{input_snapshot_id}",
        "get_snapshot",
        _SNAPSHOTS,
        lambda m, c, s, r: m.get_snapshot(c, s, r.path["input_snapshot_id"], download=bool(r.query.get("download"))),
        _READERS + FINANCEMODEL_CLASSES + (AUTOMATION_CLASS,),
        response_schema=SNAPSHOT_RESPONSE,
        query={"download": "bool"},
    ),
    Route(
        "GET",
        "/v1/snapshots/{input_snapshot_id}/observations",
        "read_snapshot_observations",
        _SNAPSHOTS,
        lambda m, c, s, r: m.read_observations(c, s, r.path["input_snapshot_id"], _observation_query(r)),
        _READERS + FINANCEMODEL_CLASSES,
        response_schema=OBSERVATIONS_RESPONSE,
        query={"instrument_id": "list", "start_date": "str", "end_date": "str", "page_size": "int", "next_token": "str"},
    ),
    # ---- routes delegated to modules owned by other agents (lazily imported) ----
    Route("POST", "/v1/ingestions", "run_ingestion", "", _delegate("run_ingestion"), ("submitter",), owner="ingestion", response_schema=REFRESH_MARKET_DATA_RESPONSE),
    Route("POST", "/v1/plans/{plan_id}/staged-outputs/{run_id}/accept", "accept_staged_output", "", _delegate("accept_staged_output"), (AUTOMATION_CLASS,), owner="staging", response_schema=_lazy_schema("finplan_platform.core.staging:ACCEPT_STAGED_OUTPUT_RESPONSE")),
    Route("GET", "/v1/daily-trigger/outcomes/{session_date}", "get_daily_trigger_outcomes", "", _delegate("get_daily_trigger_outcomes"), _READERS + (AUTOMATION_CLASS,), owner="daily_trigger", response_schema=_lazy_schema("finplan_platform.core.daily_trigger:OUTCOMES_RESPONSE")),
    Route("GET", "/v1/staged-outputs/{run_id}", "get_staged_output", "", _delegate("get_staged_output"), _READERS + ("financemodel-job-api",), owner="staging", response_schema=GET_STAGED_OUTPUT_RESPONSE),
    Route("POST", "/v1/plan-versions/{plan_version_id}/exports", "export_plan_version", "", _delegate("export_plan_version"), owner="excel", response_schema=_lazy_schema("finplan_platform.excel.service:EXPORT_RESPONSE")),
    Route("POST", "/v1/plans/{plan_id}/imports", "issue_import_grant", "", _delegate("issue_import_grant"), owner="excel", response_schema=_lazy_schema("finplan_platform.excel.service:IMPORT_GRANT_RESPONSE")),
    Route("POST", "/v1/plans/{plan_id}/imports/{import_id}/commit", "commit_import", "", _delegate("commit_import"), owner="excel", response_schema=_lazy_schema("finplan_platform.excel.service:COMMIT_IMPORT_RESPONSE")),
)

#: Delegated operation -> candidate ``module:function`` targets, tried in order. The last
#: candidate of ``get_staged_output`` is a platform fallback reading the outcome row directly.
DELEGATES: dict[str, tuple[str, ...]] = {
    "run_ingestion": ("finplan_platform.core.ingestion:run_ingestion",),
    "accept_staged_output": ("finplan_platform.core.staging:accept_staged_output",),
    "get_daily_trigger_outcomes": ("finplan_platform.core.daily_trigger:get_outcomes",),
    "get_staged_output": (
        "finplan_platform.core.staging:get_staged_output",
        "finplan_platform.core.staging:read_staged_output",
        "finplan_platform.handlers.api:staged_output_fallback",
    ),
    "export_plan_version": ("finplan_platform.excel:export_plan_version", "finplan_platform.excel.service:export_plan_version", "finplan_platform.excel.export:export_plan_version"),
    "issue_import_grant": ("finplan_platform.excel:issue_import_grant", "finplan_platform.excel.service:issue_import_grant", "finplan_platform.excel.importer:issue_import_grant"),
    "commit_import": ("finplan_platform.excel:commit_import", "finplan_platform.excel.service:commit_import", "finplan_platform.excel.importer:commit_import"),
}


class RouteNotWired(Exception):
    pass


def _resolve_delegate(operation: str) -> Callable[..., Any]:
    tried = []
    for cand in DELEGATES.get(operation, ()):
        mod_name, _, fn_name = cand.partition(":")
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            if exc.name and (mod_name == exc.name or mod_name.startswith(exc.name + ".")):
                tried.append(cand)
                continue
            raise
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            return fn
        tried.append(cand)
    raise RouteNotWired(f"operation {operation} is not available in this release (tried {', '.join(tried) or 'nothing'})")


_ALIASES = {
    "ctx": ("ctx", "context", "op_ctx"),
    "svc": ("svc", "services"),
    "repo": ("repo", "repository"),
    "store": ("store", "artifacts", "artifact_store"),
    "cfg": ("cfg", "config", "env_config"),
    "request": ("request", "body", "req", "payload"),
    "query": ("query", "params"),
    "clock": ("clock",),
}


def call_flexible(fn: Callable[..., Any], *, ctx: OperationContext, svc: Services, request: Mapping[str, Any], path: Mapping[str, str], query: Mapping[str, Any]) -> Any:
    """Call a delegated operation, binding its parameters by name (see module docstring)."""
    values: dict[str, Any] = {}
    sources = {"ctx": ctx, "svc": svc, "repo": svc.repo, "store": svc.artifacts, "cfg": svc.cfg, "request": dict(request), "query": dict(query), "clock": ctx.clock}
    for key, names in _ALIASES.items():
        for n in names:
            values[n] = sources[key]
    values.update(path)
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for i, (name, p) in enumerate(inspect.signature(fn).parameters.items()):
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if name in values:
            value = values[name]
        elif i == 0:
            value = ctx
        elif p.default is not p.empty:
            continue
        else:
            raise RouteNotWired(f"cannot bind parameter {name!r} of {getattr(fn, '__qualname__', fn)}")
        if p.kind == p.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[name] = value
    return fn(*args, **kwargs)


def staged_output_fallback(ctx: OperationContext, svc: Services, run_id: str) -> dict[str, Any]:
    """``GET /v1/staged-outputs/{run_id}`` when the staging module exposes no reader: the stored outcome row."""
    from ..core.contract_io import GET_STAGED_OUTPUT_REQUEST, require_valid
    from ..core.upgrade import serve

    require_valid({"run_id": run_id}, GET_STAGED_OUTPUT_REQUEST)
    return serve(svc.repo.require("staged_output", run_id), overlay=False)


def match_route(method: str, path: str, resource: str | None = None) -> tuple[Route, dict[str, str]] | None:
    method = method.upper()
    for route in ROUTES:
        if route.method != method:
            continue
        if resource is not None and resource == route.path:
            m = route.regex.match(path)
            return route, (m.groupdict() if m else {})
        m = route.regex.match(path)
        if m:
            return route, m.groupdict()
    return None


# ===================================================================== principals
def role_name_of(arn: str) -> str | None:
    """Role name of a role ARN (``...:role/<path/>name``) or an assumed-role session ARN."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return None
    service, resource = parts[2], parts[5]
    if service == "iam" and resource.startswith("role/"):
        return resource.rsplit("/", 1)[-1]
    if service == "sts" and resource.startswith("assumed-role/"):
        segs = resource.split("/")
        return segs[1] if len(segs) >= 3 else None
    return None


def normalize_principal(arn: str) -> str:
    """Assumed-role session ARN -> role ARN (stable idempotency scope); other ARNs unchanged."""
    parts = arn.split(":", 5)
    name = role_name_of(arn)
    if name and len(parts) == 6 and parts[2] == "sts":
        return f"arn:{parts[1]}:iam::{parts[4]}:role/{name}"
    return arn


def resolve_role_class(principal_arn: str, cfg: Any) -> str | None:
    name = role_name_of(principal_arn)
    if not name:
        return None
    patterns = {cls: cfg.principal_pattern(key) for cls, key in CONSUMER_CONFIG_KEYS.items()}
    for cls in ("website", "operator"):
        if fnmatch.fnmatchcase(name, patterns[cls]):
            return cls
    if any(fnmatch.fnmatchcase(name, pat) for pat in automation_role_patterns(cfg.env)):
        return AUTOMATION_CLASS
    if name.startswith(f"finplan-{cfg.env}-financialplanning-"):
        return "platform"
    for cls in TOOL_CLASSES + FINANCEMODEL_CLASSES:
        if fnmatch.fnmatchcase(name, patterns[cls]):
            return cls
    return None


def route_allowed(route: Route, role_class: str | None) -> bool:
    if role_class is None:
        return False
    if route.operator_only:
        return role_class in ("platform", "operator")
    return role_class in FULL_ACCESS_CLASSES or role_class in route.grants


def routes_for_class(role_class: str) -> list[Route]:
    return [r for r in ROUTES if route_allowed(r, role_class)]


# ===================================================================== HTTP mapping
_STATUS = {
    "VALIDATION_FAILED": 400,
    "INVALID_IDENTIFIER": 400,
    "UNSUPPORTED_CONTRACT_VERSION": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "OPERATION_NOT_PERMITTED": 403,
    "BUDGET_EXCEEDED": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "IMMUTABLE_RECORD": 409,
    "IDEMPOTENCY_KEY_REUSED": 422,
    "PRECONDITION_FAILED": 422,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
    "DEPENDENCY_UNAVAILABLE": 503,
}


def http_status(code: str) -> int:
    return _STATUS.get(code, 500)


def _normalize_result(result: Any) -> tuple[dict[str, Any], bool]:
    if hasattr(result, "response") and hasattr(result, "replayed"):
        return dict(result.response), bool(result.replayed)
    if isinstance(result, Mapping):
        return dict(result), False
    for attr in ("to_response", "to_dict"):
        if callable(getattr(result, attr, None)):
            return dict(getattr(result, attr)()), False
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result), False
    raise PlatformError.internal("operation returned an unsupported result")


def _convert_query(route: Route, raw: Sequence[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw:
        kind = route.query.get(key)
        if kind is None:
            raise PlatformError.validation(f"unknown query parameter {key!r}" if re.fullmatch(r"[a-z_]{1,32}", key) else "unknown query parameter", pointer=f"/{key}" if re.fullmatch(r"[a-z_]{1,32}", key) else "")
        if kind == "int":
            if not re.fullmatch(r"[0-9]{1,6}", value):
                raise PlatformError.validation(f"{key} must be an integer", pointer=f"/{key}")
            out[key] = int(value)
        elif kind == "bool":
            if value not in ("true", "false"):
                raise PlatformError.validation(f"{key} must be true or false", pointer=f"/{key}")
            out[key] = value == "true"
        elif kind == "list":
            out.setdefault(key + "s", []).extend(v for v in value.split(",") if v)
        else:
            out[key] = value
    return out


# ===================================================================== app
class ApiApp:
    """The router bound to one environment's :class:`Services` (a Lambda container or a test)."""

    def __init__(self, services: Services | Callable[[], Services], *, clock: Clock | None = None, ids: IdFactory | None = None) -> None:
        self._services = services
        self._svc: Services | None = services if isinstance(services, Services) else None
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory(self.clock)

    @property
    def svc(self) -> Services:
        if self._svc is None:
            assert callable(self._services)
            self._svc = self._services()
        return self._svc

    # ---------------------------------------------------------------- entry
    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items() if v is not None}
        cid = headers.get("x-correlation-id", "")
        if not CORRELATION_ID_RE.match(cid):
            cid = new_correlation_id()
        try:
            status, body, extra = self._dispatch(event, headers, cid)
        except PlatformError as exc:
            status, body, extra = http_status(exc.code), self._envelope(exc, cid), {}
        except Exception:
            log.exception("unhandled error (correlation_id=%s)", cid)
            status, body, extra = 500, self._envelope(PlatformError.internal(), cid), {}
        out_headers = {"Content-Type": "application/json", "X-Correlation-Id": cid, "Cache-Control": "no-store", **extra}
        return {"statusCode": status, "headers": out_headers, "body": json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)}

    def _envelope(self, exc: PlatformError, cid: str) -> dict[str, Any]:
        env = exc.to_envelope(cid, current_version())
        res = validate_document(env, "error")
        if not res.valid:  # a malformed envelope is itself a producer bug
            log.error("error envelope failed validation: %s", [i.message for i in res.issues])
            env = PlatformError.internal().to_envelope(cid, current_version())
        return env

    # ---------------------------------------------------------------- dispatch
    def _dispatch(self, event: Mapping[str, Any], headers: Mapping[str, str], cid: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        method = str(event.get("httpMethod") or "").upper()
        path = str(event.get("path") or "")
        resource = event.get("resource")
        if resource and "{proxy+}" in str(resource):
            resource = None
        found = match_route(method, path, resource)
        if found is None:
            raise PlatformError.not_found("no such route", method=method if re.fullmatch(r"[A-Z]{3,7}", method) else "unknown")
        route, path_params = found
        if event.get("pathParameters"):
            path_params = {**path_params, **{str(k): str(v) for k, v in event["pathParameters"].items() if k in path_params}}

        identity = ((event.get("requestContext") or {}).get("identity") or {})
        raw_principal = identity.get("userArn") or identity.get("caller")
        if not raw_principal or not isinstance(raw_principal, str):
            raise PlatformError("UNAUTHORIZED", "the request carries no authenticated IAM identity")
        principal = normalize_principal(raw_principal)
        svc = self.svc
        role_class = resolve_role_class(principal, svc.cfg)
        if not route_allowed(route, role_class):
            raise PlatformError.forbidden("the caller is not allowed to call this route", operation=route.operation, role_class=role_class or "unknown")

        on_behalf_of = self._caller_block(headers)
        check_declared_version(headers.get(CONTRACT_VERSION_HEADER), pointer="/contract_version")
        body = self._body(event, route)
        check_declared_version(body.get("contract_version"))
        query = _convert_query(route, self._query_pairs(event))

        ctx = OperationContext(
            caller=Caller(principal=principal, role_class=role_class, channel=(on_behalf_of or {}).get("channel"), on_behalf_of=on_behalf_of),
            env=svc.env,
            correlation_id=cid,
            clock=self.clock,
            ids=self.ids,
            synthetic=True,
        )
        parts = Parts(path=dict(path_params), query=query, body=body, headers=dict(headers))
        module = importlib.import_module(route.module) if route.module else None
        try:
            result = route.invoke(module, ctx, svc, parts)
        except RouteNotWired as exc:
            log.warning("route not wired: %s", exc)
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "this operation is not available in the deployed release", retryable=False, operation=route.operation) from None
        response, replayed = _normalize_result(result)
        schema = route.response_schema(response) if callable(route.response_schema) else route.response_schema
        if schema is not None:
            require_valid_response(response, schema)
        extra = {"X-Idempotent-Replay": "true"} if replayed else {}
        return route.status, response, extra

    @staticmethod
    def _caller_block(headers: Mapping[str, str]) -> dict[str, Any] | None:
        raw = headers.get("x-finplan-caller")
        if not raw:
            return None
        try:
            block = loads_strict(raw)
        except (ValueError, CanonicalizationError):
            raise PlatformError.validation("X-Finplan-Caller is not a JSON caller block", pointer="", header="X-Finplan-Caller") from None
        res = validate_document(block, "caller")
        if not res.valid:
            raise PlatformError.validation("X-Finplan-Caller does not match the contract caller block", pointer="", header="X-Finplan-Caller")
        return dict(block)

    @staticmethod
    def _body(event: Mapping[str, Any], route: Route) -> dict[str, Any]:
        raw = event.get("body")
        if raw in (None, ""):
            if route.write:
                raise PlatformError.validation("request body is required", pointer="")
            return {}
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        try:
            body = loads_strict(raw)
        except (ValueError, CanonicalizationError):
            raise PlatformError.validation("request body is not valid JSON", pointer="") from None
        if not isinstance(body, dict):
            raise PlatformError.validation("request body must be a JSON object", pointer="")
        if not route.write:
            raise PlatformError.validation("GET requests take no body", pointer="")
        return body

    @staticmethod
    def _query_pairs(event: Mapping[str, Any]) -> list[tuple[str, str]]:
        multi = event.get("multiValueQueryStringParameters")
        if multi:
            return [(str(k), str(v)) for k, vs in multi.items() for v in (vs or [])]
        single = event.get("queryStringParameters") or {}
        return [(str(k), str(v)) for k, v in single.items()]


# ===================================================================== local client
class LocalClient:
    """In-process client that sends API Gateway proxy events to an :class:`ApiApp`.

    Used by the unit and contract suites and as the "website-path client" double: the same
    routes, the same handler, a distinct IAM principal.
    """

    def __init__(self, app: ApiApp, principal: str, *, headers: Mapping[str, str] | None = None) -> None:
        self.app = app
        self.principal = principal
        self.headers = dict(headers or {})

    def event(self, method: str, path: str, body: Any = None, *, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        url = urlsplit(path)
        pairs = parse_qsl(url.query, keep_blank_values=True)
        multi: dict[str, list[str]] = {}
        for k, v in pairs:
            multi.setdefault(k, []).append(v)
        return {
            "httpMethod": method.upper(),
            "path": url.path,
            "resource": None,
            "headers": {**self.headers, **dict(headers or {})},
            "queryStringParameters": {k: v[-1] for k, v in multi.items()} or None,
            "multiValueQueryStringParameters": multi or None,
            "pathParameters": None,
            "body": None if body is None else (body if isinstance(body, str) else json.dumps(body)),
            "isBase64Encoded": False,
            "requestContext": {"identity": {"userArn": self.principal} if self.principal else {}, "requestId": str(uuid.uuid4()), "stage": "local"},
        }

    def call(self, method: str, path: str, body: Any = None, *, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        resp = self.app.handle(self.event(method, path, body, headers=headers))
        return resp["statusCode"], json.loads(resp["body"]), resp["headers"]

    def get(self, path: str, **kw: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        return self.call("POST", path, {} if body is None else body, **kw)


# ===================================================================== Lambda entry
_APP: ApiApp | None = None


def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover - AWS entry point
    global _APP
    if _APP is None:
        _APP = ApiApp(Services.from_lambda_environment)
    return _APP.handle(event)
