"""Staged-output acceptance (STAGING-owned; tasks 7.1-7.4; STG-01..STG-06; design P7).

FinanceModel production workers write a run's files under the platform-owned staging area
(``/finplan/<env>/financialplanning/config/run-staging-ref`` -> ``outputs`` bucket,
``staging/<run_id>/``) and write ``manifest.json`` **last**. Nothing happens on that write:
acceptance is an explicit, authenticated, idempotent platform call
(``POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept``), and the outcome is readable
through ``GET /v1/staged-outputs/{run_id}``.

Operation :func:`accept_staged_output` (request ``{expected_revision, idempotency_key}``):

1. **Request**: platform schema (contract ``$ref`` definitions only), declared contract major.
   Tool and FinanceModel role classes are refused (``FORBIDDEN``; the router denies them too).
2. **Idempotency first**: an exact retry (same caller, key and body) returns the original
   result, even after the plan head has moved.
3. **Decided runs**: a run that already has an outcome row fails with ``CONFLICT`` naming the
   existing outcome (and ``plan_version_id`` when accepted); a run yields at most one decision
   and at most one plan version (STG-06). The row is also written with an
   ``attribute_not_exists`` condition inside the commit transaction, so two concurrent
   acceptances cannot both win.
4. **Manifest present** (STG-02): no ``manifest.json`` -> ``PRECONDITION_FAILED``
   ``staged_output_incomplete``; nothing is written.
5. **Manifest**: strict JSON, contract ``staged-output-manifest`` schema (with its domain
   payload check), ``run_id``/``plan_id`` equal to the path, served contract major.
6. **Registry** (STG-03): ``run_id`` and ``model_version`` must exist in FinanceModel's
   published registry reference for this environment (:class:`ModelRegistry`). No FinanceModel
   release recorded -> ``DEPENDENCY_UNAVAILABLE`` (retryable; nothing written).
7. **Outcome** (STG-04): ``failed``/``cancelled``/``timed_out`` -> recorded ``rejected`` with an
   error envelope, no version (not an error response: the decision was recorded).
8. **Structure** (STG-03, succeeded runs): every listed file exists, matches its size and
   SHA-256, no unlisted file exists, the ``input_snapshot_id`` is in the snapshot catalog, the
   named parent belongs to the plan and the plan content is present and schema-valid. Any
   failure is recorded as ``rejected`` (``rejection_kind: structural``, listing the files) and
   the call fails with ``VALIDATION_FAILED``; no version is created.
9. ``succeeded`` + ``infeasible``/``unbounded`` -> recorded ``no_version`` with the solution
   status (not an error).
10. ``succeeded`` + ``optimal``/``feasible``/``no_effect`` -> **commit** (STG-05): the manifest
    and listed files are copied write-once to ``outputs/accepted/<run_id>/``; one transaction
    creates the version (origin ``model_run``, the manifest's lineage, the accepted manifest as
    its ``source_artifact``, content written write-once to ``plans``), moves the plan head
    under ``expected_revision``, writes the ``staged_output`` row, the idempotency record and
    the audit events. A moved head fails with ``CONFLICT`` and writes nothing, so the staged
    output stays eligible for a retry with the new revision.
11. **Validation gate** before the response: the deterministic plan rules
    (:func:`~finplan_platform.core.validation.evaluate_rules`) plus the acceptance rule
    ``required_instruments`` (every instrument of the parent version's allocation must be
    present in the staged allocation). Failures end ``invalid`` (never ``validated``), so a
    partial output cannot be published (``PRECONDITION_FAILED``).

Nothing in a response or record names a bucket or key; staged files are named only by their
manifest-relative names.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import ClientError
from finplan_contracts.canonical import CanonicalizationError, canonicalize, loads_strict
from finplan_contracts.ssm import build as ssm_build
from finplan_contracts.validate import validate as contract_validate

from .artifacts import ArtifactNotFound, key_accepted, key_staging_prefix, sha256_checksum
from .audit import audit_event
from .context import OperationContext
from .contract_io import GET_STAGED_OUTPUT_REQUEST, ref, require_valid
from .errors import PlatformError
from .repository import IdempotentOutcome, Mutation, PutNew, Record, Transition
from .services import Services
from .upgrade import check_declared_version, current_version, serve
from .validation import evaluate_rules, validation_result
from .versions import new_version_mutation

__all__ = [
    "ACCEPT_STAGED_OUTPUT_REQUEST",
    "ACCEPT_STAGED_OUTPUT_RESPONSE",
    "COMMITTABLE_SOLUTIONS",
    "MANIFEST_NAME",
    "NO_VERSION_SOLUTIONS",
    "REJECTED_COMPLETIONS",
    "ModelRegistry",
    "RegistryLookup",
    "SsmModelRegistry",
    "StaticModelRegistry",
    "accept_staged_output",
    "get_staged_outcome",
    "get_staged_output",
    "required_instruments_findings",
    "resolve_registry",
]

MANIFEST_NAME = "manifest.json"
PLAN_CONTENT_FILE = "plan-content.json"
STAGED_MANIFEST_KIND = "staged_output_manifest"
REJECTED_COMPLETIONS = frozenset({"failed", "cancelled", "timed_out"})
NO_VERSION_SOLUTIONS = frozenset({"infeasible", "unbounded"})
COMMITTABLE_SOLUTIONS = frozenset({"optimal", "feasible", "no_effect"})
#: Bounds on what one acceptance reads (phase 1 outputs are kilobytes).
MAX_STAGED_FILES = 200
MAX_STAGED_FILE_BYTES = 16 * 1024 * 1024
MAX_STAGED_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
#: Role classes that may never accept (FinanceLambdasTool classes and FinanceModel roles).
_DENIED_CLASSES = frozenset({"reader", "submitter", "plan-writer", "financemodel-job", "financemodel-job-api"})
_OPERATION = "accept_staged_output"

ACCEPT_STAGED_OUTPUT_REQUEST: dict[str, Any] = {
    "x-platform-name": "accept-staged-output-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {
        "plan_id": ref("core/v1/identifiers.json#/$defs/plan_id"),
        "run_id": ref("core/v1/identifiers.json#/$defs/run_id"),
        "expected_revision": ref("core/v1/concurrency.json#/$defs/expected_revision"),
        "idempotency_key": ref("core/v1/idempotency.json#/$defs/idempotency_key"),
        "contract_version": ref("core/v1/common.json#/$defs/contract_version"),
        "synthetic": ref("core/v1/common.json#/$defs/synthetic"),
    },
    "required": ["plan_id", "run_id", "expected_revision", "idempotency_key"],
    "additionalProperties": False,
}


#: Response of ``POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept``: the recorded outcome
#: (contract ``api/get-staged-output-response``) plus, for ``accepted``, the created version's
#: head fields and its validation result. Structural rejections are error envelopes instead.
ACCEPT_STAGED_OUTPUT_RESPONSE: dict[str, Any] = {
    "x-platform-name": "accept-staged-output-response",
    "allOf": [ref("core/v1/api/get-staged-output-response.json")],
    "type": "object",
    "properties": {
        "status": ref("core/v1/plan-version.json#/$defs/status"),
        "origin": ref("core/v1/plan-version.json#/$defs/origin"),
        "no_effect": {"type": "boolean"},
        "revision": ref("core/v1/concurrency.json#/$defs/revision"),
        "content_ref": ref("core/v1/artifact-ref.json"),
        "required_instruments": {"type": "array", "items": ref("finance/v1/instrument.json#/$defs/instrument_id"), "uniqueItems": True},
        "findings": ref("core/v1/tools/validate-plan-version-response.json#/properties/findings"),
        "validation_ruleset_version": {"type": "string", "minLength": 1, "maxLength": 64},
    },
}


# ===================================================================== registry reference
@dataclass(frozen=True)
class RegistryLookup:
    """Answer of FinanceModel's registry reference for one ``(run_id, model_version)``."""

    release_recorded: bool
    run_known: bool = False
    model_version_known: bool = False


