"""Plan versions: root and child creation, checksum-bearing reads, download grants
(tasks 4.4, 4.5, 4.6; API-04, API-08, API-09; plan-metadata-store MDS-02/MDS-03).

Creation (:func:`create_version`, the ``POST /v1/plans/{plan_id}/versions`` operation):

1. The request is validated against a contract route schema: a root (no parent) against
   ``api/create-root-version-request`` (contracts 0.2.0), a child against
   ``tools/create-override-version-request`` (a FinanceLambdasTool body is accepted unchanged).
   A client-supplied ``plan_version_id`` is rejected (``VALIDATION_FAILED``): the platform
   mints every version ID.
2. A child names ``parent_plan_version_id``; the parent must belong to the same plan
   (otherwise ``VALIDATION_FAILED``) and the child inherits ``input_snapshot_id``,
   ``configuration_id`` and ``model_version`` from its parent (the override request cannot
   name them). Children created through this route are overrides (origin ``manual_override``,
   ``run_id`` null). Only a root names ``input_snapshot_id``: it names its full lineage
   (snapshot, configuration, model version and run), origin ``model_run``. Staged-output
   acceptance, which records a run's own lineage on a child, uses :func:`new_version_mutation`
   directly.
3. ``checksum`` = ``sha256:`` + SHA-256 of the RFC 8785 (JCS) canonical ``content`` (contract
   configuration rule; the website and the agent path agree byte-for-byte). ``no_effect`` is
   true when it equals the parent's checksum; the child is still created.
4. Artifact before metadata: the canonical content bytes are written write-once to
   ``plans/<plan_id>/<plan_version_id>/content.json`` (their SHA-256 *is* the version
   checksum), then one transaction commits the version (new ID), the plan head move
   (``expected_revision``), the idempotency record and the audit events. A crash in between
   leaves an invisible orphan the daily sweep removes after 24 h.

:func:`new_version_mutation` is the shared building block for the other creators
(staged-output acceptance: origin ``model_run``; Excel import: origin ``excel_import`` with an
``excel_source`` trusted reference). They call it inside their own
:meth:`~finplan_platform.core.repository.MetadataRepository.run_idempotent` and may add
``extra_ops`` (for example the ``staged_output`` outcome row) to the same transaction.

Reads (:func:`get_plan_version`) return the record with its checksum, lineage and status, the
content trusted reference, and, on request, a time-limited download grant. The stored
content is re-hashed on every read (``INTERNAL`` on a mismatch). No bucket name or key
appears in any field except inside the presigned grant URL itself.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from finplan_contracts.canonical import canonicalize, sha256_hex

from .artifacts import key_plan_content
from .audit import AuditEvent, audit_event
from .context import OperationContext
from .contract_io import GET_BY_ID_REQUEST, create_version_request_schema, require_valid
from .errors import PlatformError
from .repository import IdempotentOutcome, Mutation, Op, version_create_mutation
from .services import Services
from .upgrade import check_declared_version, current_version, serve

__all__ = [
    "canonical_content_bytes",
    "content_checksum",
    "content_download",
    "create_version",
    "get_plan_version",
    "load_version",
    "new_version_mutation",
]

PLAN_CONTENT_KIND = "plan_content"
#: FinanceLambdasTool role classes (see ``handlers.api``); they may create override children only.
TOOL_ROLE_CLASSES = frozenset({"reader", "submitter", "plan-writer"})


def canonical_content_bytes(content: Any) -> bytes:
    return canonicalize(content)


def content_checksum(content: Any) -> str:
    """``sha256:`` + hex SHA-256 of the RFC 8785 canonical content (design P4)."""
    return "sha256:" + sha256_hex(canonicalize(content))


# ===================================================================== creation
def new_version_mutation(
    ctx: OperationContext,
    svc: Services,
    *,
    plan_id: str,
    expected_revision: int,
    content: Mapping[str, Any],
    domain: str,
    domain_schema_version: str,
    origin: str,
    parent_plan_version_id: str | None = None,
    input_snapshot_id: str | None = None,
    configuration_id: str | None = None,
    model_version: str | None = None,
    run_id: str | None = None,
    source_artifact: Mapping[str, Any] | None = None,
    reason: str | None = None,
    operation: str = "create_plan_version",
    extra_ops: Iterable[Op] = (),
    extra_audit: Iterable[AuditEvent] = (),
    response_extra: Mapping[str, Any] | None = None,
) -> Mutation:
    """Build (artifact written, nothing committed) the version-create transaction.

    Raises ``NOT_FOUND`` (plan or parent missing), ``VALIDATION_FAILED`` (parent of another
    plan, lineage missing, content invalid) or ``CONFLICT`` (stale ``expected_revision``,
    checked early so no artifact is written for a request that cannot commit; the
    transaction re-checks it).
    """
    if origin not in ("model_run", "manual_override", "excel_import"):
        raise PlatformError.validation("unknown origin", pointer="/origin")
    plan = svc.repo.require("plan", plan_id)
    current = int(plan.attrs.get("revision") or 0)
    if current != int(expected_revision):
        raise PlatformError.conflict("expected_revision does not match the plan head revision", record_type="plan", expected_revision=int(expected_revision), current_revision=current)
    parent: dict[str, Any] | None = None
    if parent_plan_version_id is not None:
        prec = svc.repo.get("plan_version", parent_plan_version_id)
        if prec is None:
            raise PlatformError.not_found("parent plan version not found", record_type="plan_version", field="parent_plan_version_id")
        parent = prec.doc
        if parent.get("plan_id") != plan_id:
            raise PlatformError.validation("parent_plan_version_id belongs to a different plan", pointer="/parent_plan_version_id", field="parent_plan_version_id")
    elif origin in ("manual_override", "excel_import"):
        raise PlatformError.validation("overrides and imports always create a child version; name parent_plan_version_id", pointer="/parent_plan_version_id")

    def lineage(name: str, given: str | None) -> str | None:
        if given is not None:
            return given
        return parent.get(name) if parent else None

    body = copy.deepcopy(dict(content))
    checksum = content_checksum(body)
    synthetic = bool(plan.doc.get("synthetic", True))
    pv_id = ctx.new_id("plan_version_id")
    doc: dict[str, Any] = {
        "plan_version_id": pv_id,
        "plan_id": plan_id,
        "parent_plan_version_id": parent_plan_version_id,
        "input_snapshot_id": lineage("input_snapshot_id", input_snapshot_id),
        "configuration_id": lineage("configuration_id", configuration_id),
        "model_version": lineage("model_version", model_version),
        "run_id": run_id if origin == "model_run" else None,
        "origin": origin,
        "status": "pending_validation",
        "checksum": checksum,
        "domain": domain,
        "domain_schema_version": domain_schema_version,
        "content": body,
        "no_effect": bool(parent is not None and parent.get("checksum") == checksum),
        "created_at": ctx.now_ts(),
        "synthetic": synthetic,
    }
    if source_artifact is not None:
        doc["source_artifact"] = dict(source_artifact)
    for name in ("input_snapshot_id", "configuration_id", "model_version"):
        if doc[name] is None:
            raise PlatformError.validation(f"{name} is required for a root version", pointer=f"/{name}", field=name)
    # validate the record before writing anything (content via the domain adapter schema)
    pre = {**doc, "content_ref": {"artifact_id": "art_pending", "owner": "financialplanning", "kind": PLAN_CONTENT_KIND, "checksum": checksum, "content_type": "application/json"}}
    require_valid(pre, "plan-version")

    # artifact first (write-once); its SHA-256 is the version checksum
    stored = svc.store.put_once("plans", key_plan_content(plan_id, pv_id), canonical_content_bytes(body), "application/json")
    if stored.checksum != checksum:  # pragma: no cover - canonical bytes hash to the checksum by construction
        raise PlatformError.internal("content artifact checksum mismatch")
    doc["content_ref"] = stored.to_ref(ctx.new_id("artifact_id"), PLAN_CONTENT_KIND, synthetic=synthetic, domain=domain)
    require_valid(doc, "plan-version")

    response: dict[str, Any] = {
        "plan_version_id": pv_id,
        "plan_id": plan_id,
        "parent_plan_version_id": parent_plan_version_id,
        "origin": origin,
        "status": doc["status"],
        "checksum": checksum,
        "no_effect": doc["no_effect"],
        "revision": int(expected_revision) + 1,
        "input_snapshot_id": doc["input_snapshot_id"],
        "content_ref": doc["content_ref"],
        "contract_version": current_version(),
        "synthetic": synthetic,
        **dict(response_extra or {}),
    }
    mutation = version_create_mutation(
        svc.repo,
        ctx,
        plan_id=plan_id,
        expected_revision=int(expected_revision),
        version_doc=doc,
        contract_version=current_version(),
        response=response,
        operation=operation,
        extra_ops=extra_ops,
    )
    mutation.audit.append(
        audit_event(
            ctx,
            record_id=pv_id,
            record_type="plan_version",
            operation=operation,
            prior=None,
            new={"status": "pending_validation"},
            plan_id=plan_id,
            parent_plan_version_id=parent_plan_version_id,
            origin=origin,
            checksum=checksum,
            no_effect=doc["no_effect"],
            **({"reason": reason} if reason else {}),
        )
    )
    mutation.audit.extend(extra_audit)
    return mutation


def create_version(ctx: OperationContext, svc: Services, plan_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """``POST /v1/plans/{plan_id}/versions``: root (``model_run``) or child (``manual_override``)."""
    req = dict(request)
    if "plan_version_id" in req:
        raise PlatformError.validation("plan_version_id is minted by the platform and must not be supplied", pointer="/plan_version_id", field="plan_version_id")
    if "plan_id" in req and req["plan_id"] != plan_id:
        raise PlatformError.validation("plan_id in the body does not match the path", pointer="/plan_id", field="plan_id")
    req["plan_id"] = plan_id
    require_valid(req, create_version_request_schema(req))
    check_declared_version(req.get("contract_version"))
    parent = req.get("parent_plan_version_id")
    origin = req.get("origin") or ("manual_override" if parent else "model_run")
    if ctx.caller.role_class in TOOL_ROLE_CLASSES and origin != "manual_override":
        # tool roles create overrides only; model-run versions carry FinanceModel lineage that
        # only platform-side callers (staged-output acceptance, operators, the website path) record
        raise PlatformError.forbidden("tool roles may only create manual override child versions", role_class=ctx.caller.role_class, origin=origin)

    def execute() -> Mutation:
        return new_version_mutation(
            ctx,
            svc,
            plan_id=plan_id,
            expected_revision=int(req["expected_revision"]),
            content=req["content"],
            domain=req["domain"],
            domain_schema_version=req["domain_schema_version"],
            origin=origin,
            parent_plan_version_id=parent,
            input_snapshot_id=req.get("input_snapshot_id"),
            configuration_id=req.get("configuration_id"),
            model_version=req.get("model_version"),
            run_id=req.get("run_id"),
            reason=req.get("reason"),
        )

    return svc.repo.run_idempotent(ctx, operation="create_plan_version", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)


# ===================================================================== reads
def load_version(svc: Services, plan_version_id: str) -> tuple[Any, dict[str, Any]]:
    """(record, served doc) with the content checksum re-verified."""
    rec = svc.repo.require("plan_version", plan_version_id)
    doc = serve(rec)
    if "content" in doc and content_checksum(doc["content"]) != doc.get("checksum"):
        raise PlatformError.internal("stored plan version failed checksum verification", record_type="plan_version")
    return rec, doc


def content_download(ctx: OperationContext, svc: Services, doc: Mapping[str, Any]) -> dict[str, Any]:
    """A time-limited download grant for the version's content artifact (API-09).

    The stored object is checked first (exists, recorded SHA-256 equals the reference
    checksum); the grant itself is a presigned GET valid for the configured TTL.
    """
    ref = doc.get("content_ref") or {}
    key = key_plan_content(str(doc["plan_id"]), str(doc["plan_version_id"]))
    store = svc.store
    head = store.head("plans", key)
    if head is None:
        raise PlatformError.not_found("plan content artifact is not available (expired or not yet written)", record_type="plan_version")
    if ref.get("checksum") and head.checksum != ref["checksum"]:
        raise PlatformError.internal("plan content artifact failed checksum verification")
    grant = store.download_grant("plans", key, int(svc.cfg.limits["download_grant_ttl_seconds"]))
    return {"artifact_id": ref.get("artifact_id"), "checksum": head.checksum, "url": grant["url"], "expires_at": grant["expires_at"]}


def get_plan_version(ctx: OperationContext, svc: Services, plan_version_id: str, *, download: bool = False) -> dict[str, Any]:
    """``tools/get-plan-version-response``: record + content trusted reference (+ download grant)."""
    require_valid({"plan_version_id": plan_version_id}, GET_BY_ID_REQUEST("plan_version_id"))
    _rec, doc = load_version(svc, plan_version_id)
    out: dict[str, Any] = {"plan_version": doc, "synthetic": bool(doc.get("synthetic", True))}
    if doc.get("content_ref"):
        out["content_ref"] = doc["content_ref"]
    if download:
        out["download_grant"] = content_download(ctx, svc, doc)
    return out
