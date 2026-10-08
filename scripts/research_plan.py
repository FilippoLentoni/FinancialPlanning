"""Post-deploy step: the per-environment research plan (daily-recommendation-trigger, design T3; task 3.3).

Idempotently creates one **hypothetical** paper portfolio (``synthetic: true``: no real holdings) and one
plan on it, and records the plan ID at ``/finplan/<env>/financialplanning/config/research-plan-ref``.
The daily trigger stages its ``daily_recommendation`` output against this plan. Re-running the step
changes nothing: when the parameter names a plan the API can read, it returns it; otherwise the
creates use fixed idempotency keys, so a retry returns the original records.

Runs from ``scripts/stage_runner.py publish`` with the stage role (an operator principal of the API).
"""

from __future__ import annotations

from typing import Any

from finplan_contracts import ssm as contract_ssm

__all__ = ["PLAN_NAME", "PORTFOLIO_NAME", "ensure_research_plan", "research_plan_parameter"]

PORTFOLIO_NAME = "Research universe paper portfolio (hypothetical)"
PLAN_NAME = "Daily research recommendation (hypothetical)"


def research_plan_parameter(env: str) -> str:
    return contract_ssm.build(env, "financialplanning", "config", "research-plan-ref")


def _expect(resp: tuple[int, dict[str, Any], dict[str, str]] | tuple[int, dict[str, Any]], ok: tuple[int, ...], what: str) -> dict[str, Any]:
    code, body = resp[0], resp[1]
    if code not in ok:
        raise RuntimeError(f"{what} failed with HTTP {code} ({body.get('code', '')})")
    return body


def ensure_research_plan(env: str, transport: Any, ssm: Any) -> dict[str, Any]:
    """Return ``{"plan_id", "portfolio_id", "created"}``; creates at most one portfolio and one plan per environment."""
    name = research_plan_parameter(env)
    try:
        current = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
    except Exception as exc:
        if "ParameterNotFound" not in type(exc).__name__ + str(exc):
            raise
        current = ""
    if current:
        code, body = transport.call("GET", f"/v1/plans/{current}")[:2]
        if code == 200:
            plan = body.get("plan", body)
            return {"plan_id": current, "portfolio_id": plan.get("portfolio_id"), "created": False}
    pf = _expect(transport.call("POST", "/v1/portfolios", {"name": PORTFOLIO_NAME, "base_currency": "USD", "synthetic": True, "idempotency_key": f"research-portfolio-{env}-v1"}), (200, 201), "create research portfolio")
    pf = pf.get("portfolio", pf)
    if pf.get("synthetic") is not True:
        raise RuntimeError("the research portfolio must be synthetic (hypothetical, no real holdings)")
    plan = _expect(transport.call("POST", f"/v1/portfolios/{pf['portfolio_id']}/plans", {"name": PLAN_NAME, "expected_revision": pf["revision"], "idempotency_key": f"research-plan-{env}-v1"}), (200, 201), "create research plan")
    plan_id = plan["plan_id"]
    ssm.put_parameter(Name=name, Value=plan_id, Type="String", Overwrite=True, Description="Research plan of the daily recommendation trigger (hypothetical paper portfolio)")
    return {"plan_id": plan_id, "portfolio_id": pf["portfolio_id"], "created": True}
