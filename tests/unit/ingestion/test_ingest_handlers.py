"""Trigger adapters (task 6.8; ING-01, ING-09): scheduler event, platform invoke, API proxy, plan-API Services."""

from __future__ import annotations

import json
from typing import Any

import pytest
from finplan_platform.core.clock import FrozenClock
from finplan_platform.core.context import Caller, OperationContext
from finplan_platform.core.ingestion import run_ingestion, set_default_deps
from finplan_platform.handlers import ingest as ingest_handler
from finplan_platform.handlers.scheduler import handle_scheduled, schedule_principal, scheduler_input

from .conftest import on_demand


@pytest.fixture
def wired(make_deps: Any) -> Any:
    deps = make_deps()
    set_default_deps(deps)
    yield deps
    set_default_deps(None)


def _event(ts: str = "2026-01-12T14:00:00Z") -> dict[str, Any]:
    ev = dict(scheduler_input("finance/etf-daily/SPY"))
    ev["scheduled_time"] = ts
    ev["execution_id"] = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    return ev


def test_scheduler_event_runs_scheduled_ingestion_with_derived_key(wired: Any, clock: FrozenClock) -> None:
    clock.set("2026-01-12T14:00:00Z")
    a = handle_scheduled(_event(), deps=wired, clock=clock)
    b = ingest_handler.handler(_event())  # duplicate delivery through the Lambda entry point
    assert a["input_snapshot_id"] == b["input_snapshot_id"] and a["trigger"] == "scheduled"
    assert a["correlation_id"].startswith("cor_sched_")
    rec = next(iter(wired.repo.scan("snapshot_catalog")))
    assert rec.attrs["caller_principal"] == schedule_principal("beta") and rec.attrs["trigger"] == "scheduled"
    assert len(list(wired.repo.scan("snapshot_catalog"))) == 1


def test_scheduler_errors_are_raised_for_retry_and_dlq(wired: Any) -> None:
    with pytest.raises(Exception) as ei:
        ingest_handler.handler(_event("2031-03-03T14:00:00Z"))  # outside calendar coverage
    assert getattr(ei.value, "code", None) == "PRECONDITION_FAILED"


def test_platform_invoke_from_plan_api_returns_status_and_body(wired: Any) -> None:
    ev = {"source": "finplan.plan-api", "caller": {"principal": "arn:aws:iam::<account-id>:role/finplan-beta-financelambdastool-tool-role-submitter", "role_class": "submitter"}, "correlation_id": "cor_test_invoke_1", "body": on_demand("2026-01-09", "2026-01-09")}
    out = ingest_handler.handler(ev)
    assert out["statusCode"] == 201 and out["body"]["correlation_id"] == "cor_test_invoke_1"
    again = ingest_handler.handler(ev)
    assert again["statusCode"] == 200 and again["body"]["input_snapshot_id"] == out["body"]["input_snapshot_id"]
    bad = ingest_handler.handler({**ev, "body": {**ev["body"], "dataset_id": "finance/index-level/SPX", "idempotency_key": "x2"}})
    assert bad["statusCode"] == 400 and bad["body"]["code"] == "VALIDATION_FAILED" and bad["body"]["correlation_id"] == "cor_test_invoke_1"


def test_api_gateway_proxy_uses_the_authenticated_principal(wired: Any) -> None:
    ev = {
        "requestContext": {"identity": {"userArn": "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-website"}},
        "headers": {"X-Correlation-Id": "cor_proxy_test_01"},
        "body": json.dumps(on_demand("2026-01-09", "2026-01-09", "proxy-1")),
    }
    out = ingest_handler.handler(ev)
    assert out["statusCode"] == 201 and out["headers"]["X-Correlation-Id"] == "cor_proxy_test_01"
    rec = next(iter(wired.repo.scan("snapshot_catalog")))
    assert rec.attrs["caller_principal"].endswith("financialplanning-website")
    anon = ingest_handler.handler({"requestContext": {"identity": {}}, "body": "{}"})
    assert anon["statusCode"] == 401 and json.loads(anon["body"])["code"] == "UNAUTHORIZED"


def test_budget_exceeded_maps_to_an_envelope(make_deps: Any) -> None:
    deps = make_deps(budget_state="enforced")
    status, body = ingest_handler.handle_on_demand(Caller("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-operator"), on_demand("2026-01-09", "2026-01-09"), deps=deps)
    assert status == 402 and body["code"] == "BUDGET_EXCEEDED" and body["retryable"] is False


def test_plan_api_services_path_runs_the_same_operation(make_deps: Any, clock: FrozenClock) -> None:
    """The plan-API router calls ``run_ingestion(ctx, request, svc=...)`` in-process."""
    deps = make_deps()

    class Svc:  # the subset of core.services.Services the ingestion path reads
        cfg = deps.config
        config = deps.config
        repo = deps.repo
        store = deps.store
        extras = {"ingestion_provider": deps.provider, "budget_gate": deps.budget_gate}

    ctx = OperationContext(caller=Caller("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-operator"), env="beta", correlation_id="cor_svc_path_01", clock=clock)
    res = run_ingestion(ctx, on_demand("2026-01-09", "2026-01-09"), svc=Svc())
    assert res["new_snapshot"] and res["snapshot"]["status"] == "approved"


def test_plan_api_router_binds_run_ingestion(make_deps: Any, clock: FrozenClock) -> None:
    """``POST /v1/ingestions`` is delegated by the plan-API router; its parameter binding reaches ``svc``."""
    api = pytest.importorskip("finplan_platform.handlers.api")
    from finplan_platform.core.ingestion import run_ingestion as target
    from finplan_platform.core.services import Services

    deps = make_deps()
    svc = Services(cfg=deps.config, repo=deps.repo, artifacts=deps.store, extras={"ingestion_provider": deps.provider, "budget_gate": deps.budget_gate})
    ctx = OperationContext(caller=Caller("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-operator"), env="beta", correlation_id="cor_router_bind_1", clock=clock)
    res = api.call_flexible(target, ctx=ctx, svc=svc, request=on_demand("2026-01-09", "2026-01-09"), path={}, query={})
    assert res["new_snapshot"] and res["snapshot"]["status"] == "approved"
