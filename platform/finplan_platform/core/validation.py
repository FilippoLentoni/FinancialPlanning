"""Deterministic plan-version validation (task 5.1; API-05; plan-metadata-store MDS-04).

Ruleset (``validation.ruleset_version`` in configuration, ``plan-rules-v1``); every rule is
pure platform code over the stored record, the portfolio and the referenced snapshot's
catalog row and payload, so the same inputs always give the same findings, in the same order:

=====================  ===================================================================
rule                   finding when
=====================  ===================================================================
``schema``             the content fails the domain adapter's ``plan_content`` schema
``currency``           ``base_currency`` differs from the portfolio's base currency
``duplicate_instrument`` an instrument appears twice in ``allocation.weights``
``weights_sum``        sum(weights) + ``cash_weight`` differs from 1 by more than the
                       configured tolerance (default 1e-9) - accounting reconciliation
``no_negative_cash``   ``cash_weight`` < 0
``long_only``          ``constraints.long_only`` and a negative weight
``max_weight``         a weight above ``constraints.max_weight`` (+ tolerance)
``min_weight``         a listed weight below ``constraints.min_weight`` (- tolerance)
``snapshot_exists``    the referenced ``input_snapshot_id`` is not in the catalog or expired
``instrument_coverage`` an allocated instrument is not covered by the referenced snapshot
=====================  ===================================================================

``max_turnover`` needs a previous holding and is not evaluated by ``plan-rules-v1``.

Outcome: a ``pending_validation`` version moves to ``validated`` (no findings) or ``invalid``
(the findings are stored as contract error envelopes in ``validation_errors``) through one
conditional transition with its audit event; ``validation_ruleset_version`` and
``validated_at`` are recorded with it. Validating an already validated or invalid version
returns the stored result and records no new transition (API-05 "repeatable"). An ``invalid``
version can never become ``validated`` (MDS-04) and cannot be published (API-06).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from finplan_contracts.validate import validate as contract_validate

from .audit import audit_event
from .context import OperationContext
from .contract_io import require_valid
from .errors import PlatformError
from .repository import IdempotentOutcome, Mutation, Record, Transition
from .services import Services
from .snapshot_reads import snapshot_instruments
from .upgrade import check_declared_version, current_version

__all__ = ["Finding", "evaluate_rules", "validate_plan_version", "validate_version_now", "validation_result"]

Finding = dict[str, Any]


def _finding(code: str, rule: str, message: str, pointer: str = "", **details: Any) -> Finding:
    return {"code": code, "message": message, "pointer": pointer, "details": {"rule": rule, **details}}


def evaluate_rules(svc: Services, doc: Mapping[str, Any]) -> list[Finding]:
    """All findings for one plan version document (empty list = valid)."""
    tol = float(svc.cfg.validation["weight_sum_tolerance"])
    findings: list[Finding] = []
    content = doc.get("content")
    res = contract_validate(content, "plan-content")
    if not res.valid:
        for issue in res.issues:
            findings.append(_finding("VALIDATION_FAILED", "schema", "content does not match the plan content schema", "/content" + issue.pointer))
        return findings
    assert isinstance(content, dict)
    plan = svc.repo.get("plan", str(doc.get("plan_id")))
    if plan is not None:
        portfolio = svc.repo.get("portfolio", str(plan.doc.get("portfolio_id")))
        if portfolio is not None and portfolio.doc.get("base_currency") != content.get("base_currency"):
            findings.append(_finding("VALIDATION_FAILED", "currency", "base_currency differs from the portfolio's base currency", "/content/base_currency"))
    weights = list(content["allocation"]["weights"])
    seen: set[str] = set()
    for i, w in enumerate(weights):
        if w["instrument_id"] in seen:
            findings.append(_finding("VALIDATION_FAILED", "duplicate_instrument", "instrument appears more than once in the allocation", f"/content/allocation/weights/{i}/instrument_id", instrument_id=w["instrument_id"]))
        seen.add(w["instrument_id"])
    cash = float(content["allocation"].get("cash_weight", 0.0))
    total = math.fsum([float(w["weight"]) for w in weights] + [cash])
    if abs(total - 1.0) > tol:
        findings.append(_finding("VALIDATION_FAILED", "weights_sum", "allocation weights plus cash do not sum to 1 within the configured tolerance", "/content/allocation", total=round(total, 12), tolerance=tol))
    if cash < 0:
        findings.append(_finding("VALIDATION_FAILED", "no_negative_cash", "cash weight is negative", "/content/allocation/cash_weight"))
    constraints = content.get("constraints") or {}
    for i, w in enumerate(weights):
        wt = float(w["weight"])
        ptr = f"/content/allocation/weights/{i}/weight"
        if constraints.get("long_only") and wt < 0:
            findings.append(_finding("VALIDATION_FAILED", "long_only", "negative weight in a long-only plan", ptr, instrument_id=w["instrument_id"]))
        if "max_weight" in constraints and wt > float(constraints["max_weight"]) + tol:
            findings.append(_finding("VALIDATION_FAILED", "max_weight", "weight exceeds the max_weight constraint", ptr, instrument_id=w["instrument_id"], limit=constraints["max_weight"]))
        if "min_weight" in constraints and wt < float(constraints["min_weight"]) - tol:
            findings.append(_finding("VALIDATION_FAILED", "min_weight", "weight is below the min_weight constraint", ptr, instrument_id=w["instrument_id"], limit=constraints["min_weight"]))
    snap_id = str(doc.get("input_snapshot_id"))
    snap = svc.repo.get("snapshot_catalog", snap_id)
    if snap is None or snap.attrs.get("status") == "expired" or snap.doc.get("status") == "expired":
        findings.append(_finding("PRECONDITION_FAILED", "snapshot_exists", "the referenced input snapshot does not exist in this environment or has expired", "/input_snapshot_id"))
    else:
        covered = snapshot_instruments(svc, snap)
        for i, w in enumerate(weights):
            if w["instrument_id"] not in covered:
                findings.append(_finding("PRECONDITION_FAILED", "instrument_coverage", "instrument is not covered by the referenced snapshot", f"/content/allocation/weights/{i}/instrument_id", instrument_id=w["instrument_id"]))
    return findings


def _envelopes(ctx: OperationContext, findings: list[Finding]) -> list[dict[str, Any]]:
    """Findings as contract error envelopes (``plan-version.validation_errors``)."""
    out = []
    for f in findings:
        details = {**f["details"], "pointer": f["pointer"]}
        out.append({"code": f["code"], "message": f["message"], "retryable": False, "details": details, "correlation_id": ctx.correlation_id, "contract_version": current_version()})
    return out


def validation_result(doc: Mapping[str, Any]) -> dict[str, Any]:
    """``tools/validate-plan-version-response`` derived only from the stored record (repeatable)."""
    findings = []
    for env in doc.get("validation_errors") or []:
        details = dict(env.get("details") or {})
        pointer = details.pop("pointer", "")
        findings.append({"code": env["code"], "message": env["message"], "pointer": pointer, "details": details})
    out = {"plan_version_id": doc["plan_version_id"], "status": doc["status"], "findings": findings, "synthetic": bool(doc.get("synthetic", True))}
    if doc.get("validation_ruleset_version"):
        out["validation_ruleset_version"] = doc["validation_ruleset_version"]
    return out


def _transition(ctx: OperationContext, svc: Services, rec: Record) -> tuple[Transition, list[Any], dict[str, Any]]:
    findings = evaluate_rules(svc, rec.doc)
    status = "invalid" if findings else "validated"
    changes: dict[str, Any] = {"status": status, "validation_ruleset_version": svc.cfg.validation["ruleset_version"], "validated_at": ctx.now_ts()}
    if findings:
        changes["validation_errors"] = _envelopes(ctx, findings)
    new_doc = {**rec.doc, **changes}
    require_valid(new_doc, "plan-version")
    op = Transition("plan_version", record_id=rec.id, old_doc=rec.doc, new_doc=new_doc, allowed_fields=("validation_ruleset_version", "validated_at"))
    ev = audit_event(
        ctx,
        record_id=rec.id,
        record_type="plan_version",
        operation="validate_plan_version",
        prior={"status": rec.doc.get("status")},
        new={"status": status},
        validation_ruleset_version=changes["validation_ruleset_version"],
        findings=len(findings),
        checksum=rec.doc.get("checksum"),
    )
    return op, [ev], new_doc


def validate_version_now(ctx: OperationContext, svc: Services, plan_version_id: str) -> dict[str, Any]:
    """Validate without an idempotency record (internal callers: staged-output acceptance, Excel commit).

    Already-final versions return their stored result unchanged.
    """
    rec = svc.repo.require("plan_version", plan_version_id)
    if rec.doc.get("status") != "pending_validation":
        return validation_result(rec.doc)
    op, audit, new_doc = _transition(ctx, svc, rec)
    try:
        svc.repo.commit([op], audit=audit)
    except PlatformError as exc:
        if exc.code != "PRECONDITION_FAILED":
            raise
        return validation_result(svc.repo.require("plan_version", plan_version_id).doc)
    return validation_result(new_doc)


def validate_plan_version(ctx: OperationContext, svc: Services, plan_version_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """``POST /v1/plan-versions/{plan_version_id}/validate`` (idempotent, status-conditional)."""
    req = dict(request)
    if "plan_version_id" in req and req["plan_version_id"] != plan_version_id:
        raise PlatformError.validation("plan_version_id in the body does not match the path", pointer="/plan_version_id")
    req["plan_version_id"] = plan_version_id
    require_valid(req, "tools/validate-plan-version-request")
    check_declared_version(req.get("contract_version"))

    def execute() -> Mutation:
        rec = svc.repo.require("plan_version", plan_version_id)
        if rec.doc.get("status") != "pending_validation":
            # repeatable: the stored result, no new transition (only the idempotency record is written)
            return Mutation(ops=[], response=validation_result(rec.doc))
        op, audit, new_doc = _transition(ctx, svc, rec)
        return Mutation(ops=[op], response=validation_result(new_doc), audit=audit)

    try:
        return svc.repo.run_idempotent(ctx, operation="validate_plan_version", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)
    except PlatformError as exc:
        # a concurrent validation finished first: return its (identical, deterministic) stored result
        if exc.code == "PRECONDITION_FAILED" and exc.details.get("record_type") == "plan_version":
            return svc.repo.run_idempotent(ctx, operation="validate_plan_version", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)
        raise
