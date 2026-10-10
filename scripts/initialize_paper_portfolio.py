"""Initialize the explicitly approved beta $10,000 paper book from its existing plan.

This is an operator action, never a recommendation side effect. Existing state is
returned unchanged. Quantities are fractional and use completed, unadjusted close
prices from one approved snapshot. No brokerage or execution endpoint is called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from typing import Any
from urllib.parse import urlencode

from scripts.research_plan import research_plan_parameter

INITIAL_CAPITAL = 10_000.0


def _read(transport: Any, path: str) -> dict[str, Any]:
    code, body = transport.call("GET", path)[:2]
    if code != 200:
        raise RuntimeError(f"paper initialization read failed: HTTP {code} ({body.get('code', '')})")
    return body


def initialize_paper_portfolio(env: str, transport: Any, ssm: Any, *, dataset_id: str, apply: bool = False) -> dict[str, Any]:
    """Return {created, state}; apply=False previews the exact proposed write."""
    if env != "beta":
        raise ValueError("this approved paper initialization is beta only")
    plan_id = ssm.get_parameter(Name=research_plan_parameter(env))["Parameter"]["Value"].strip()
    plan_view = _read(transport, f"/v1/plans/{plan_id}")
    plan = plan_view["plan"]
    if plan.get("synthetic") is not True:
        raise ValueError("the research plan must be explicitly hypothetical")
    portfolio_id = plan["portfolio_id"]
    path = f"/v1/portfolios/{portfolio_id}/state"
    code, body = transport.call("GET", path)[:2]
    if code == 200:
        return {"created": False, "state": body}
    if code != 422 or body.get("code") != "PRECONDITION_FAILED" or body.get("details", {}).get("reason") != "portfolio_state_missing":
        raise RuntimeError(f"saved paper state lookup failed: HTTP {code} ({body.get('code', '')})")
    publication = plan_view.get("current_publication")
    if not publication:
        raise ValueError("paper initialization requires the existing published research plan")
    version_id = publication["plan_version_id"]
    version = _read(transport, f"/v1/plan-versions/{version_id}")["plan_version"]
    if version.get("plan_id") != plan_id or version.get("synthetic") is not True:
        raise ValueError("published plan lineage is not the hypothetical research plan")
    content = version["content"]
    if content.get("base_currency") != "USD":
        raise ValueError("paper initialization supports USD only")
    allocation = content["allocation"]
    cash_weight = allocation.get("cash_weight", 0.0)
    weights: dict[str, float] = {}
    for position in allocation["weights"]:
        instrument = position["instrument_id"]
        weight = position["weight"]
        if instrument in weights or not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("published allocation weights must be unique and finite")
        weights[instrument] = weight
    if not math.isfinite(cash_weight) or not 0 <= cash_weight <= 1 or not math.isclose(sum(weights.values()) + cash_weight, 1.0, abs_tol=1e-8):
        raise ValueError("published allocation must sum to one including cash")
    snapshot = _read(transport, "/v1/snapshots/latest?" + urlencode({"dataset_id": dataset_id}))["snapshot"]
    if snapshot.get("status") != "approved":
        raise ValueError("paper initialization requires an approved snapshot")
    snapshot_id = snapshot["input_snapshot_id"]
    as_of = snapshot["coverage"]["end"]
    prices: dict[str, float] = {}
    next_token = None
    while True:
        query: dict[str, Any] = {"start_date": as_of, "end_date": as_of}
        if next_token:
            query["next_token"] = next_token
        page = _read(transport, f"/v1/snapshots/{snapshot_id}/observations?" + urlencode(query))
        for row in page.get("observations", []):
            instrument = row["instrument_id"]
            if instrument not in weights or row.get("kind") != "completed_daily" or row.get("session_date") != as_of:
                continue
            price = row["close"]
            if instrument in prices or not math.isfinite(price) or price <= 0:
                raise ValueError("approved close prices must be unique, finite and positive")
            prices[instrument] = price
        next_token = page.get("next_token")
        if not next_token:
            break
    if set(weights) - prices.keys():
        raise ValueError("approved snapshot lacks completed close prices for plan instruments")
    paper_state = {"positions": [{"instrument_id": instrument, "quantity": INITIAL_CAPITAL * weight / prices[instrument]} for instrument, weight in sorted(weights.items())], "cash_balance": INITIAL_CAPITAL * cash_weight, "high_watermark": INITIAL_CAPITAL, "as_of": as_of, "base_currency": "USD", "mode": "paper", "source": "paper_initialization", "source_plan_version_id": version_id, "input_snapshot_id": snapshot_id}
    digest = hashlib.sha256(json.dumps(paper_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = {"paper_state": paper_state, "expected_revision": 0, "idempotency_key": f"paper_init_{digest}"}
    if not apply:
        return {"created": False, "preview": True, "portfolio_id": portfolio_id, "request": request}
    code, response = transport.call("PUT", path, request)[:2]
    if code == 409:
        # Another operator initialized concurrently; preserve its committed book.
        return {"created": False, "state": _read(transport, path)}
    if code != 200:
        raise RuntimeError(f"paper initialization write failed: HTTP {code} ({response.get('code', '')})")
    return {"created": True, "state": response}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["beta"], required=True)
    parser.add_argument("--apply", action="store_true", help="perform the one-time operator write; default previews")
    args = parser.parse_args()
    import boto3
    from finplan_platform.core.config import load_config
    from finplan_platform.handlers.daily_trigger import SigV4Json
    cfg = load_config(args.env)
    session = boto3.Session(region_name=cfg.region)
    ssm = session.client("ssm")
    endpoint = ssm.get_parameter(Name=f"/finplan/{args.env}/financialplanning/api/plan-endpoint")["Parameter"]["Value"]
    if cfg.universe is None:
        raise RuntimeError("the beta research universe is not configured")
    transport = SigV4Json(endpoint, cfg.region, session.get_credentials())
    result = initialize_paper_portfolio(args.env, transport, ssm, dataset_id=cfg.universe.dataset_id, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
