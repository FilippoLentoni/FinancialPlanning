"""DLY-06 (the trigger and scheduler roles cannot publish: API resource policy, the step role's identity
policy and the router), the trigger infrastructure shape, and the idempotent research-plan step (task 3.3)."""

from __future__ import annotations

from typing import Any

import pytest
from finplan_platform.core.config import load_config
from finplan_platform.handlers.api import (
    AUTOMATION_CLASS,
    ROUTES,
    resolve_role_class,
    route_allowed,
)

from infra.policy_sim import Principal, role_arn, simulate
from infra.stacks.api import resource_policy_document
from infra.stacks.daily_trigger import publish_deny_resources, trigger_api_resources

PUBLISH = "execute-api:/v1/POST/v1/plans/pl_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K/publications"
ACCEPT = "execute-api:/v1/POST/v1/plans/pl_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K/staged-outputs/run_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K/accept"
AUTOMATION_ROLES = ("finplan-beta-financialplanning-daily-trigger-step-role", "finplan-beta-financialplanning-daily-ingest-schedule-role")


def _route(op: str) -> Any:
    return next(r for r in ROUTES if r.operation == op)


@pytest.mark.parametrize("role", AUTOMATION_ROLES)
def test_resource_policy_denies_publish_to_trigger_and_scheduler_roles(role: str) -> None:
    rp = resource_policy_document(load_config("beta"), role_arn)
    p = Principal.role(role)
    assert not simulate("execute-api:Invoke", PUBLISH, p, resource_policy=rp).allowed
    assert simulate("execute-api:Invoke", ACCEPT, p, resource_policy=rp).allowed
    # another platform role (the operator path) still publishes
    assert simulate("execute-api:Invoke", PUBLISH, Principal.role("finplan-beta-financialplanning-operator"), resource_policy=rp).allowed


def test_step_role_identity_policy_explicitly_denies_publish() -> None:
    def arn(p: str) -> str:
        return f"arn:aws:execute-api:us-east-2:<account-id>:*/{p.split('/', 1)[1]}" if p.startswith("*/") else p

    policy = {
        "Statement": [
            {"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": [arn(p) for p in trigger_api_resources()]},
            {"Effect": "Deny", "Action": "execute-api:Invoke", "Resource": [arn(p) for p in publish_deny_resources()]},
        ]
    }
    p = Principal.role(AUTOMATION_ROLES[0], policy)
    res = "arn:aws:execute-api:us-east-2:<account-id>:abc123/v1/"
    assert not simulate("execute-api:Invoke", res + "POST/v1/plans/pl_X/publications", p).allowed
    assert simulate("execute-api:Invoke", res + "POST/v1/plans/pl_X/staged-outputs/run_X/accept", p).allowed
    assert simulate("execute-api:Invoke", res + "GET/v1/snapshots/snap_X", p).allowed
    assert not simulate("execute-api:Invoke", res + "POST/v1/plans/pl_X/versions", p).allowed


def test_router_class_of_automation_roles_has_no_publish() -> None:
    cfg = load_config("beta")
    for role in AUTOMATION_ROLES:
        assert resolve_role_class(f"arn:aws:sts::<account-id>:assumed-role/{role}/session", cfg) == AUTOMATION_CLASS
    assert resolve_role_class("arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-plan-api-handler-role", cfg) == "platform"
    assert not route_allowed(_route("publish_plan_version"), AUTOMATION_CLASS)
    assert not route_allowed(_route("create_plan_version"), AUTOMATION_CLASS)
    for op in ("accept_staged_output", "get_snapshot", "get_plan", "get_daily_trigger_outcomes"):
        assert route_allowed(_route(op), AUTOMATION_CLASS), op


# ------------------------------------------------------------------ research plan (T3)
class FakeTransport:
    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.keys: dict[str, dict[str, Any]] = {}

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/v1/plans/"):
            pid = path.rsplit("/", 1)[1]
            return (200, {"plan": self.plans[pid]}, {}) if pid in self.plans else (404, {"code": "NOT_FOUND"}, {})
        if method == "POST" and path == "/v1/portfolios":
            pf = self.keys.setdefault(body["idempotency_key"], {"portfolio_id": "pf_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K", "revision": 1, "synthetic": body["synthetic"]})
            return 201, pf, {}
        if method == "POST" and path.endswith("/plans"):
            plan = self.keys.setdefault(body["idempotency_key"], {"plan_id": "pl_01KDXG7S8RWX2V6Q2Q0ZCJ1V9K", "portfolio_id": path.split("/")[3]})
            self.plans[plan["plan_id"]] = plan
            return 201, plan, {}
        raise AssertionError((method, path))


class FakeSsm:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.puts = 0

    def get_parameter(self, Name: str) -> dict[str, Any]:
        if Name not in self.values:
            raise type("ParameterNotFound", (Exception,), {})(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name: str, Value: str, **_: Any) -> None:
        self.values[Name] = Value
        self.puts += 1


def test_research_plan_step_is_idempotent_and_synthetic() -> None:
    from scripts.research_plan import ensure_research_plan, research_plan_parameter

    t, ssm = FakeTransport(), FakeSsm()
    first = ensure_research_plan("beta", t, ssm)
    assert first["created"] is True and ssm.values[research_plan_parameter("beta")] == first["plan_id"]
    again = ensure_research_plan("beta", t, ssm)
    assert again == {"plan_id": first["plan_id"], "portfolio_id": first["portfolio_id"], "created": False}
    assert ssm.puts == 1 and sum(1 for m, p in t.calls if m == "POST") == 2
    assert research_plan_parameter("beta") == "/finplan/beta/financialplanning/config/research-plan-ref"
