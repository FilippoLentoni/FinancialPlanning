"""Publication of an exact validated plan version (task 5.2; API-06; MDS-06 audit).

``POST /v1/plans/{plan_id}/publications`` (:func:`publish`), request = contract
``tools/publish-plan-version-request`` (``plan_id``, ``plan_version_id``,
``expected_revision`` of the **publication head**, ``idempotency_key``):

* the version must belong to the plan (``VALIDATION_FAILED``) and be ``validated``
  (``PRECONDITION_FAILED`` with ``reason: version_not_validated`` otherwise);
* one transaction: a condition check that the version is still ``validated``, the new
  publication (minted ``publication_id``, the version's checksum, ``supersedes_publication_id``
  = the previous head), the publication-head move (``publication_revision`` = expected + 1,
  ``current_publication_id``), the idempotency record and the audit events (the publication
  event records ``publication_id``, ``plan_version_id``, checksum, caller and
  ``correlation_id``);
* the previous publication is never modified (it stays readable as it was);
* two concurrent publishes with the same ``expected_revision``: exactly one commits, the other
  gets ``CONFLICT``.

The response is the contract publication record plus ``publication_revision``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .audit import audit_event
from .context import OperationContext
from .contract_io import GET_BY_ID_REQUEST, require_valid
from .errors import PlatformError
from .repository import Check, Cond, HeadMove, IdempotentOutcome, Mutation, PutNew
from .services import Services
from .upgrade import check_declared_version, current_version, serve

__all__ = ["get_publication", "publish"]


def publish(ctx: OperationContext, svc: Services, plan_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    req = dict(request)
    if "plan_id" in req and req["plan_id"] != plan_id:
        raise PlatformError.validation("plan_id in the body does not match the path", pointer="/plan_id")
    req["plan_id"] = plan_id
    require_valid(req, "tools/publish-plan-version-request")
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        plan = svc.repo.require("plan", plan_id)
        pv_id = req["plan_version_id"]
        version = svc.repo.require("plan_version", pv_id)
        if version.doc.get("plan_id") != plan_id:
            raise PlatformError.validation("plan_version_id belongs to a different plan", pointer="/plan_version_id", field="plan_version_id")
        status = version.attrs.get("status") or version.doc.get("status")
        if status != "validated":
            raise PlatformError.precondition("only a validated plan version can be published", reason="version_not_validated", status=status)
        expected = int(req["expected_revision"])
        current = int(plan.attrs.get("publication_revision") or 0)
        if current != expected:
            raise PlatformError.conflict("expected_revision does not match the publication head revision", record_type="plan", expected_revision=expected, current_revision=current)
        previous = plan.attrs.get("current_publication_id")
        synthetic = bool(plan.doc.get("synthetic", True))
        doc: dict[str, Any] = {
            "publication_id": ctx.new_id("publication_id"),
            "plan_id": plan_id,
            "plan_version_id": pv_id,
            "plan_version_checksum": version.doc["checksum"],
            "plan_version_status": "validated",
            "supersedes_publication_id": previous,
            "published_at": ctx.now_ts(),
            "synthetic": synthetic,
        }
        require_valid(doc, "publication")
        ops = [
            Check(
                "plan_version",
                record_id=pv_id,
                condition=Cond.exists("pk") & Cond.eq("status", "validated"),
                error=lambda _cur: PlatformError.precondition("only a validated plan version can be published", reason="version_not_validated"),
            ),
            PutNew("publication", doc=doc, contract_version=current_version()),
            HeadMove("plan", record_id=plan_id, expected_revision=expected, revision_attr="publication_revision", set_attrs={"current_publication_id": doc["publication_id"]}),
        ]
        audit = [
            audit_event(
                ctx,
                record_id=doc["publication_id"],
                record_type="publication",
                operation="publish_plan_version",
                prior=None,
                new={"publication_id": doc["publication_id"]},
                plan_id=plan_id,
                plan_version_id=pv_id,
                checksum=doc["plan_version_checksum"],
                supersedes_publication_id=previous,
            ),
            audit_event(
                ctx,
                record_id=plan_id,
                record_type="plan",
                operation="publish_plan_version",
                prior={"publication_revision": expected, "current_publication_id": previous},
                new={"publication_revision": expected + 1, "current_publication_id": doc["publication_id"]},
                plan_version_id=pv_id,
                checksum=doc["plan_version_checksum"],
            ),
        ]
        response = {**doc, "publication_revision": expected + 1, "contract_version": current_version()}
        return Mutation(ops=ops, response=response, audit=audit)

    return svc.repo.run_idempotent(ctx, operation="publish_plan_version", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


def get_publication(ctx: OperationContext, svc: Services, publication_id: str) -> dict[str, Any]:
    require_valid({"publication_id": publication_id}, GET_BY_ID_REQUEST("publication_id"))
    return serve(svc.repo.require("publication", publication_id))
