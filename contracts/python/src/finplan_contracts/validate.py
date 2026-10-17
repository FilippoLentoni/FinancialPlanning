"""Validate documents against contract schemas.

``validate(document, schema)`` runs JSON Schema 2020-12 validation (schemas are
resolved offline through :mod:`finplan_contracts.schemas`) and then every semantic
check the schema declares in ``x-finplan-checks``. It returns a
:class:`ValidationResult` with structured, deterministic issues. Messages never echo
instance values, so an error envelope built from them cannot leak storage
locations or secrets that a caller put into a request.

Error-code mapping:

* a failure inside a subschema annotated ``x-finplan-error-code`` takes that code;
  identifier definitions carry ``INVALID_IDENTIFIER`` (so a ``pl_`` value in a
  ``plan_version_id`` field yields ``INVALID_IDENTIFIER`` naming the field), and the
  execution ``mode`` carries ``OPERATION_NOT_PERMITTED`` (``live`` is rejected);
* everything else is ``VALIDATION_FAILED``.

Semantic checks are registered with :func:`register_check`; a check receives a
:class:`CheckContext` and returns a list of issues. Constraints JSON Schema cannot
express (for example "budget categories sum to at most the ceiling", ENV-17) are
implemented this way.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from jsonschema.exceptions import ValidationError, best_match

from .schemas import ID_BASE, SchemaInfo, SchemaNotFound, SchemaStore, load_store

VALIDATION_FAILED = "VALIDATION_FAILED"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
OPERATION_NOT_PERMITTED = "OPERATION_NOT_PERMITTED"
#: When several issues carry different codes, the envelope uses the first of these.
CODE_PRIORITY = (OPERATION_NOT_PERMITTED, INVALID_IDENTIFIER, VALIDATION_FAILED)


# --------------------------------------------------------------------- results
@dataclass(frozen=True)
class ValidationIssue:
    code: str
    pointer: str
    message: str
    field: str | None = None
    keyword: str | None = None
    schema_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"code": self.code, "pointer": self.pointer, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        if self.keyword is not None:
            d["keyword"] = self.keyword
        return d


@dataclass
class ValidationResult:
    schema_id: str
    schema_name: str
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def __bool__(self) -> bool:
        return self.valid

    @property
    def code(self) -> str | None:
        """The error code an API would return for this result (None when valid)."""
        if not self.issues:
            return None
        codes = {i.code for i in self.issues}
        for c in CODE_PRIORITY:
            if c in codes:
                return c
        return sorted(codes)[0]

    @property
    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def primary(self) -> ValidationIssue | None:
        code = self.code
        return next((i for i in self.issues if i.code == code), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_name,
            "schema_id": self.schema_id,
            "valid": self.valid,
            "code": self.code,
            "issues": [i.to_dict() for i in self.issues],
        }

    def to_error_envelope(self, correlation_id: str, contract_version: str | None = None) -> dict[str, Any]:
        """Build a ``core/v1/error.json`` envelope for an invalid result."""
        if self.valid:
            raise ValueError("result is valid; there is no error to report")
        primary = self.primary()
        assert primary is not None
        details: dict[str, Any] = {"pointer": primary.pointer, "schema_id": self.schema_id, "errors": [i.to_dict() for i in self.issues]}
        if primary.field is not None:
            details["field"] = primary.field
        elif primary.code == INVALID_IDENTIFIER:
            details["field"] = primary.pointer.rsplit("/", 1)[-1] or "/"
        message = {
            INVALID_IDENTIFIER: f"invalid identifier in field '{details.get('field')}'",
            OPERATION_NOT_PERMITTED: f"operation not permitted at '{primary.pointer or '/'}'",
        }.get(primary.code, f"validation failed at '{primary.pointer or '/'}'")
        return {
            "code": primary.code,
            "message": message,
            "retryable": False,
            "details": details,
            "correlation_id": correlation_id,
            "contract_version": contract_version or load_store().version,
        }


# ------------------------------------------------------------- semantic checks
@dataclass
class CheckContext:
    document: Any
    info: SchemaInfo
    store: SchemaStore
    context: dict[str, Any]


CheckFn = Callable[[CheckContext], list[ValidationIssue]]
CHECKS: dict[str, CheckFn] = {}


def register_check(name: str) -> Callable[[CheckFn], CheckFn]:
    """Register a semantic check referenced by name from a schema's ``x-finplan-checks``."""

    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return deco