class ModelRegistry(Protocol):
    """FinanceModel's published registry reference for one environment (design P7 step 2)."""

    def lookup(self, run_id: str, model_version: str) -> RegistryLookup: ...


@dataclass
class StaticModelRegistry:
    """In-memory registry (tests, local fixture runs): known runs and model versions."""

    runs: frozenset[str] | set[str] = frozenset()
    model_versions: frozenset[str] | set[str] = frozenset()
    release_recorded: bool = True

    def lookup(self, run_id: str, model_version: str) -> RegistryLookup:
        if not self.release_recorded:
            return RegistryLookup(False)
        return RegistryLookup(True, run_id in self.runs, model_version in self.model_versions)


class SsmModelRegistry:
    """Resolves ``/finplan/<env>/financemodel/model/registry-ref`` and delegates the lookup.

    A missing parameter means FinanceModel has no release in the environment
    (``release_recorded`` false -> ``DEPENDENCY_UNAVAILABLE``). The lookup itself goes through
    FinanceModel's job API (explicit invoke grant, design P7); that client is injected as
    ``lookup_client(registry_ref, run_id, model_version) -> RegistryLookup``. Until FinanceModel
    publishes that interface no client exists, and acceptance fails with
    ``DEPENDENCY_UNAVAILABLE`` instead of guessing.
    """

    def __init__(self, ssm_client: Any, env: str, lookup_client: Any = None) -> None:
        self.ssm = ssm_client
        self.env = env
        self.lookup_client = lookup_client

    @property
    def parameter_name(self) -> str:
        return ssm_build(self.env, "financemodel", "model", "registry-ref")

    def lookup(self, run_id: str, model_version: str) -> RegistryLookup:
        try:
            value = self.ssm.get_parameter(Name=self.parameter_name)["Parameter"]["Value"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return RegistryLookup(False)
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "the model registry reference could not be read", retryable=True) from None
        if self.lookup_client is None:
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "the model registry lookup is not available in this release", retryable=True, dependency="financemodel-registry")
        result = self.lookup_client(value, run_id, model_version)
        if not isinstance(result, RegistryLookup):  # pragma: no cover - defensive
            raise PlatformError.internal("model registry lookup returned an unexpected result")
        return result


