"""Ingestion Lambda entry point (task 6.8; ING-01): every trigger reaches :func:`run_ingestion`.

Accepted events:

1. **Scheduler** (``source`` ``finplan.scheduler``): handled by
   :func:`finplan_platform.handlers.scheduler.handle_scheduled`; errors are raised so the
   asynchronous retry policy and the dead-letter queue see them.
2. **Platform invoke** from the plan-API function (``source`` ``finplan.plan-api``):
   ``{"source", "caller": {"principal", "role_class"?, "channel"?, "on_behalf_of"?},
   "correlation_id", "body"}``. The plan-API function authenticated the caller (API Gateway
   IAM auth) and forwards the identity; the ingestion function's resource policy and the
   bucket/table policies admit only platform principals of the environment. Returns
   ``{"statusCode", "body"}`` with the response or the contract error envelope.
3. **API Gateway proxy** (``POST /v1/ingestions`` integrated directly, if the API stack
   chooses to): the caller principal comes from ``requestContext.identity.userArn``, never
   from the body. Returns an API Gateway proxy response.

The on-demand request body is the contract ``refresh-market-data-request``
(``idempotency_key`` required).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from ..core.clock import Clock, SystemClock
from ..core.context import CORRELATION_ID_RE, Caller, OperationContext, new_correlation_id
from ..core.errors import PlatformError
from ..core.ingestion import IngestionDeps, default_deps, ingest
from .scheduler import handle_scheduled, is_scheduler_event

log = logging.getLogger(__name__)

__all__ = ["PLATFORM_INVOKE_SOURCE", "handle_on_demand", "handler", "http_status"]

PLATFORM_INVOKE_SOURCE = "finplan.plan-api"
_STATUS = {
    "VALIDATION_FAILED": 400,
    "INVALID_IDENTIFIER": 400,
    "UNSUPPORTED_CONTRACT_VERSION": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "OPERATION_NOT_PERMITTED": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "IMMUTABLE_RECORD": 409,
    "PRECONDITION_FAILED": 412,
    "BUDGET_EXCEEDED": 402,
    "RATE_LIMITED": 429,
    "DEPENDENCY_UNAVAILABLE": 503,
    "INTERNAL": 500,
}


def http_status(code: str) -> int:
    return _STATUS.get(code, 500)


def _correlation(value: Any) -> str:
    return value if isinstance(value, str) and CORRELATION_ID_RE.match(value) else new_correlation_id()


def handle_on_demand(caller: Caller, body: Any, *, correlation_id: str | None = None, deps: IngestionDeps | None = None, clock: Clock | None = None) -> tuple[int, dict[str, Any]]:
    """Run an on-demand ingestion; returns ``(http_status, response_or_error_envelope)``."""
    cid = _correlation(correlation_id)
    try:
        deps = deps or default_deps()
        ctx = OperationContext(caller=caller, env=deps.config.env, correlation_id=cid, clock=clock or SystemClock(), trigger="on_demand")
        outcome = ingest(ctx, body, deps=deps)
    except PlatformError as exc:
        return http_status(exc.code), exc.to_envelope(cid)
    except Exception:
        log.exception("ingestion failed", extra={"correlation_id": cid})
        return 500, PlatformError.internal().to_envelope(cid)
    status = 201 if outcome.response.get("new_snapshot") and not outcome.replayed else 200
    return status, {**outcome.response, "correlation_id": cid}


def _caller_from_invoke(event: Mapping[str, Any]) -> Caller:
    c = event.get("caller") or {}
    return Caller(principal=str(c.get("principal") or ""), role_class=c.get("role_class"), channel=c.get("channel") or "api", on_behalf_of=c.get("on_behalf_of"))


def _caller_from_proxy(event: Mapping[str, Any]) -> Caller:
    identity = (event.get("requestContext") or {}).get("identity") or {}
    principal = identity.get("userArn") or identity.get("caller")
    if not principal:
        raise PlatformError("UNAUTHORIZED", "the request carries no authenticated principal")
    return Caller(principal=str(principal), channel="api")


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    if is_scheduler_event(event):
        result = handle_scheduled(event)
        log.info(json.dumps({"trigger": "scheduled", "input_snapshot_id": result.get("input_snapshot_id"), "correlation_id": result.get("correlation_id")}))
        return result
    if isinstance(event, Mapping) and event.get("source") == PLATFORM_INVOKE_SOURCE:
        try:
            caller = _caller_from_invoke(event)
        except ValueError:
            cid = _correlation(event.get("correlation_id"))
            return {"statusCode": 401, "body": PlatformError("UNAUTHORIZED", "no caller principal forwarded").to_envelope(cid)}
        status, body = handle_on_demand(caller, event.get("body"), correlation_id=event.get("correlation_id"))
        return {"statusCode": status, "body": body}
    if isinstance(event, Mapping) and "requestContext" in event:
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        cid = _correlation(headers.get("x-correlation-id"))
        try:
            caller = _caller_from_proxy(event)
            raw = event.get("body") or "{}"
            body = json.loads(raw) if isinstance(raw, str) else raw
        except PlatformError as exc:
            return _proxy(http_status(exc.code), exc.to_envelope(cid))
        except json.JSONDecodeError:
            return _proxy(400, PlatformError.validation("request body is not valid JSON").to_envelope(cid))
        status, out = handle_on_demand(caller, body, correlation_id=cid)
        return _proxy(status, out)
    cid = new_correlation_id()
    return {"statusCode": 400, "body": PlatformError.validation("unrecognized ingestion event").to_envelope(cid)}


def _proxy(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    cid = body.get("correlation_id")
    return {"statusCode": status, "headers": {"Content-Type": "application/json", **({"X-Correlation-Id": cid} if cid else {})}, "body": json.dumps(body, separators=(",", ":"))}