# ------------------------------------------------------------------- helpers
def _esc(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def pointer_of(path: Iterable[Any]) -> str:
    return "".join("/" + _esc(p) for p in path)


def resolve_pointer(doc: Any, pointer: str) -> tuple[bool, Any]:
    if pointer in ("", "/"):
        return True, doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur = cur[int(tok)]
        else:
            return False, None
    return True, cur


def iter_strings(doc: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], str]]:
    if isinstance(doc, str):
        yield path, doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from iter_strings(v, path + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from iter_strings(v, path + (i,))


_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


def _safe(value: Any) -> str:
    """Echo a value only if it is a short, harmless token; otherwise redact."""
    return f"'{value}'" if isinstance(value, str) and _SAFE_TOKEN.match(value) else "<redacted>"


def _issue_from_error(err: ValidationError, prefix: tuple[Any, ...] = ()) -> ValidationIssue:
    if err.context:
        sub = best_match(err.context)
        if sub is not None:
            return _issue_from_error(sub, prefix)
    path = prefix + tuple(err.absolute_path)
    schema = err.schema if isinstance(err.schema, dict) else {}
    code = schema.get("x-finplan-error-code", VALIDATION_FAILED)
    kw = err.validator
    field_name: str | None = next((str(p) for p in reversed(path) if isinstance(p, str)), None)
    vv = err.validator_value
    if kw == "required":
        m = re.match(r"^'(.+)' is a required property$", err.message)
        missing = m.group(1) if m else None
        if missing is None and isinstance(vv, list) and isinstance(err.instance, dict):
            missing = next((r for r in vv if r not in err.instance), None)
        if missing is not None:
            path = path + (missing,)
            field_name = missing
        message = f"missing required property '{field_name}'"
    elif kw == "additionalProperties" and isinstance(err.instance, dict):
        allowed = set(schema.get("properties", {}))
        extras = sorted(k for k in err.instance if k not in allowed)
        if extras:
            path = path + (extras[0],)
            field_name = extras[0]
        message = "unknown propert" + ("ies " if len(extras) > 1 else "y ") + ", ".join(f"'{e}'" if _SAFE_TOKEN.match(e) else "<redacted>" for e in extras)
    elif "x-finplan-identifier" in schema:
        ident = schema["x-finplan-identifier"]
        message = f"field '{field_name}' must be a valid {ident}"
    elif kw == "pattern":
        message = "value does not match the required format"
    elif kw == "enum":
        message = "value is not one of the allowed values: " + ", ".join(map(str, vv))
    elif kw == "const":
        message = f"value must be {json.dumps(vv)}"
    elif kw == "type":
        message = f"expected type {json.dumps(vv)}"
    elif kw == "not" and isinstance(vv, dict) and isinstance(vv.get("required"), list) and isinstance(err.instance, dict):
        present = [r for r in vv["required"] if r in err.instance]
        if len(present) == 1:
            path = path + (present[0],)
            field_name = present[0]
        message = "propert" + ("ies " if len(present) > 1 else "y ") + ", ".join(f"'{r}'" for r in present) + " not allowed in this state"
    elif kw == "not":
        message = "value matches a forbidden form"
    elif kw == "propertyNames":
        message = "property name is not allowed"
    elif kw in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "multipleOf"):
        message = f"value violates {kw} {vv}"
    elif kw == "uniqueItems":
        message = "array items must be unique"
    elif kw == "contains":
        message = "array does not contain a required item"
    elif kw == "format":
        message = f"value is not a valid {vv}"
    else:
        message = f"value violates '{kw}'"
    pointer = pointer_of(path)
    return ValidationIssue(
        code=code,
        pointer=pointer,
        message=f"{pointer or '/'}: {message}",
        field=field_name,
        keyword=kw,
        schema_path=pointer_of(err.absolute_schema_path),
    )


def _schema_issues(store: SchemaStore, info: SchemaInfo, document: Any, prefix: tuple[Any, ...] = ()) -> list[ValidationIssue]:
    validator = store.validator(info.id)
    return [_issue_from_error(e, prefix) for e in validator.iter_errors(document)]


