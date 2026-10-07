"""Scheduler adapter (task 6.8; ING-01, ING-02, ING-09): EventBridge Scheduler event -> :func:`run_ingestion`.

The daily schedule (``America/New_York``, weekdays, 09:00 or 09:30 from configuration)
invokes the ingestion function directly with this static input, in which EventBridge
Scheduler substitutes the context attributes::

    {"source": "finplan.scheduler", "trigger": "scheduled", "dataset_id": "finance/etf-daily/<ticker>",
     "scheduled_time": "<aws.scheduler.scheduled-time>", "execution_id": "<aws.scheduler.execution-id>"}

The adapter builds an :class:`OperationContext` with ``trigger="scheduled"`` and the schedule's
platform principal, then calls the same operation the API route uses. The idempotency key is
derived from environment, dataset and scheduled session date inside the operation, so a
duplicate delivery or a retry returns the original result. Errors are raised (not swallowed)
so the Lambda asynchronous retry policy retries them and exhausted events reach the
dead-letter queue, whose depth raises the alarm.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..core.clock import Clock, SystemClock
from ..core.context import Caller, OperationContext, new_correlation_id
from ..core.ingestion import IngestionDeps, default_deps, run_ingestion

__all__ = ["SCHEDULER_SOURCE", "handle_scheduled", "is_scheduler_event", "schedule_principal", "scheduler_input"]

SCHEDULER_SOURCE = "finplan.scheduler"
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def is_scheduler_event(event: Any) -> bool:
    return isinstance(event, Mapping) and event.get("source") == SCHEDULER_SOURCE and event.get("trigger") == "scheduled"


def schedule_principal(env: str) -> str:
    """Stable idempotency/audit principal of the daily schedule (its platform role name)."""
    return f"finplan-{env}-financialplanning-daily-ingest-schedule-role"


def scheduler_input(dataset_id: str) -> dict[str, str]:
    """The schedule target's static input (context attributes are substituted by the scheduler)."""
    return {
        "source": SCHEDULER_SOURCE,
        "trigger": "scheduled",
        "dataset_id": dataset_id,
        "scheduled_time": "<aws.scheduler.scheduled-time>",
        "execution_id": "<aws.scheduler.execution-id>",
    }


def _correlation_id(event: Mapping[str, Any]) -> str:
    exec_id = _SAFE.sub("", str(event.get("execution_id") or ""))
    if 8 <= len(exec_id) <= 100:
        return f"cor_sched_{exec_id}"
    return new_correlation_id()


def handle_scheduled(event: Mapping[str, Any], *, deps: IngestionDeps | None = None, clock: Clock | None = None) -> dict[str, Any]:
    deps = deps or default_deps()
    env = deps.config.env
    ctx = OperationContext(
        caller=Caller(principal=schedule_principal(env), role_class="platform", channel="scheduler"),
        env=env,
        correlation_id=_correlation_id(event),
        clock=clock or SystemClock(),
        trigger="scheduled",
    )
    request = {"dataset_id": event.get("dataset_id") or deps.config.dataset_id, "scheduled_time": event.get("scheduled_time")}
    response = run_ingestion(ctx, request, deps=deps)
    return {"correlation_id": ctx.correlation_id, **response}
