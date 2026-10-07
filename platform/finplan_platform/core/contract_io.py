"""Request and response validation through the pinned contract package (API-owned; task 4.2, API-02).

Two kinds of schemas are used:

* **contract schemas** from the pinned ``finplan-contracts`` package, referenced by name
  (``tools/publish-plan-version-request``, ``plan-version``, ``finance/v1`` ``portfolio`` ...).
  They are loaded from the installed package, never copied (CS-01 copied-``$id`` check). Since
  contracts 0.2.0 (contracts design D13, CS-12) the package also carries the plan API route
  schemas the platform used to define itself: ``api/create-portfolio-request``/``-response``,
  ``api/create-plan-request``/``-response``, ``api/create-root-version-request``/``-response``,
  ``api/record-execution-request``/``-response``, ``api/read-snapshot-observations-response``
  and ``api/get-staged-output-response``. The constants below name them;
* **platform request/response schemas** only where the contract has none: the by-ID reads'
  path parameters, the observation query, and the platform-only routes (staged-output
  acceptance, Excel export and import). They are platform-internal, carry no ``$id`` in the
  contract namespace, and build only on contract definitions through ``$ref`` (identifiers,
  timestamps, checksums, ``artifact-ref``, ``observation`` ...), so error codes and pointers are
  the contract's own (``INVALID_IDENTIFIER`` naming the field, ``OPERATION_NOT_PERMITTED`` for
  ``mode: live``).

Both run through the package validator machinery (``ContractValidator`` with the package
registry, plus the semantic checks a schema declares in ``x-finplan-checks``, for example
``no_storage_locations`` (CS-08) and ``domain_payload``). Invalid documents become a
:class:`~finplan_platform.core.errors.PlatformError` built by the package's own envelope
builder (:func:`~finplan_platform.core.errors.from_validation`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from finplan_contracts.schemas import ID_BASE, ContractValidator, SchemaInfo, load_store
from finplan_contracts.validate import CHECKS, CheckContext, ValidationIssue, ValidationResult
from finplan_contracts.validate import validate as contract_validate

from .errors import PlatformError, from_validation

__all__ = [
    "CREATE_PLAN_REQUEST",
    "CREATE_PLAN_RESPONSE",
    "CREATE_PORTFOLIO_REQUEST",
    "CREATE_PORTFOLIO_RESPONSE",
    "CREATE_OVERRIDE_VERSION_REQUEST",
    "CREATE_OVERRIDE_VERSION_RESPONSE",
    "CREATE_ROOT_VERSION_REQUEST",
    "CREATE_ROOT_VERSION_RESPONSE",
    "GET_STAGED_OUTPUT_RESPONSE",
    "OBSERVATIONS_RESPONSE",
    "PLATFORM_SCHEMAS",
    "RECORD_EXECUTION_REQUEST",
    "RECORD_EXECUTION_RESPONSE",
    "REFRESH_MARKET_DATA_RESPONSE",
    "contract_version",
    "create_version_request_schema",
    "create_version_response_schema",
    "ref",
    "require_valid",
    "require_valid_response",
    "validate_document",
]


def ref(path: str) -> dict[str, str]:
    """``$ref`` to a contract definition, e.g. ``ref("core/v1/identifiers.json#/$defs/plan_id")``."""
    return {"$ref": f"{ID_BASE}{path}"}


def contract_version() -> str:
    """Version of the pinned contract package (what writers stamp on records)."""
    return load_store().version


# --------------------------------------------------------------------- validation
_validators: dict[str, Any] = {}


def _issues_from_error(err: Any) -> ValidationIssue:
    # The package's private error-to-issue mapper gives the exact contract pointers,
    # field names and error codes; reuse it rather than re-implement the mapping.
    from finplan_contracts.validate import _issue_from_error

    return _issue_from_error(err)


def _sorted(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    from finplan_contracts.validate import _sorted as pkg_sorted

    return pkg_sorted(issues)


def validate_document(document: Any, schema: str | Mapping[str, Any], *, context: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate against a contract schema name or a platform schema dict (see module docstring)."""
    if isinstance(schema, str):
        return contract_validate(document, schema, context=dict(context or {}))
    store = load_store()
    name = str(schema.get("x-platform-name", "platform-schema"))
    sid = f"urn:finplan:financialplanning:plan-api:{name}"
    v = _validators.get(sid)
    if v is None:
        v = ContractValidator(dict(schema), registry=store.registry)
        _validators[sid] = v
    issues = [_issues_from_error(e) for e in v.iter_errors(document)]
    info = SchemaInfo(name=name, namespace="platform", major=0, id=sid, path=Path(name), schema=dict(schema))
    cctx = CheckContext(document=document, info=info, store=store, context=dict(context or {}))
    for check in schema.get("x-finplan-checks", []):
        issues.extend(CHECKS[check](cctx))
    return ValidationResult(schema_id=sid, schema_name=name, issues=_sorted(issues))