def _sorted(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for i in sorted(issues, key=lambda i: (i.pointer, i.code, i.message)):
        key = (i.code, i.pointer, i.message)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


# --------------------------------------------------------------------- public
def validate(
    document: Any,
    schema: str,
    *,
    context: dict[str, Any] | None = None,
    store: SchemaStore | None = None,
    root: str | Path | None = None,
) -> ValidationResult:
    """Validate ``document`` against ``schema`` (name, ``ns/name`` or ``$id``).

    ``context`` carries values a semantic check needs from outside the document,
    for example ``{"cost_ceiling_usd": 50}`` for the budget allocation.
    """
    store = store or load_store(root)
    info = store.get(schema)
    issues = _schema_issues(store, info, document)
    ctx = CheckContext(document=document, info=info, store=store, context=dict(context or {}))
    for name in info.checks:
        fn = CHECKS.get(name)
        if fn is None:
            raise KeyError(f"schema {info.name} declares unknown semantic check {name!r}")
        issues.extend(fn(ctx))
    return ValidationResult(schema_id=info.id, schema_name=info.name, issues=_sorted(issues))


def is_valid(document: Any, schema: str, **kwargs: Any) -> bool:
    return validate(document, schema, **kwargs).valid


# ------------------------------------------------------------ registered checks
def _fail(pointer: str, message: str, field: str | None = None, code: str = VALIDATION_FAILED, keyword: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, pointer=pointer, message=f"{pointer or '/'}: {message}", field=field, keyword=keyword)


def _domain_entry(store: SchemaStore, domain: Any) -> dict[str, Any] | None:
    for entry in store.domain_registry().get("domains", []):
        if entry.get("domain") == domain:
            return entry
    return None


@register_check("domain_payload")
def check_domain_payload(ctx: CheckContext) -> list[ValidationIssue]:
    """Domain registered, version served, and each declared payload valid for its adapter (DOM-01/02)."""
    doc, store = ctx.document, ctx.store
    if not isinstance(doc, dict):
        return []
    issues: list[ValidationIssue] = []

    def check_domain_at(base: str) -> dict[str, Any] | None:
        ok, holder = resolve_pointer(doc, base)
        if not ok or not isinstance(holder, dict) or "domain" not in holder:
            return None
        domain = holder["domain"]
        entry = _domain_entry(store, domain)
        if entry is None:
            issues.append(_fail(f"{base}/domain", f"domain {_safe(domain)} is not registered", field="domain", keyword="x-finplan-domain"))
            return None
        dsv = holder.get("domain_schema_version")
        if dsv is not None and dsv not in entry.get("domain_schema_versions", []):
            issues.append(_fail(f"{base}/domain_schema_version", f"domain_schema_version {json.dumps(dsv) if isinstance(dsv, str) and len(dsv) < 16 else '<redacted>'} is not served by domain {_safe(domain)}", field="domain_schema_version", keyword="x-finplan-domain"))
            return None
        return entry

    root_entry = check_domain_at("")
    for spec in ctx.info.schema.get("x-finplan-domain-payloads", []):
        pointer = spec["pointer"]
        ok, payload = resolve_pointer(doc, pointer)
        if not ok:
            continue  # absence is handled by "required" in the schema itself
        dom_ptr = spec.get("domain_pointer", "")
        entry = root_entry if dom_ptr == "" else check_domain_at(dom_ptr)
        if dom_ptr:
            ok_r, _ = resolve_pointer(doc, "/domain")
            ok_n, nested = resolve_pointer(doc, f"{dom_ptr}/domain")
            if ok_r and ok_n and nested != doc.get("domain"):
                issues.append(_fail(f"{dom_ptr}/domain", "nested domain differs from the envelope domain", field="domain"))
        if entry is None:
            continue
        if "kind_pointer" in spec:
            okk, kind = resolve_pointer(doc, spec["kind_pointer"])
            if not okk:
                continue
        else:
            kind = spec["kind"]
        target = entry.get("payload_schemas", {}).get(kind)
        if target is None:
            issues.append(_fail(spec.get("kind_pointer", pointer), f"payload kind {_safe(kind)} is not registered for domain {_safe(entry['domain'])}", keyword="x-finplan-domain"))
            continue
        try:
            payload_info = store.get(target)
        except SchemaNotFound:
            issues.append(_fail(pointer, "registered payload schema is missing from the package", keyword="x-finplan-domain"))
            continue
        prefix = tuple(t.replace("~1", "/").replace("~0", "~") for t in pointer.lstrip("/").split("/")) if pointer not in ("", "/") else ()
        prefix = tuple(int(t) if t.isdigit() else t for t in prefix)
        issues.extend(_schema_issues(store, payload_info, payload, prefix))
    return issues


#: Raw storage locations: storage URIs, ARNs, S3 endpoints. Used for requests and errors.
_STORAGE_URI = re.compile(r"(?i)(\b(s3a?|s3n|gs|hdfs|file|ftp)://|\barn:[a-z0-9-]*:|\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com)")
#: Caller-chosen locations in requests: any URI, absolute/home/relative paths, backslash paths.
_PATH_LIKE = re.compile(r"(?i)(^\s*[a-z][a-z0-9+.-]*://|^\s*(/|~/|\.{1,2}/)|\\)")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)|File \"[^\"]+\", line [0-9]+")


