"""Portfolios, plans and plan reads (tasks 4.3, 4.6, 4.8; API-03, API-08, API-11).

Operations (all ``(ctx, svc, ...)``; writes return an
:class:`~finplan_platform.core.repository.IdempotentOutcome`):

* :func:`create_portfolio` - synthetic-only in phase 1 (``synthetic: false`` ->
  ``OPERATION_NOT_PERMITTED``); minted ``portfolio_id``, ``revision`` 1.
* :func:`create_plan` - guarded by the portfolio's ``expected_revision`` (the portfolio head
  moves to ``revision + 1`` in the same transaction); the plan starts with head
  ``{current_version_id: null, revision: 1}`` and ``publication_revision`` 1.
* :func:`get_portfolio`, :func:`get_plan` (head, ``revision``, ``current_publication_id``,
  ``publication_revision`` and the current publication), :func:`list_plan_versions`
  (newest first, opaque page token, stable while new versions are committed: version IDs
  are ULIDs minted in commit order, so a new version always sorts before the first page).

Every state change is one transaction with its idempotency record and audit event(s).
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
import math
from typing import Any

from .audit import audit_event
from .context import OperationContext
from .contract_io import CREATE_PLAN_REQUEST, CREATE_PORTFOLIO_REQUEST, GET_BY_ID_REQUEST, require_valid
from .errors import PlatformError
from .repository import HeadMove, IdempotentOutcome, Mutation, PutNew, decode_page_token
from .services import Services
from .upgrade import check_declared_version, current_version, serve

__all__ = [
    "create_plan",
    "create_portfolio",
    "get_plan",
    "get_portfolio",
    "get_portfolio_state",
    "put_portfolio_state",
    "list_plan_versions",
    "plan_view",
    "version_summary",
]

INITIAL_REVISION = 1


# ===================================================================== portfolios
def create_portfolio(ctx: OperationContext, svc: Services, request: Mapping[str, Any]) -> IdempotentOutcome:
    req = dict(request)
    if req.get("synthetic") is False:
        # phase scope: non-synthetic portfolios are out of phase 1 (single-account decision does not lift this)
        raise PlatformError.not_permitted("non-synthetic portfolios are not permitted in phase 1", field="synthetic", phase=svc.cfg.phase)
    require_valid(req, CREATE_PORTFOLIO_REQUEST)
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        doc: dict[str, Any] = {
            "portfolio_id": ctx.new_id("portfolio_id"),
            "name": req["name"],
            "base_currency": req["base_currency"],
            "synthetic": True,
            "revision": INITIAL_REVISION,
            "created_at": ctx.now_ts(),
        }
        if "settings" in req:
            doc["settings"] = req["settings"]
        require_valid(doc, "portfolio")
        ev = audit_event(ctx, record_id=doc["portfolio_id"], record_type="portfolio", operation="create_portfolio", prior=None, new={"revision": INITIAL_REVISION}, synthetic=True)
        response = {**doc, "contract_version": current_version()}
        return Mutation(ops=[PutNew("portfolio", doc=doc, contract_version=current_version())], response=response, audit=[ev])

    return svc.repo.run_idempotent(ctx, operation="create_portfolio", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def get_portfolio(ctx: OperationContext, svc: Services, portfolio_id: str) -> dict[str, Any]:
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    return serve(svc.repo.require("portfolio", portfolio_id))


# ===================================================================== saved paper state
def _state_view(portfolio: Any) -> dict[str, Any]:
    state = portfolio.attrs.get("paper_state")
    if not state:
        raise PlatformError.precondition("saved paper portfolio state has not been initialized", reason="portfolio_state_missing", portfolio_id=portfolio.id)
    return {"portfolio_id": portfolio.id, "revision": int(portfolio.attrs["paper_state_revision"]), "paper_state": copy.deepcopy(state), "synthetic": True, "contract_version": current_version()}


def get_portfolio_state(ctx: OperationContext, svc: Services, portfolio_id: str) -> dict[str, Any]:
    require_valid({"portfolio_id": portfolio_id}, GET_BY_ID_REQUEST("portfolio_id"))
    return _state_view(svc.repo.require("portfolio", portfolio_id))


def put_portfolio_state(ctx: OperationContext, svc: Services, portfolio_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """Operator-only compare-and-set of a hypothetical book; inference never calls this write."""
    if ctx.caller.role_class not in ("platform", "operator"):
        raise PlatformError("FORBIDDEN", "only platform operators may update saved paper portfolio state")
    req = dict(request)
    if "portfolio_id" in req and req["portfolio_id"] != portfolio_id:
        raise PlatformError.validation("portfolio_id in the body does not match the path", pointer="/portfolio_id")
    req["portfolio_id"] = portfolio_id
    require_valid(req, "api/put-portfolio-state-request")
    check_declared_version(req.get("contract_version"))
    state = req["paper_state"]
    seen = set()
    for index, position in enumerate(state["positions"]):
        if position["instrument_id"] in seen:
            raise PlatformError.validation("paper positions must have unique instruments", pointer=f"/paper_state/positions/{index}/instrument_id")
        seen.add(position["instrument_id"])
        if not math.isfinite(position["quantity"]):
            raise PlatformError.validation("quantity must be finite", pointer=f"/paper_state/positions/{index}/quantity")
    for field in ("cash_balance", "high_watermark"):
        if not math.isfinite(state[field]):
            raise PlatformError.validation(f"{field} must be finite", pointer=f"/paper_state/{field}")
    if state["high_watermark"] < state["cash_balance"]:
        raise PlatformError.validation("high_watermark cannot be below current cash", pointer="/paper_state/high_watermark")
    if state["as_of"] > ctx.clock.now().date().isoformat():
        raise PlatformError.validation("paper state date cannot be in the future", pointer="/paper_state/as_of")

    def execute() -> Mutation:
        portfolio = svc.repo.require("portfolio", portfolio_id)
        if portfolio.doc.get("synthetic") is not True:
            raise PlatformError.not_permitted("only hypothetical portfolios support paper state", field="synthetic")
        if portfolio.doc.get("base_currency") != state["base_currency"]:
            raise PlatformError.validation("paper state currency must match the portfolio", pointer="/paper_state/base_currency")
        expected = req["expected_revision"]
        current = int(portfolio.attrs.get("paper_state_revision") or 0)
        if current != expected or (current == 0 and portfolio.attrs.get("paper_state")):
            raise PlatformError.conflict("expected_revision does not match the paper state revision", expected_revision=expected, current_revision=current)
        stored = copy.deepcopy(state)
        response = {"portfolio_id": portfolio_id, "revision": expected + 1, "paper_state": stored, "synthetic": True, "contract_version": current_version()}
        event = audit_event(ctx, record_id=portfolio_id, record_type="portfolio", operation="put_portfolio_state", prior={"revision": current, "paper_state": copy.deepcopy(portfolio.attrs.get("paper_state"))}, new={"revision": expected + 1, "paper_state": stored}, synthetic=True)
        move = HeadMove("portfolio", record_id=portfolio_id, expected_revision=expected, revision_attr="paper_state_revision", set_attrs={"paper_state": stored}, allow_missing_revision=expected == 0)
        history = []
        if ctx.env == "beta":
            from .portfolio_lifecycle import baseline_ops, history_op
            history = [*baseline_ops(ctx, svc, portfolio), history_op(ctx, svc, portfolio_id, expected + 1, stored, reason="operator_update")]
        return Mutation(ops=[move, *history], response=response, audit=[event])

    return svc.repo.run_idempotent(ctx, operation="put_portfolio_state", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


# ===================================================================== plans
def plan_view(record: Any) -> dict[str, Any]:
    """Contract ``plan`` record, including ``publication_revision`` (the publication head's
    revision, a contract field since 0.2.0, overlaid from the head attributes)."""
    return serve(record)


def create_plan(ctx: OperationContext, svc: Services, portfolio_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    req = {**dict(request)}
    if "portfolio_id" in req and req["portfolio_id"] != portfolio_id:
        raise PlatformError.validation("portfolio_id in the body does not match the path", pointer="/portfolio_id")
    req["portfolio_id"] = portfolio_id
    require_valid(req, CREATE_PLAN_REQUEST)
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        portfolio = svc.repo.require("portfolio", portfolio_id)
        current = int(portfolio.attrs.get("revision") or 0)
        expected = int(req["expected_revision"])
        if current != expected:
            raise PlatformError.conflict("expected_revision does not match the portfolio revision", record_type="portfolio", expected_revision=expected, current_revision=current)
        synthetic = bool(portfolio.doc.get("synthetic", True))
        doc: dict[str, Any] = {
            "plan_id": ctx.new_id("plan_id"),
            "portfolio_id": portfolio_id,
            "name": req["name"],
            "head": {"current_version_id": None, "revision": INITIAL_REVISION},
            "current_publication_id": None,
            "created_at": ctx.now_ts(),
            "synthetic": synthetic,
        }
        require_valid(doc, "plan")
        ops = [
            PutNew("plan", doc=doc, contract_version=current_version(), extra_attrs={"publication_revision": INITIAL_REVISION}),
            HeadMove("portfolio", record_id=portfolio_id, expected_revision=expected),
        ]
        audit = [
            audit_event(ctx, record_id=doc["plan_id"], record_type="plan", operation="create_plan", prior=None, new={"revision": INITIAL_REVISION, "publication_revision": INITIAL_REVISION}, portfolio_id=portfolio_id),
            audit_event(ctx, record_id=portfolio_id, record_type="portfolio", operation="create_plan", prior={"revision": expected}, new={"revision": expected + 1}, plan_id=doc["plan_id"]),
        ]
        response = {**doc, "publication_revision": INITIAL_REVISION, "portfolio_revision": expected + 1, "contract_version": current_version()}
        return Mutation(ops=ops, response=response, audit=audit)

    return svc.repo.run_idempotent(ctx, operation="create_plan", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def get_plan(ctx: OperationContext, svc: Services, plan_id: str) -> dict[str, Any]:
    """``tools/get-plan-response``: the plan head plus the current publication (or null)."""
    require_valid({"plan_id": plan_id}, "tools/get-plan-request")
    rec = svc.repo.require("plan", plan_id)
    plan = plan_view(rec)
    pub_id = plan.get("current_publication_id")
    current_publication = serve(svc.repo.require("publication", pub_id)) if pub_id else None
    return {"plan": plan, "current_publication": current_publication, "synthetic": bool(plan.get("synthetic", True))}


# ===================================================================== version list
def version_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: doc.get(k) for k in ("plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "created_at")}


def list_plan_versions(ctx: OperationContext, svc: Services, plan_id: str, *, page_size: int | None = None, next_token: str | None = None) -> dict[str, Any]:
    """``tools/list-plan-versions-response`` page (API-11)."""
    req: dict[str, Any] = {"plan_id": plan_id}
    if page_size is not None:
        req["page_size"] = page_size
    if next_token is not None:
        req["next_token"] = next_token
    require_valid(req, "tools/list-plan-versions-request")
    limit = min(int(page_size or svc.cfg.limits["page_size_max"]), int(svc.cfg.limits["page_size_max"]))
    plan = svc.repo.require("plan", plan_id)
    if next_token:
        start = decode_page_token(next_token)
        if (start.get("plan_id") or {}).get("S") != plan_id:
            raise PlatformError.validation("next_token does not belong to this plan's version list", pointer="/next_token")
    records, token = svc.repo.query_index("plan_version", "plan-index", plan_id, newest_first=True, limit=limit, page_token=next_token)
    versions = [version_summary(serve(r, overlay=False)) for r in records]
    return {"plan_id": plan_id, "versions": versions, "next_token": token, "synthetic": bool(plan.doc.get("synthetic", True))}