def require_valid(document: Any, schema: str | Mapping[str, Any], **kwargs: Any) -> None:
    """Raise the contract error (``VALIDATION_FAILED``/``INVALID_IDENTIFIER``/...) for an invalid request."""
    res = validate_document(document, schema, **kwargs)
    if not res.valid:
        raise from_validation(res)


def require_valid_response(document: Any, schema: str | Mapping[str, Any]) -> None:
    """A response that fails its schema is a producer bug: ``INTERNAL``, never sent."""
    res = validate_document(document, schema)
    if not res.valid:
        raise PlatformError.internal("response failed contract validation", schema=res.schema_name, errors=[i.pointer for i in res.issues][:10])


# --------------------------------------------------------------------- contract route schemas
#: Plan API route schemas in the pinned contract package (contracts 0.2.0, D13 / CS-12).
CREATE_PORTFOLIO_REQUEST = "api/create-portfolio-request"
CREATE_PORTFOLIO_RESPONSE = "api/create-portfolio-response"
CREATE_PLAN_REQUEST = "api/create-plan-request"
CREATE_PLAN_RESPONSE = "api/create-plan-response"
CREATE_ROOT_VERSION_REQUEST = "api/create-root-version-request"
CREATE_ROOT_VERSION_RESPONSE = "api/create-root-version-response"
CREATE_OVERRIDE_VERSION_REQUEST = "tools/create-override-version-request"
CREATE_OVERRIDE_VERSION_RESPONSE = "tools/create-override-version-response"
RECORD_EXECUTION_REQUEST = "api/record-execution-request"
RECORD_EXECUTION_RESPONSE = "api/record-execution-response"
OBSERVATIONS_RESPONSE = "api/read-snapshot-observations-response"
GET_STAGED_OUTPUT_RESPONSE = "api/get-staged-output-response"
#: ``POST /v1/ingestions`` (also the scheduled run): a holiday without an earlier snapshot answers
#: ``no_session`` with a null ``input_snapshot_id`` and no ``snapshot`` (contracts 0.2.0).
REFRESH_MARKET_DATA_RESPONSE = "tools/refresh-market-data-response"


def _is_root(request: Mapping[str, Any]) -> bool:
    return request.get("parent_plan_version_id") is None


def create_version_request_schema(request: Mapping[str, Any]) -> str:
    """``POST /v1/plans/{plan_id}/versions``: a root (no parent) names its full lineage
    (``api/create-root-version-request``); a child is the contract override request and inherits
    ``input_snapshot_id``, ``configuration_id`` and ``model_version`` from its parent."""
    return CREATE_ROOT_VERSION_REQUEST if _is_root(request) else CREATE_OVERRIDE_VERSION_REQUEST


def create_version_response_schema(response: Mapping[str, Any]) -> str:
    return CREATE_OVERRIDE_VERSION_RESPONSE if response.get("origin") == "manual_override" else CREATE_ROOT_VERSION_RESPONSE


# --------------------------------------------------------------------- platform schemas
_CV = ref("core/v1/common.json#/$defs/contract_version")
_ID = lambda name: ref(f"core/v1/identifiers.json#/$defs/{name}")

GET_BY_ID_REQUEST = lambda id_name: {
    "x-platform-name": f"get-{id_name.replace('_', '-')}-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {id_name: _ID(id_name), "download": {"type": "boolean"}, "contract_version": _CV},
    "required": [id_name],
    "additionalProperties": False,
}

GET_STAGED_OUTPUT_REQUEST = GET_BY_ID_REQUEST("run_id")

#: Query of ``GET /v1/snapshots/{input_snapshot_id}/observations`` (path and query parameters; the
#: contract defines only its response).
OBSERVATIONS_REQUEST: dict[str, Any] = {
    "x-platform-name": "snapshot-observations-request",
    "x-finplan-checks": ["no_storage_locations"],
    "type": "object",
    "properties": {
        "input_snapshot_id": _ID("input_snapshot_id"),
        "instrument_ids": {"type": "array", "items": ref("finance/v1/instrument.json#/$defs/instrument_id"), "uniqueItems": True, "minItems": 1, "maxItems": 50},
        "start_date": ref("core/v1/common.json#/$defs/date"),
        "end_date": ref("core/v1/common.json#/$defs/date"),
        "page_size": ref("core/v1/common.json#/$defs/page_size"),
        "next_token": ref("core/v1/common.json#/$defs/next_token"),
        "contract_version": _CV,
    },
    "required": ["input_snapshot_id"],
    "additionalProperties": False,
}

PLATFORM_SCHEMAS: dict[str, dict[str, Any]] = {
    s["x-platform-name"]: s
    for s in (
        OBSERVATIONS_REQUEST,
        GET_STAGED_OUTPUT_REQUEST,
    )
}