@register_check("no_storage_locations")
def check_no_storage_locations(ctx: CheckContext) -> list[ValidationIssue]:
    """Reject caller-chosen storage locations: s3:// and other URIs, ARNs, path-like values (CS-08)."""
    out = []
    for path, s in iter_strings(ctx.document):
        if s.startswith(ID_BASE):
            continue  # contract $ids are identifiers, never fetched
        # Public research citations are archived evidence, never caller-selected
        # storage destinations. Private storage references remain forbidden.
        if (ctx.info.name == "tools/record-agent-activity-request"
                and path and path[0] == "payload"
                and s.startswith("https://") and not _STORAGE_URI.search(s)):
            continue
        if _STORAGE_URI.search(s) or _PATH_LIKE.search(s):
            p = pointer_of(path)
            out.append(_fail(p, "storage locations, URIs, ARNs and path-like values are not accepted; use a trusted artifact reference", field=next((str(x) for x in reversed(path) if isinstance(x, str)), None), keyword="x-finplan-no-storage-locations"))
    return out


@register_check("no_leaks")
def check_no_leaks(ctx: CheckContext) -> list[ValidationIssue]:
    """Error envelopes must not carry stack traces or raw storage locations (CS-05)."""
    doc = ctx.document
    if not isinstance(doc, dict):
        return []
    out = []
    for key in ("message", "details"):
        if key not in doc:
            continue
        for path, s in iter_strings(doc[key], (key,)):
            if _TRACEBACK.search(s) or _STORAGE_URI.search(s):
                out.append(_fail(pointer_of(path), "error envelopes must not contain stack traces or raw storage locations", keyword="x-finplan-no-leaks"))
    return out


def _error_codes(store: SchemaStore) -> dict[str, dict[str, Any]]:
    return store.get("error-codes").schema.get("x-finplan-error-codes", {})


@register_check("error_retryable")
def check_error_retryable(ctx: CheckContext) -> list[ValidationIssue]:
    """Codes registered with a fixed retryable value must carry it (CS-06)."""
    doc = ctx.document
    if not isinstance(doc, dict):
        return []
    reg = _error_codes(ctx.store).get(doc.get("code"))
    if reg and reg.get("retryable_fixed") and isinstance(doc.get("retryable"), bool) and doc["retryable"] != reg["retryable"]:
        return [_fail("/retryable", f"code {doc['code']} must have retryable {json.dumps(reg['retryable'])}", field="retryable")]
    return []