def resolve_registry(svc: Services) -> ModelRegistry:
    """``svc.extras["model_registry"]`` if injected, else the SSM-backed reference."""
    reg = svc.extras.get("model_registry")
    if reg is not None:
        return reg
    ssm = svc.extras.get("ssm")
    if ssm is None:
        raise PlatformError("DEPENDENCY_UNAVAILABLE", "the model registry reference is not configured", retryable=True, dependency="financemodel-registry")
    return SsmModelRegistry(ssm, svc.env, svc.extras.get("model_registry_lookup"))


# ===================================================================== helpers
class _Rejected(Exception):
    """A recorded structural rejection (internal control flow)."""

    def __init__(self, message: str, *, pointer: str = "", field: str | None = None, files: Sequence[Mapping[str, Any]] = (), **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.pointer = pointer
        self.field = field
        self.files = [dict(f) for f in files]
        self.details = details

    def error(self) -> PlatformError:
        d: dict[str, Any] = {"reason": "staged_output_rejected", **self.details}
        if self.field:
            d["field"] = self.field
        if self.files:
            d["files"] = self.files[:50]
        return PlatformError.validation(self.message, pointer=self.pointer, **d)


def _manifest_key(run_id: str) -> str:
    return key_staging_prefix(run_id) + MANIFEST_NAME


def _read_manifest(svc: Services, run_id: str) -> bytes | None:
    head = svc.store.head("outputs", _manifest_key(run_id))
    if head is None:
        return None
    if head.size_bytes > MAX_MANIFEST_BYTES:
        raise _Rejected("the staged manifest exceeds the size limit", pointer="", reason_detail="manifest_too_large")
    try:
        data, _ = svc.store.get("outputs", _manifest_key(run_id))
    except ArtifactNotFound:
        return None
    except PlatformError as exc:
        if exc.code == "INTERNAL":
            raise _Rejected("the staged manifest failed its stored checksum", pointer="") from None
        raise
    return data


def _parse_manifest(raw: bytes, *, plan_id: str, run_id: str) -> dict[str, Any]:
    try:
        manifest = loads_strict(raw)
    except (ValueError, CanonicalizationError, UnicodeDecodeError):
        raise _Rejected("the staged manifest is not valid JSON", pointer="") from None
    if not isinstance(manifest, dict):
        raise _Rejected("the staged manifest must be a JSON object", pointer="")
    res = contract_validate(manifest, "staged-output-manifest")
    if not res.valid:
        issue = res.primary()
        assert issue is not None
        raise _Rejected(
            "the staged manifest does not validate against the contract",
            pointer=issue.pointer,
            field=issue.field,
            issues=[{"pointer": i.pointer, "code": i.code} for i in res.issues[:20]],
        )
    if manifest["run_id"] != run_id:
        raise _Rejected("the manifest's run_id does not match the staged run", pointer="/run_id", field="run_id")
    if manifest["plan_id"] != plan_id:
        # a request error (wrong plan in the path), not a defect of the run: nothing is recorded
        raise PlatformError.validation("the staged output belongs to a different plan", pointer="/plan_id", field="plan_id")
    check_declared_version(manifest.get("contract_version"))
    return manifest


def _check_registry(svc: Services, manifest: Mapping[str, Any]) -> None:
    found = resolve_registry(svc).lookup(manifest["run_id"], manifest["model_version"])
    if not found.release_recorded:
        raise PlatformError("DEPENDENCY_UNAVAILABLE", "no FinanceModel release is recorded in this environment", retryable=True, dependency="financemodel-registry")
    if not found.model_version_known:
        raise _Rejected("model_version is not in the environment's FinanceModel registry", pointer="/model_version", field="model_version")
    if not found.run_known:
        raise _Rejected("run_id is not in the environment's FinanceModel registry", pointer="/run_id", field="run_id")


def _check_files(svc: Services, run_id: str, files: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
    """Every listed file exists with its size and SHA-256; no unlisted file exists."""
    if len(files) > MAX_STAGED_FILES:
        raise _Rejected("the manifest lists too many files", pointer="/files", count=len(files))
    prefix = key_staging_prefix(run_id)
    listed = {str(f["name"]): f for f in files}
    problems: list[dict[str, Any]] = []
    if len(listed) != len(files):
        problems.append({"name": "(duplicate names)", "problem": "duplicate"})
    if MANIFEST_NAME in listed:
        problems.append({"name": MANIFEST_NAME, "problem": "manifest_listed_as_file"})
    present = set()
    for key, _modified in svc.store.list_objects("outputs", prefix):
        name = key[len(prefix):]
        if not name or name.endswith("/"):
            continue
        present.add(name)
    for name in sorted(present - set(listed) - {MANIFEST_NAME}):
        problems.append({"name": name, "problem": "unlisted"})
    contents: dict[str, bytes] = {}
    total = 0
    for name, entry in sorted(listed.items()):
        if name not in present:
            problems.append({"name": name, "problem": "missing"})
            continue
        head = svc.store.head("outputs", prefix + name)
        if head is None:
            problems.append({"name": name, "problem": "missing"})
            continue
        if head.size_bytes > MAX_STAGED_FILE_BYTES or total + head.size_bytes > MAX_STAGED_TOTAL_BYTES:
            problems.append({"name": name, "problem": "too_large"})
            continue
        try:
            data, _ = svc.store.get("outputs", prefix + name)
        except ArtifactNotFound:
            problems.append({"name": name, "problem": "missing"})
            continue
        except PlatformError as exc:
            if exc.code != "INTERNAL":
                raise
            problems.append({"name": name, "problem": "checksum_mismatch"})
            continue
        total += len(data)
        if sha256_checksum(data) != entry["checksum"]:
            problems.append({"name": name, "problem": "checksum_mismatch"})
        elif len(data) != int(entry["size_bytes"]):
            problems.append({"name": name, "problem": "size_mismatch"})
        else:
            contents[name] = data
    if problems:
        kinds = sorted({p["problem"] for p in problems})
        raise _Rejected("staged files do not match the manifest", pointer="/files", files=problems, problems=kinds)
    return contents


def _plan_content(manifest: Mapping[str, Any], contents: Mapping[str, bytes]) -> dict[str, Any]:
    inline = (manifest.get("payload") or {}).get("plan_content")
    from_file: Any = None
    if PLAN_CONTENT_FILE in contents:
        try:
            from_file = loads_strict(contents[PLAN_CONTENT_FILE])
        except (ValueError, CanonicalizationError, UnicodeDecodeError):
            raise _Rejected("plan-content.json is not valid JSON", pointer="/files", files=[{"name": PLAN_CONTENT_FILE, "problem": "invalid_json"}]) from None
    if from_file is not None and inline is not None and canonicalize(from_file) != canonicalize(inline):
        raise _Rejected("plan-content.json differs from the manifest payload's plan_content", pointer="/payload/plan_content", files=[{"name": PLAN_CONTENT_FILE, "problem": "differs_from_payload"}])
    content = from_file if from_file is not None else inline
    if content is None:
        raise _Rejected("a succeeded run must stage plan content (plan-content.json or payload.plan_content)", pointer="/payload/plan_content")
    res = contract_validate(content, "plan-content")
    if not res.valid:
        issue = res.primary()
        assert issue is not None
        raise _Rejected("the staged plan content does not match the plan content schema", pointer="/payload/plan_content" + issue.pointer, field=issue.field)
    return dict(content)


def required_instruments_findings(required: Sequence[str], content: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Acceptance rule ``required_instruments``: instruments the plan requires but the output omits."""
    present = {str(w.get("instrument_id")) for w in ((content.get("allocation") or {}).get("weights") or []) if isinstance(w, Mapping)}
    missing = sorted(set(required) - present)
    if not missing:
        return []
    return [
        {
            "code": "VALIDATION_FAILED",
            "message": "the staged allocation omits instruments the plan requires",
            "pointer": "/content/allocation/weights",
            "details": {"rule": "required_instruments", "missing_instruments": missing},
        }
    ]


def _required_instruments(svc: Services, parent_id: str | None) -> list[str]:
    """The plan's required instruments: the parent version's allocated instruments (if any)."""
    if not parent_id:
        return []
    parent = svc.repo.get("plan_version", parent_id)
    if parent is None:
        return []
    weights = ((parent.doc.get("content") or {}).get("allocation") or {}).get("weights") or []
    return sorted({str(w["instrument_id"]) for w in weights if isinstance(w, Mapping) and "instrument_id" in w})


def _envelopes(ctx: OperationContext, findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in findings:
        out.append(
            {
                "code": f["code"],
                "message": f["message"],
                "retryable": False,
                "details": {**dict(f.get("details") or {}), "pointer": f.get("pointer", "")},
                "correlation_id": ctx.correlation_id,
                "contract_version": current_version(),
            }
        )
    return out


def _validate_accepted(ctx: OperationContext, svc: Services, plan_version_id: str, required: Sequence[str]) -> dict[str, Any]:
    """Validation gate (STG-05): plan rules + ``required_instruments``; one audited transition.

    A version already ``validated``/``invalid`` returns its stored result unchanged, so a
    replayed acceptance never re-decides.
    """
    rec = svc.repo.require("plan_version", plan_version_id)
    if rec.doc.get("status") != "pending_validation":
        return validation_result(rec.doc)
    findings = list(evaluate_rules(svc, rec.doc)) + required_instruments_findings(required, rec.doc.get("content") or {})
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
        gate="staged_output_acceptance",
        required_instruments=list(required),
    )
    try:
        svc.repo.commit([op], audit=[ev])
    except PlatformError as exc:
        if exc.code != "PRECONDITION_FAILED":
            raise
        return validation_result(svc.repo.require("plan_version", plan_version_id).doc)
    return validation_result(new_doc)


def _existing_outcome_conflict(rec: Record | None, run_id: str) -> PlatformError:
    details: dict[str, Any] = {"record_type": "staged_output", "run_id": run_id, "reason": "run_already_decided"}
    if rec is not None:
        details["outcome"] = rec.doc.get("outcome")
        if rec.doc.get("plan_version_id"):
            details["plan_version_id"] = rec.doc["plan_version_id"]
    return PlatformError.conflict("this run already has an acceptance outcome; a run produces at most one plan version", **details)


def _outcome_doc(ctx: OperationContext, manifest: Mapping[str, Any], *, plan_id: str, run_id: str, outcome: str, manifest_checksum: str | None, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "run_id": run_id,
        "plan_id": plan_id,
        "outcome": outcome,
        "decided_at": ctx.now_ts(),
        "correlation_id": ctx.correlation_id,
        "synthetic": bool(manifest.get("synthetic", True)) if manifest else True,
    }
    for name in ("model_version", "configuration_id", "input_snapshot_id", "parent_plan_version_id", "completion_status", "solution_status"):
        if manifest and manifest.get(name) is not None:
            doc[name] = manifest[name]
    if manifest_checksum:
        doc["manifest_checksum"] = manifest_checksum
    doc.update({k: v for k, v in extra.items() if v is not None})
    return doc


def _outcome_put(doc: Mapping[str, Any]) -> PutNew:
    run_id = str(doc["run_id"])
    return PutNew("staged_output", doc=dict(doc), contract_version=current_version(), error=lambda cur: _existing_outcome_conflict(cur, run_id))


def _decision_mutation(ctx: OperationContext, doc: dict[str, Any]) -> Mutation:
    ev = audit_event(
        ctx,
        record_id=str(doc["run_id"]),
        record_type="staged_output",
        operation=_OPERATION,
        prior=None,
        new={"outcome": doc["outcome"]},
        plan_id=doc["plan_id"],
        **({"rejection_kind": doc["rejection_kind"]} if doc.get("rejection_kind") else {}),
        **({"solution_status": doc["solution_status"]} if doc.get("solution_status") else {}),
    )
    return Mutation(ops=[_outcome_put(doc)], response=dict(doc), audit=[ev])


# ===================================================================== acceptance
def accept_staged_output(ctx: OperationContext, svc: Services, plan_id: str, run_id: str, request: Mapping[str, Any]) -> IdempotentOutcome:
    """``POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept`` (see the module docstring).

    Returns an :class:`IdempotentOutcome` (``response``, ``replayed``): ``outcome`` ``accepted``
    (version fields, ``status`` after the validation gate, ``findings``), ``no_version`` or
    ``rejected`` (run outcome). Structural rejections are recorded and then raised as
    ``VALIDATION_FAILED``.
    """
    req = dict(request)
    for name, value in (("plan_id", plan_id), ("run_id", run_id)):
        if name in req and req[name] != value:
            raise PlatformError.validation(f"{name} in the body does not match the path", pointer=f"/{name}", field=name)
        req[name] = value
    require_valid(req, ACCEPT_STAGED_OUTPUT_REQUEST)
    check_declared_version(req.get("contract_version"))
    if ctx.caller.role_class in _DENIED_CLASSES:
        raise PlatformError.forbidden("staged-output acceptance is a platform-side operation", role_class=ctx.caller.role_class)
    expected_revision = int(req["expected_revision"])

    def execute() -> Mutation:
        plan = svc.repo.require("plan", plan_id)
        existing = svc.repo.get("staged_output", run_id)
        if existing is not None:
            raise _existing_outcome_conflict(existing, run_id)
        manifest: dict[str, Any] = {}
        manifest_checksum: str | None = None
        try:
            raw = _read_manifest(svc, run_id)
            if raw is None:
                raise PlatformError.precondition("the staged output has no manifest yet; acceptance starts only after the manifest is written", reason="staged_output_incomplete", run_id=run_id)
            manifest_checksum = sha256_checksum(raw)
            manifest = _parse_manifest(raw, plan_id=plan_id, run_id=run_id)
            _check_registry(svc, manifest)
            completion = manifest["completion_status"]
            if completion in REJECTED_COMPLETIONS:
                err = PlatformError.precondition("the model run did not succeed; no plan version is created", reason="run_not_succeeded", completion_status=completion, run_id=run_id)
                doc = _outcome_doc(ctx, manifest, plan_id=plan_id, run_id=run_id, outcome="rejected", manifest_checksum=manifest_checksum, rejection_kind="run_outcome", error=err.to_envelope(ctx.correlation_id, current_version()))
                return _decision_mutation(ctx, doc)
            contents = _check_files(svc, run_id, manifest["files"])
            if svc.repo.get("snapshot_catalog", manifest["input_snapshot_id"]) is None:
                raise _Rejected("input_snapshot_id is not in this environment's snapshot catalog", pointer="/input_snapshot_id", field="input_snapshot_id")
            parent_id = manifest.get("parent_plan_version_id")
            if parent_id:
                parent = svc.repo.get("plan_version", parent_id)
                if parent is None or parent.doc.get("plan_id") != plan_id:
                    raise _Rejected("parent_plan_version_id is not a version of this plan", pointer="/parent_plan_version_id", field="parent_plan_version_id")
            solution = manifest["solution_status"]
            if solution in NO_VERSION_SOLUTIONS:
                doc = _outcome_doc(ctx, manifest, plan_id=plan_id, run_id=run_id, outcome="no_version", manifest_checksum=manifest_checksum)
                return _decision_mutation(ctx, doc)
            if solution not in COMMITTABLE_SOLUTIONS:
                raise _Rejected("solution_status is not one this platform can commit", pointer="/solution_status", field="solution_status")
            content = _plan_content(manifest, contents)
        except _Rejected as rej:
            err = rej.error()
            doc = _outcome_doc(
                ctx,
                manifest,
                plan_id=plan_id,
                run_id=run_id,
                outcome="rejected",
                manifest_checksum=manifest_checksum,
                rejection_kind="structural",
                error=err.to_envelope(ctx.correlation_id, current_version()),
                rejected_files=rej.files or None,
            )
            return _decision_mutation(ctx, doc)

        # ---- commit path: head checked before any artifact is written
        current = int(plan.attrs.get("revision") or 0)
        if current != expected_revision:
            raise PlatformError.conflict(
                "expected_revision does not match the plan head revision; the staged output stays eligible for a retry",
                record_type="plan",
                expected_revision=expected_revision,
                current_revision=current,
                current_version_id=plan.attrs.get("current_version_id"),
            )
        synthetic = bool(plan.doc.get("synthetic", True))
        accepted_manifest = svc.store.put_once_or_verify("outputs", key_accepted(run_id, MANIFEST_NAME), raw, "application/json")
        for name, data in sorted(contents.items()):
            entry = next(f for f in manifest["files"] if f["name"] == name)
            svc.store.put_once_or_verify("outputs", key_accepted(run_id, name), data, str(entry.get("content_type") or "application/octet-stream"))
        source_ref = accepted_manifest.to_ref(ctx.new_id("artifact_id"), STAGED_MANIFEST_KIND, synthetic=synthetic, domain=manifest["domain"])
        required = _required_instruments(svc, parent_id)
        # the plan version id is minted inside new_version_mutation; the outcome row is added after
        mutation = new_version_mutation(
            ctx,
            svc,
            plan_id=plan_id,
            expected_revision=expected_revision,
            content=content,
            domain=manifest["domain"],
            domain_schema_version=manifest["domain_schema_version"],
            origin="model_run",
            parent_plan_version_id=parent_id,
            input_snapshot_id=manifest["input_snapshot_id"],
            configuration_id=manifest["configuration_id"],
            model_version=manifest["model_version"],
            run_id=run_id,
            source_artifact=source_ref,
            reason="staged output acceptance",
            operation=_OPERATION,
        )
        pv_id = mutation.response["plan_version_id"]
        doc = _outcome_doc(ctx, manifest, plan_id=plan_id, run_id=run_id, outcome="accepted", manifest_checksum=manifest_checksum, plan_version_id=pv_id, manifest_ref=source_ref)
        mutation.ops.append(_outcome_put(doc))
        mutation.audit.append(audit_event(ctx, record_id=run_id, record_type="staged_output", operation=_OPERATION, prior=None, new={"outcome": "accepted"}, plan_id=plan_id, plan_version_id=pv_id))
        mutation.response = {
            **doc,
            "status": mutation.response["status"],
            "checksum": mutation.response["checksum"],
            "origin": "model_run",
            "parent_plan_version_id": mutation.response["parent_plan_version_id"],
            "no_effect": mutation.response["no_effect"],
            "revision": mutation.response["revision"],
            "content_ref": mutation.response["content_ref"],
            "required_instruments": list(required),
            "contract_version": current_version(),
        }
        return mutation

    outcome = svc.repo.run_idempotent(ctx, operation=_OPERATION, idempotency_key=req.get("idempotency_key"), request_body=req, execute=execute)
    response = dict(outcome.response)
    if response.get("outcome") == "accepted":
        result = _validate_accepted(ctx, svc, response["plan_version_id"], response.get("required_instruments") or [])
        response["status"] = result["status"]
        response["findings"] = result["findings"]
        if result.get("validation_ruleset_version"):
            response["validation_ruleset_version"] = result["validation_ruleset_version"]
    elif response.get("outcome") == "rejected" and response.get("rejection_kind") == "structural":
        env = response["error"]
        raise PlatformError(env["code"], env["message"], details={**env.get("details", {}), "run_id": run_id})
    return IdempotentOutcome(response, outcome.replayed)


# ===================================================================== outcome read
def get_staged_output(ctx: OperationContext, svc: Services, run_id: str) -> dict[str, Any]:
    """``GET /v1/staged-outputs/{run_id}``: ``accepted`` (+ ``plan_version_id`` and its current
    validation status), ``rejected`` (+ error envelope) or ``no_version`` (+ solution status)."""
    require_valid({"run_id": run_id}, GET_STAGED_OUTPUT_REQUEST)
    rec = svc.repo.get("staged_output", run_id)
    if rec is None:
        raise PlatformError.not_found("no acceptance outcome for this run", record_type="staged_output")
    doc = serve(rec, overlay=False)
    if doc.get("outcome") == "accepted" and doc.get("plan_version_id"):
        pv = svc.repo.get("plan_version", str(doc["plan_version_id"]))
        if pv is not None:
            doc["plan_version_status"] = pv.doc.get("status")
            doc["checksum"] = pv.doc.get("checksum")
    return doc


#: Alias named in the staging interface (``get_staged_outcome(ctx, run_id)``).
get_staged_outcome = get_staged_output


def staged_outcome_json(doc: Mapping[str, Any]) -> str:  # pragma: no cover - debugging aid
    return json.dumps(doc, sort_keys=True, indent=2)
