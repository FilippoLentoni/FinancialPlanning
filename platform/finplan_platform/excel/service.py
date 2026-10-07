"""Excel export and import operations (tasks 8.1-8.4; XLS-01..XLS-06; design P4, P8).

Routes (registered in :mod:`finplan_platform.handlers.api`, delegated here by name):

* ``POST /v1/plan-versions/{plan_version_id}/exports`` -> :func:`export_plan_version`
* ``POST /v1/plans/{plan_id}/imports`` -> :func:`issue_import_grant`
* ``POST /v1/plans/{plan_id}/imports/{import_id}/commit`` -> :func:`commit_import`

**Export** writes the ``xlsx-plan-v1`` workbook for one version (meta: plan, base version,
base checksum, the current head revision; allocations incl. the ``CASH`` row) write-once into
the ``plans`` bucket (``<plan_id>/<plan_version_id>/export-xlsx-plan-v1-<sha16>.xlsx``) and
returns a trusted reference (kind ``plan_export``) plus a time-limited download grant.

**Import** is two calls. The grant call mints ``import_id`` and returns a presigned POST
(``uploads/incoming/<import_id>.xlsx`` in ``raw``, size range, at most 15 minutes); the grant is
recorded as an audit event of the import (record type ``excel_import``). The commit call:

1. checks the grant belongs to the plan and the upload exists (size from the object head);
2. reads the upload and retains it write-once as ``uploads/excel/<sha256>.xlsx`` (lineage,
   trusted reference kind ``excel_source``, checksum = the upload's SHA-256);
3. **idempotency**: scope (caller, env, ``commit_excel_import``) + key; the request hash covers
   the plan and the uploaded file's SHA-256, so the same file under the same key returns the
   original result, whichever ``import_id`` carried it (XLS-06);
4. pre-checks and parses the package (:mod:`.package`, :mod:`.reader`): no macro, ActiveX, OLE,
   external link, DTD/entity, size or ratio problem; data-only cells, formulas rejected;
5. maps it onto the canonical contract: the base version must belong to the plan and its
   checksum must equal ``base_checksum``; every instrument must be covered by the base
   version's snapshot (otherwise ``VALIDATION_FAILED`` naming sheet, row and instrument);
   the plan head must still be at the workbook's ``expected_revision`` (otherwise ``CONFLICT``
   naming the current head version);
6. creates the child through the shared create-version operation
   (:func:`~finplan_platform.core.versions.new_version_mutation`, origin ``excel_import``,
   ``source_artifact`` = the ``excel_source`` reference), so content, checksum, ``no_effect``,
   head move, idempotency record and audit events are exactly the API's. The response is the
   contract ``import-report`` (outcome ``accepted``).

Every rejection after the upload is read is **recorded**: the source stays retained, an
``import-report`` (outcome ``rejected``, findings) is written write-once to the ``reports``
bucket when the base version is known, and an audit event ``reject_excel_import`` records the
reason, findings and source checksum. The call then fails with the error (``VALIDATION_FAILED``
or ``CONFLICT``); no version is created.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from finplan_contracts.canonical import canonical_text

from ..core.artifacts import (
    ArtifactExists,
    ArtifactNotFound,
    check_upload_grant,
    key_excel_incoming,
    key_excel_source,
    key_plan_export,
    key_report,
    sha256_checksum,
)
from ..core.audit import audit_event
from ..core.clock import parse_timestamp, to_timestamp
from ..core.context import OperationContext
from ..core.contract_io import ref, require_valid, require_valid_response
from ..core.errors import PlatformError
from ..core.repository import IdempotentOutcome, Mutation
from ..core.services import Services
from ..core.snapshot_reads import snapshot_instruments
from ..core.upgrade import check_declared_version, current_version
from ..core.versions import load_version, new_version_mutation
from .package import ImportRejected, PackageLimits, open_package
from .reader import ParsedWorkbook, parse_workbook
from .template import ALLOCATIONS_SHEET, CASH_INSTRUMENT_ID, META_SHEET, TEMPLATE_VERSION, XLSX_CONTENT_TYPE, TemplateSpec
from .writer import build_workbook

__all__ = [
    "COMMIT_IMPORT_REQUEST",
    "COMMIT_IMPORT_RESPONSE",
    "EXPORT_RESPONSE",
    "IMPORT_GRANT_RESPONSE",
    "EXPORT_REQUEST",
    "IMPORT_GRANT_REQUEST",
    "commit_import",
    "content_from_workbook",
    "export_plan_version",
    "import_workbook_bytes",
    "issue_import_grant",
]

_IMPORT_ID = {"type": "string", "pattern": "^imp_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_IDEM = ref("core/v1/idempotency.json#/$defs/idempotency_key")
_CV = ref("core/v1/common.json#/$defs/contract_version")

EXPORT_REQUEST: dict[str, Any] = {
    "x-platform-name": "export-plan-version-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {
        "plan_version_id": ref("core/v1/identifiers.json#/$defs/plan_version_id"),
        "template_version": {"const": TEMPLATE_VERSION},
        "idempotency_key": _IDEM,
        "contract_version": _CV,
    },
    "required": ["plan_version_id", "idempotency_key"],
    "additionalProperties": False,
}
IMPORT_GRANT_REQUEST: dict[str, Any] = {
    "x-platform-name": "issue-import-grant-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {"plan_id": ref("core/v1/identifiers.json#/$defs/plan_id"), "idempotency_key": _IDEM, "contract_version": _CV},
    "required": ["plan_id", "idempotency_key"],
    "additionalProperties": False,
}
COMMIT_IMPORT_REQUEST: dict[str, Any] = {
    "x-platform-name": "commit-import-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {
        "plan_id": ref("core/v1/identifiers.json#/$defs/plan_id"),
        "import_id": _IMPORT_ID,
        "file_name": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[^/\\\\]+$"},
        "idempotency_key": _IDEM,
        "contract_version": _CV,
    },
    "required": ["plan_id", "import_id", "idempotency_key"],
    "additionalProperties": False,
}
_TS = ref("core/v1/common.json#/$defs/timestamp")
_GRANT_URL = {"type": "string", "pattern": "^https://"}
EXPORT_RESPONSE: dict[str, Any] = {
    "x-platform-name": "export-plan-version-response",
    "type": "object",
    "properties": {
        "plan_version_id": ref("core/v1/identifiers.json#/$defs/plan_version_id"),
        "plan_id": ref("core/v1/identifiers.json#/$defs/plan_id"),
        "template_version": {"const": TEMPLATE_VERSION},
        "base_checksum": ref("core/v1/common.json#/$defs/checksum"),
        "expected_revision": ref("core/v1/concurrency.json#/$defs/revision"),
        "exported_at": _TS,
        "export_ref": ref("core/v1/artifact-ref.json"),
        "download_grant": {
            "type": "object",
            "properties": {
                "artifact_id": ref("core/v1/artifact-ref.json#/$defs/artifact_id"),
                "checksum": ref("core/v1/common.json#/$defs/checksum"),
                "url": _GRANT_URL,
                "expires_at": _TS,
            },
            "required": ["artifact_id", "checksum", "url", "expires_at"],
            "additionalProperties": False,
        },
        "synthetic": ref("core/v1/common.json#/$defs/synthetic"),
        "contract_version": _CV,
    },
    "required": ["plan_version_id", "plan_id", "template_version", "base_checksum", "expected_revision", "exported_at", "export_ref", "download_grant"],
    "additionalProperties": False,
}
IMPORT_GRANT_RESPONSE: dict[str, Any] = {
    "x-platform-name": "issue-import-grant-response",
    "type": "object",
    "properties": {
        "import_id": _IMPORT_ID,
        "plan_id": ref("core/v1/identifiers.json#/$defs/plan_id"),
        "template_version": {"const": TEMPLATE_VERSION},
        "expires_at": _TS,
        "max_bytes": {"type": "integer", "minimum": 1},
        "upload": {
            "type": "object",
            "properties": {"url": _GRANT_URL, "fields": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["url", "fields"],
            "additionalProperties": False,
        },
    },
    "required": ["import_id", "plan_id", "template_version", "expires_at", "max_bytes", "upload"],
    "additionalProperties": False,
}
#: An accepted import: the contract import report plus the created child version's head fields.
COMMIT_IMPORT_RESPONSE: dict[str, Any] = {
    "x-platform-name": "commit-import-response",
    "allOf": [ref("core/v1/import-report.json")],
    "type": "object",
    "properties": {
        "import_id": _IMPORT_ID,
        "plan_version": {
            "type": "object",
            "properties": {
                "plan_version_id": ref("core/v1/identifiers.json#/$defs/plan_version_id"),
                "parent_plan_version_id": ref("core/v1/identifiers.json#/$defs/plan_version_id_or_null"),
                "origin": ref("core/v1/plan-version.json#/$defs/origin"),
                "status": ref("core/v1/plan-version.json#/$defs/status"),
                "checksum": ref("core/v1/common.json#/$defs/checksum"),
                "no_effect": {"type": "boolean"},
                "revision": ref("core/v1/concurrency.json#/$defs/revision"),
            },
            "required": ["plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "no_effect", "revision"],
            "additionalProperties": False,
        },
        "contract_version": _CV,
    },
    "required": ["import_id", "plan_version"],
}
_TOOL_CLASSES = frozenset({"reader", "submitter", "plan-writer", "financemodel-job", "financemodel-job-api"})


def _path_params(request: Mapping[str, Any], **path: str) -> dict[str, Any]:
    req = dict(request)
    for name, value in path.items():
        if name in req and req[name] != value:
            raise PlatformError.validation(f"{name} in the body does not match the path", pointer=f"/{name}", field=name)
        req[name] = value
    return req


def _deny_tools(ctx: OperationContext) -> None:
    if ctx.caller.role_class in _TOOL_CLASSES:
        raise PlatformError.forbidden("Excel import and export are not available to tool or model roles in phase 1", role_class=ctx.caller.role_class)


def _export_key(plan_id: str, plan_version_id: str, checksum: str) -> str:
    return key_plan_export(plan_id, plan_version_id, f"{TEMPLATE_VERSION}-{checksum.removeprefix('sha256:')[:16]}")


# ===================================================================== export (8.1)
def export_plan_version(ctx: OperationContext, svc: Services, plan_version_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """``POST /v1/plan-versions/{plan_version_id}/exports`` -> export reference + download grant."""
    req = _path_params(request, plan_version_id=plan_version_id)
    require_valid(req, EXPORT_REQUEST)
    check_declared_version(req.get("contract_version"))
    _deny_tools(ctx)

    def execute() -> Mutation:
        _rec, doc = load_version(svc, plan_version_id)
        plan = svc.repo.require("plan", str(doc["plan_id"]))
        revision = int(plan.attrs.get("revision") or 0)
        synthetic = bool(doc.get("synthetic", True))
        metadata = {
            "template_version": TEMPLATE_VERSION,
            "contract_version": current_version(),
            "plan_id": doc["plan_id"],
            "base_plan_version_id": plan_version_id,
            "base_checksum": doc["checksum"],
            "expected_revision": revision,
            "exported_at": ctx.now_ts(),
            "synthetic": synthetic,
        }
        data = build_workbook(doc["content"], metadata)
        stored = svc.store.put_once_or_verify("plans", _export_key(str(doc["plan_id"]), plan_version_id, sha256_checksum(data)), data, XLSX_CONTENT_TYPE)
        export_ref = stored.to_ref(ctx.new_id("artifact_id"), "plan_export", synthetic=synthetic, domain=str(doc.get("domain") or "finance"))
        response = {
            "plan_version_id": plan_version_id,
            "plan_id": doc["plan_id"],
            "template_version": TEMPLATE_VERSION,
            "base_checksum": doc["checksum"],
            "expected_revision": revision,
            "exported_at": metadata["exported_at"],
            "export_ref": export_ref,
            "synthetic": synthetic,
            "contract_version": current_version(),
        }
        ev = audit_event(ctx, record_id=plan_version_id, record_type="plan_version", operation="export_plan_version", prior=None, new=None, template_version=TEMPLATE_VERSION, export_checksum=export_ref["checksum"], expected_revision=revision)
        return Mutation(ops=[], response=response, audit=[ev])

    outcome = svc.repo.run_idempotent(ctx, operation="export_plan_version", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)
    response = dict(outcome.response)
    # the grant is minted per call (never stored in the idempotency record)
    key = _export_key(str(response["plan_id"]), plan_version_id, response["export_ref"]["checksum"])
    head = svc.store.head("plans", key)
    if head is None:
        raise PlatformError.not_found("the export is no longer available (expired); request a new export with a new idempotency key", record_type="plan_export")
    grant = svc.store.download_grant("plans", key, int(svc.cfg.limits["download_grant_ttl_seconds"]))
    response["download_grant"] = {"artifact_id": response["export_ref"]["artifact_id"], "checksum": head.checksum, "url": grant["url"], "expires_at": grant["expires_at"]}
    return IdempotentOutcome(response, outcome.replayed)


# ===================================================================== import grant (2.5 / 8.4)
def issue_import_grant(ctx: OperationContext, svc: Services, plan_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """``POST /v1/plans/{plan_id}/imports`` -> ``import_id`` + presigned POST (size range, <= 15 min)."""
    req = _path_params(request, plan_id=plan_id)
    require_valid(req, IMPORT_GRANT_REQUEST)
    check_declared_version(req.get("contract_version"))
    _deny_tools(ctx)
    limits = svc.cfg.limits
    ttl = min(int(limits["excel_upload_grant_ttl_seconds"]), 900)
    max_bytes = int(limits["excel_upload_max_bytes"])

    def execute() -> Mutation:
        svc.repo.require("plan", plan_id)
        import_id = ctx.new_id("import_id")
        expires_at = to_timestamp(ctx.clock.now() + timedelta(seconds=ttl))
        response = {"import_id": import_id, "plan_id": plan_id, "template_version": TEMPLATE_VERSION, "expires_at": expires_at, "max_bytes": max_bytes}
        ev = audit_event(ctx, record_id=import_id, record_type="excel_import", operation="issue_import_grant", prior=None, new={"status": "grant_issued"}, plan_id=plan_id, expires_at=expires_at, max_bytes=max_bytes)
        return Mutation(ops=[], response=response, audit=[ev])

    outcome = svc.repo.run_idempotent(ctx, operation="issue_import_grant", idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)
    response = dict(outcome.response)
    check_upload_grant(response["expires_at"], ctx.clock)  # a replay after expiry gets no new form
    remaining = int((parse_timestamp(response["expires_at"]) - ctx.clock.now()).total_seconds())
    grant = svc.store.excel_upload_grant(response["import_id"], max_bytes=max_bytes, ttl_seconds=max(1, min(remaining, ttl)))
    response["upload"] = grant.client_view()["upload"]
    return IdempotentOutcome(response, outcome.replayed)


def _grant_record(svc: Services, import_id: str, plan_id: str) -> Mapping[str, Any]:
    for ev in svc.repo.audit_events(import_id):
        if ev.operation == "issue_import_grant" and ev.record_type == "excel_import":
            if ev.details.get("plan_id") != plan_id:
                break
            return ev.details
    raise PlatformError.not_found("import not found for this plan", record_type="excel_import")


# ===================================================================== mapping (8.3)
def content_from_workbook(base_content: Mapping[str, Any], parsed: ParsedWorkbook) -> dict[str, Any]:
    """Canonical plan content: the base version's content with the workbook's allocation.

    ``base_currency``, ``constraints`` and ``fees`` are not editable in ``xlsx-plan-v1`` and are
    carried over unchanged; the ``CASH`` row becomes ``allocation.cash_weight``.
    """
    content = copy.deepcopy(dict(base_content))
    allocation = dict(content.get("allocation") or {})
    allocation["weights"] = [{"instrument_id": r["instrument_id"], "weight": r["target_weight"]} for r in parsed.rows if r["instrument_id"] != CASH_INSTRUMENT_ID]
    if parsed.cash_weight is not None:
        allocation["cash_weight"] = parsed.cash_weight
    else:
        allocation.pop("cash_weight", None)
    content["allocation"] = allocation
    return content


def _unknown_instruments(svc: Services, base: Mapping[str, Any], parsed: ParsedWorkbook) -> list[dict[str, Any]]:
    snap = svc.repo.get("snapshot_catalog", str(base.get("input_snapshot_id")))
    if snap is None:
        return []  # the validation rule ``snapshot_exists`` reports it on the created version
    covered = snapshot_instruments(svc, snap)
    findings = []
    for r in parsed.rows:
        if r["instrument_id"] != CASH_INSTRUMENT_ID and r["instrument_id"] not in covered:
            findings.append(
                {
                    "severity": "error",
                    "code": "VALIDATION_FAILED",
                    "message": "instrument not in the version's snapshot",
                    "sheet": ALLOCATIONS_SHEET,
                    "row": r["row"],
                    "column": "A",
                    "field": "instrument_id",
                    "details": {"instrument_id": r["instrument_id"]},
                }
            )
    return findings


def _meta_finding(message: str, field: str, **details: Any) -> dict[str, Any]:
    f: dict[str, Any] = {"severity": "error", "code": "VALIDATION_FAILED", "message": message, "sheet": META_SHEET, "field": field}
    if details:
        f["details"] = details
    return f


# ===================================================================== commit (8.3, 8.4)
class _Recorded(Exception):
    def __init__(self, error: PlatformError) -> None:
        self.error = error


def _report(ctx: OperationContext, *, plan_id: str, base_pv: str, expected_revision: int | None, source_ref: Mapping[str, Any], outcome: str, findings: list[dict[str, Any]], created: str | None = None, no_effect: bool | None = None, synthetic: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {
        "plan_id": plan_id,
        "base_plan_version_id": base_pv,
        "template_version": TEMPLATE_VERSION,
        "source_artifact": dict(source_ref),
        "outcome": outcome,
        "findings": findings,
        "correlation_id": ctx.correlation_id,
        "created_at": ctx.now_ts(),
        "synthetic": synthetic,
    }
    if expected_revision is not None:
        report["expected_revision"] = expected_revision
    if created:
        report["created_plan_version_id"] = created
    if no_effect is not None:
        report["no_effect"] = no_effect
    return report


def _record_rejection(
    ctx: OperationContext,
    svc: Services,
    *,
    plan_id: str,
    import_id: str,
    error: PlatformError,
    findings: list[dict[str, Any]],
    source_ref: Mapping[str, Any] | None,
    source_checksum: str | None,
    base_pv: str | None = None,
    expected_revision: int | None = None,
    synthetic: bool = True,
) -> None:
    """Rejection record: an ``import-report`` artifact (when the base is known) + an audit event."""
    report_ref: dict[str, Any] | None = None
    if source_ref is not None and base_pv:
        if not findings:
            findings = [{"severity": "error", "code": error.code, "message": error.message}]
        report = _report(ctx, plan_id=plan_id, base_pv=base_pv, expected_revision=expected_revision, source_ref=source_ref, outcome="rejected", findings=findings[:200], synthetic=synthetic)
        require_valid_response(report, "import-report")
        artifact_id = ctx.new_id("artifact_id")
        try:
            stored = svc.store.put_once("reports", key_report(import_id, artifact_id), canonical_text(report).encode("utf-8"), "application/json")
            report_ref = stored.to_ref(artifact_id, "import_report", synthetic=synthetic)
        except ArtifactExists:  # pragma: no cover - fresh artifact IDs never collide
            pass
    details: dict[str, Any] = {"plan_id": plan_id, "code": error.code, "reason": error.details.get("reason"), "findings": findings[:50]}
    if source_checksum:
        details["source_checksum"] = source_checksum
    if report_ref:
        details["report_ref"] = report_ref
    ev = audit_event(ctx, record_id=import_id, record_type="excel_import", operation="reject_excel_import", prior=None, new={"status": "rejected"}, **details)
    svc.repo.commit([], audit=[ev])


def import_workbook_bytes(data: bytes, limits: Mapping[str, Any], *, spec: TemplateSpec | None = None) -> ParsedWorkbook:
    """Pre-check and parse uploaded bytes (pure: no storage, no network)."""
    return parse_workbook(open_package(data, PackageLimits.from_config(limits)), spec=spec)


def commit_import(ctx: OperationContext, svc: Services, plan_id: str, import_id: str, request: Mapping[str, Any], *, spec: TemplateSpec | None = None) -> IdempotentOutcome:
    """``POST /v1/plans/{plan_id}/imports/{import_id}/commit`` (see the module docstring)."""
    req = _path_params(request, plan_id=plan_id, import_id=import_id)
    require_valid(req, COMMIT_IMPORT_REQUEST)
    check_declared_version(req.get("contract_version"))
    _deny_tools(ctx)
    plan = svc.repo.require("plan", plan_id)
    plan_synthetic = bool(plan.doc.get("synthetic", True))
    _grant_record(svc, import_id, plan_id)
    limits = svc.cfg.limits
    incoming = key_excel_incoming(import_id)
    head = svc.store.head("raw", incoming)
    if head is None:
        raise PlatformError.precondition("no workbook has been uploaded for this import", reason="upload_missing", import_id=import_id)
    if head.size_bytes > int(limits["excel_upload_max_bytes"]):
        err = ImportRejected("file_too_large", "the workbook exceeds the upload size limit", size_bytes=head.size_bytes, max_bytes=int(limits["excel_upload_max_bytes"]))
        _record_rejection(ctx, svc, plan_id=plan_id, import_id=import_id, error=err, findings=[], source_ref=None, source_checksum=None)
        raise err
    try:
        data, _ = svc.store.get("raw", incoming)
    except ArtifactNotFound:
        raise PlatformError.precondition("no workbook has been uploaded for this import", reason="upload_missing", import_id=import_id) from None
    checksum = sha256_checksum(data)
    # lineage: the upload is retained by content hash (accepted or rejected)
    source = svc.store.put_once_or_verify("raw", key_excel_source(checksum.removeprefix("sha256:")), data, XLSX_CONTENT_TYPE)
    source_ref = source.to_ref(ctx.new_id("artifact_id"), "excel_source", synthetic=plan_synthetic)
    file_name = req.get("file_name")
    hash_body = {"plan_id": plan_id, "source_checksum": checksum, "template_version": TEMPLATE_VERSION}

    def execute() -> Mutation:
        parsed: ParsedWorkbook | None = None
        base_pv: str | None = None
        expected: int | None = None
        try:
            if file_name is not None and not str(file_name).lower().endswith(".xlsx"):
                raise ImportRejected("unsupported_format", "only .xlsx workbooks are accepted (macro-enabled, binary and legacy formats are rejected)", file_extension=str(file_name).rsplit(".", 1)[-1][:16].lower() if "." in str(file_name) else "")
            parsed = import_workbook_bytes(data, limits, spec=spec)
            meta = parsed.metadata
            base_pv = str(meta["base_plan_version_id"])
            expected = int(meta["expected_revision"])
            if meta["plan_id"] != plan_id:
                raise ImportRejected("workbook_content_rejected", "the workbook belongs to a different plan", findings=[_meta_finding("plan_id does not match the import's plan", "plan_id")])
            base_rec = svc.repo.get("plan_version", base_pv)
            if base_rec is None or base_rec.doc.get("plan_id") != plan_id:
                bad = base_pv
                base_pv = None  # not a version of this plan: no report can name it as the base
                raise ImportRejected("workbook_content_rejected", "the workbook's base version is not a version of this plan", findings=[_meta_finding("base_plan_version_id is not a version of this plan", "base_plan_version_id", plan_version_id=bad)])
            _r, base = load_version(svc, base_pv)
            if base.get("checksum") != meta["base_checksum"]:
                raise ImportRejected("workbook_content_rejected", "the workbook's base_checksum does not match the base version", findings=[_meta_finding("base_checksum does not match the base version", "base_checksum")])
            unknown = _unknown_instruments(svc, base, parsed)
            if unknown:
                first = unknown[0]
                raise ImportRejected(
                    "unknown_instrument",
                    f"instrument {first['details']['instrument_id']} is not in the version's snapshot",
                    findings=unknown,
                    sheet=first["sheet"],
                    row=first["row"],
                    instrument_id=first["details"]["instrument_id"],
                )
            head_rev = svc.repo.require("plan", plan_id)
            current = int(head_rev.attrs.get("revision") or 0)
            if current != expected:
                raise _stale(expected, current, head_rev.attrs.get("current_version_id"))
            content = content_from_workbook(base["content"], parsed)
            mutation = new_version_mutation(
                ctx,
                svc,
                plan_id=plan_id,
                expected_revision=expected,
                content=content,
                domain=str(base["domain"]),
                domain_schema_version=str(base["domain_schema_version"]),
                origin="excel_import",
                parent_plan_version_id=base_pv,
                source_artifact=source_ref,
                reason="excel import",
                operation="commit_excel_import",
                extra_audit=[audit_event(ctx, record_id=import_id, record_type="excel_import", operation="commit_excel_import", prior={"status": "grant_issued"}, new={"status": "accepted"}, plan_id=plan_id, source_checksum=checksum)],
            )
        except PlatformError as exc:
            findings = list(getattr(exc, "findings", None) or exc.details.get("findings") or [])
            if exc.code == "CONFLICT" and not findings:
                findings = [{"severity": "error", "code": "CONFLICT", "message": exc.message, "sheet": META_SHEET, "field": "expected_revision", "details": {k: v for k, v in exc.details.items() if k in ("current_revision", "current_version_id", "expected_revision")}}]
            if exc.code in ("VALIDATION_FAILED", "CONFLICT", "INVALID_IDENTIFIER", "PRECONDITION_FAILED"):
                _record_rejection(ctx, svc, plan_id=plan_id, import_id=import_id, error=exc, findings=findings, source_ref=source_ref, source_checksum=checksum, base_pv=base_pv, expected_revision=expected, synthetic=plan_synthetic)
            raise
        pv_id = mutation.response["plan_version_id"]
        report = _report(ctx, plan_id=plan_id, base_pv=base_pv, expected_revision=expected, source_ref=source_ref, outcome="accepted", findings=[], created=pv_id, no_effect=bool(mutation.response["no_effect"]), synthetic=plan_synthetic)
        require_valid_response(report, "import-report")
        mutation.response = {
            **report,
            "import_id": import_id,
            "plan_version": {k: mutation.response[k] for k in ("plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "no_effect", "revision")},
            "contract_version": current_version(),
        }
        return mutation

    try:
        outcome = svc.repo.run_idempotent(ctx, operation="commit_excel_import", idempotency_key=req.get("idempotency_key"), request_body=hash_body, execute=execute)
    except PlatformError as exc:
        if exc.code == "CONFLICT" and exc.details.get("record_type") == "plan" and "current_version_id" not in exc.details:
            # the head moved between the early check and the transaction: name the current head
            head_now = svc.repo.require("plan", plan_id)
            raise _stale(exc.details.get("expected_revision"), int(head_now.attrs.get("revision") or 0), head_now.attrs.get("current_version_id")) from None
        raise
    return outcome


def _stale(expected: Any, current: int, current_version_id: Any) -> PlatformError:
    return PlatformError.conflict(
        "the workbook is stale: the plan head has moved since export; export the current head version and re-apply the edits",
        record_type="plan",
        reason="stale_workbook",
        expected_revision=expected,
        current_revision=current,
        current_version_id=current_version_id,
    )