@register_check("budget_allocation_within_ceiling")
def check_budget_allocation(ctx: CheckContext) -> list[ValidationIssue]:
    """Budget categories must sum to at most the cost ceiling (ENV-17).

    The ceiling comes from ``context['cost_ceiling_usd']`` (the value of
    ``/finplan/shared/financialplanning/config/cost-ceiling-usd``), defaulting to the
    registered default ceiling (USD 50).
    """
    doc = ctx.document
    if not isinstance(doc, dict):
        return []
    ceiling = ctx.context.get("cost_ceiling_usd", ctx.info.schema.get("x-finplan-default-ceiling-usd"))
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        return [_fail("", "cost ceiling is unknown", keyword="x-finplan-budget")]
    values = [v for v in doc.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    total = sum(values)
    if total > ceiling + 1e-9:
        return [_fail("", f"budget categories sum to {total:g} USD, above the ceiling of {ceiling:g} USD", keyword="x-finplan-budget")]
    return []


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@register_check("idempotency_retention")
def check_idempotency_retention(ctx: CheckContext) -> list[ValidationIssue]:
    """Idempotency records are retained at least the registered minimum (7 days)."""
    doc = ctx.document
    if not isinstance(doc, dict):
        return []
    start, end = _parse_ts(doc.get("recorded_at")), _parse_ts(doc.get("retain_until"))
    days = ctx.info.schema.get("x-finplan-min-retention-days", 7)
    if start and end and end - start < timedelta(days=days):
        return [_fail("/retain_until", f"idempotency records must be retained at least {days} days", field="retain_until")]
    return []


@register_check("schema_ids_resolve")
def check_schema_ids_resolve(ctx: CheckContext) -> list[ValidationIssue]:
    """Every contract ``$id`` named in the document exists in this package version."""
    out = []
    for path, s in iter_strings(ctx.document):
        if s.startswith(ID_BASE) and s not in ctx.store:
            out.append(_fail(pointer_of(path), "schema $id does not resolve in this contract version", keyword="x-finplan-schema-id"))
    return out


@register_check("tool_catalog_environment")
def check_tool_catalog_environment(ctx: CheckContext) -> list[ValidationIssue]:
    """Catalog entries are unique and reference Lambda parameters of the catalog's environment."""
    doc = ctx.document
    if not isinstance(doc, dict) or not isinstance(doc.get("tools"), list):
        return []
    out, seen = [], set()
    env = doc.get("environment")
    for i, t in enumerate(doc["tools"]):
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if name in seen:
            out.append(_fail(f"/tools/{i}/name", "duplicate tool name", field="name"))
        seen.add(name)
        ref = t.get("lambda_ref_parameter")
        if isinstance(ref, str) and ref.count("/") >= 3 and ref.split("/")[2] != env:
            out.append(_fail(f"/tools/{i}/lambda_ref_parameter", "Lambda reference parameter is not in the catalog's environment", field="lambda_ref_parameter"))
    return out


@register_check("manifest_outputs_own_segment")
def check_manifest_outputs(ctx: CheckContext) -> list[ValidationIssue]:
    """Manifest outputs are parameters under the repo's own segment, same environment or shared (ENV-07)."""
    doc = ctx.document
    if not isinstance(doc, dict) or not isinstance(doc.get("outputs"), dict):
        return []
    out = []
    for key, name in doc["outputs"].items():
        if not isinstance(name, str) or name.count("/") < 5:
            continue
        _, _, env, repo, *_ = name.split("/")
        if repo != doc.get("repo"):
            out.append(_fail(f"/outputs/{_esc(key)}", "output parameter is outside the repository's own segment", field=key))
        elif env not in (doc.get("environment"), "shared"):
            out.append(_fail(f"/outputs/{_esc(key)}", "output parameter is in another environment", field=key))
    return out


# GPU approval rule (ENV-20, task 13.5): job-status schemas declare
# ``gpu_approval_required``; the rule itself lives in ``gpu_rule``.
from . import gpu_rule as _gpu_rule  # noqa: E402  (needs CHECKS/register_check above)

_gpu_rule.register_semantic_check()


# ------------------------------------------------------------------------ CLI
def _parse_context(items: list[str]) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    for item in items:
        key, _, raw = item.partition("=")
        try:
            ctx[key] = json.loads(raw)
        except json.JSONDecodeError:
            ctx[key] = raw
    return ctx


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="finplan-conformance validate", description="Validate JSON documents against a contract schema.")
    parser.add_argument("--schema", "-s", required=True, help="schema name (e.g. plan-version), ns/name or $id")
    parser.add_argument("--root", help="contracts root (default: bundled or source checkout)")
    parser.add_argument("--context", action="append", default=[], metavar="KEY=JSON", help="semantic-check context, e.g. cost_ceiling_usd=50")
    parser.add_argument("--envelope", action="store_true", help="print an error envelope for invalid documents")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        store = load_store(args.root)
        store.get(args.schema)
    except (SchemaNotFound, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ctx = _parse_context(args.context)
    rc = 0
    for f in args.files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"file": str(f), "valid": False, "error": f"unreadable JSON: {type(exc).__name__}"}))
            rc = 1
            continue
        res = validate(doc, args.schema, context=ctx, store=store)
        out = {"file": str(f), **res.to_dict()}
        if args.envelope and not res.valid:
            out["envelope"] = res.to_error_envelope(correlation_id="cli-validate-0001", contract_version=store.version)
        print(json.dumps(out, sort_keys=True))
        if not res.valid:
            rc = 1
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
