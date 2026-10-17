"""Executions, recorded separately from publication (task 5.3; API-07).

``POST /v1/publications/{publication_id}/executions`` (:func:`record_execution`):

* ``mode`` must be ``paper`` or ``simulated``; anything else, ``live`` included, fails with
  ``OPERATION_NOT_PERMITTED`` (the contract ``execution.mode`` definition carries that code);
* the execution is its own record (minted ``execution_id``, status ``recorded``) referencing
  exactly the given ``publication_id``; the publication, the plan version and the plan heads
  are never written (the transaction holds only a condition check on the publication, the
  new execution, the idempotency record and the audit event);
* ``publication_superseded`` is computed at write time from the plan's publication head: true
  when the referenced publication is no longer the plan's current one.

Executions are platform-only in phase 1: no FinanceLambdasTool role class and no FinanceModel
principal may call this route (handler route grants and the API resource policy).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .audit import audit_event
from .context import OperationContext
from .contract_io import GET_BY_ID_REQUEST, RECORD_EXECUTION_REQUEST, require_valid
from .errors import PlatformError
from .repository import Check, IdempotentOutcome, Mutation, PutNew
from .services import Services
from .upgrade import check_declared_version, current_version, serve

__all__ = ["get_execution", "record_execution"]


def record_execution(ctx: OperationContext, svc: Services, publication_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    req = dict(request)
    if "publication_id" in req and req["publication_id"] != publication_id:
        raise PlatformError.validation("publication_id in the body does not match the path", pointer="/publication_id")
    req["publication_id"] = publication_id
    require_valid(req, RECORD_EXECUTION_REQUEST)
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        pub = svc.repo.require("publication", publication_id)
        plan = svc.repo.require("plan", str(pub.doc["plan_id"]))
        superseded = plan.attrs.get("current_publication_id") != publication_id
        doc: dict[str, Any] = {
            "execution_id": ctx.new_id("execution_id"),
            "publication_id": publication_id,
            "plan_id": pub.doc["plan_id"],
            "plan_version_id": pub.doc["plan_version_id"],
            "mode": req["mode"],
            "status": "recorded",
            "publication_superseded": superseded,
            "requested_at": ctx.now_ts(),
            "synthetic": bool(pub.doc.get("synthetic", True)),
        }
        if req.get("ledger") is not None:
            require_valid(req["ledger"],"domain-envelope")
            if req["ledger"].get("domain") != "finance" or req["ledger"].get("payload_kind") != "execution_ledger":
                raise PlatformError.validation("a finance execution ledger is required",pointer="/ledger")
            doc["ledger"] = dict(req["ledger"])
        if req.get("note"):
            doc["note"] = req["note"]
        require_valid(doc, "execution")
        ops = [Check("publication", record_id=publication_id), PutNew("execution", doc=doc, contract_version=current_version())]
        ev = audit_event(
            ctx,
            record_id=doc["execution_id"],
            record_type="execution",
            operation="record_execution",
            prior=None,
            new={"status": "recorded"},
            publication_id=publication_id,
            plan_version_id=doc["plan_version_id"],
            mode=doc["mode"],
            publication_superseded=superseded,
        )
        return Mutation(ops=ops, response={**doc, "contract_version": current_version()}, audit=[ev])

    return svc.repo.run_idempotent(ctx, operation="record_execution", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def get_execution(ctx: OperationContext, svc: Services, execution_id: str) -> dict[str, Any]:
    require_valid({"execution_id": execution_id}, GET_BY_ID_REQUEST("execution_id"))
    return serve(svc.repo.require("execution", execution_id))


def list_executions(ctx, svc, publication_id, *, page_size=None, next_token=None):
    from .repository import decode_page_token
    require_valid({"publication_id":publication_id, **({"page_size":page_size} if page_size is not None else {}), **({"next_token":next_token} if next_token else {})}, "tools/list-executions-request")
    svc.repo.require("publication",publication_id)
    if next_token and (decode_page_token(next_token).get("publication_id") or {}).get("S") != publication_id:
        raise PlatformError.validation("page token belongs to another publication",pointer="/next_token")
    rows,token=svc.repo.query_index("execution","publication-index",publication_id,limit=min(int(page_size or 100),100),page_token=next_token)
    return {"executions":[serve(r) for r in rows],"next_token":token}
