"""Metadata repository over DynamoDB (plan-metadata-store; tasks 3.1-3.5).

One injected low-level DynamoDB client (boto3 ``client("dynamodb")`` in Lambda, moto or
:class:`tests.fakes.dynamodb.FakeDynamoDB` in tests) and one table per record type
(design P3). Every state change goes through :meth:`MetadataRepository.commit`, a single
``TransactWriteItems`` call that carries the state change, the idempotency record and
the audit event(s); either all of them commit or none does (MDS-03, MDS-05).

Table layout (shared by the CDK metadata stack, the fake and moto; :data:`TABLES`)
-------------------------------------------------------------------------------
* partition key ``pk`` (string, the record's platform ID); the audit table adds sort
  key ``sk`` (``<timestamp>#<event_id>``);
* ``doc``: the contract record as RFC 8785 canonical JSON (exact numbers, exact bytes);
* top-level scalar attributes copied from the doc for conditions and indexes
  (``status``, ``plan_id``, ``dataset``, ...) and head attributes that are *not* part of
  the immutable doc (``revision``, ``current_version_id``, ``publication_revision``,
  ``current_publication_id``); readers overlay them (:meth:`Record.head`).

Write primitives (:class:`Op` subclasses)
-----------------------------------------
* :class:`PutNew`: insert, conditioned on the ID not existing. A collision is an attempt
  to overwrite a committed record: ``IMMUTABLE_RECORD`` (MDS-04).
* :class:`HeadMove`: optimistic-concurrency update of a head (``revision = :expected``),
  incrementing the revision: ``CONFLICT`` on mismatch, ``NOT_FOUND`` when absent.
* :class:`Transition`: status change conditioned on the current status and checked
  against the allowed transitions; the doc may change only in the table's mutable fields
  (otherwise ``IMMUTABLE_RECORD``): ``PRECONDITION_FAILED`` on a stale status.
* :class:`Check`: a ``ConditionCheck`` (for example "portfolio exists").
* :class:`PutConditional`: a put with an explicit condition (for example one staged
  output outcome per ``run_id``) and an explicit error.

Cancellation mapping: the first ``ConditionalCheckFailed`` reason is mapped through the
failing op's ``on_fail``; a failed idempotency put takes priority (it means a concurrent
duplicate committed first) and resolves to a replay or ``IDEMPOTENCY_KEY_REUSED``;
``TransactionConflict`` is retried a bounded number of times; throttling and service
errors become ``DEPENDENCY_UNAVAILABLE`` (retryable).

Idempotency (:meth:`MetadataRepository.run_idempotent`)
-------------------------------------------------------
Scope (caller principal, environment, operation) plus ``idempotency_key``; the request
hash is the contract ``request_hash`` (JCS SHA-256) of the request body. A repeat with
the same hash returns the stored response (``replayed``), a different hash fails with
``IDEMPOTENCY_KEY_REUSED``. Records carry a DynamoDB TTL of ``idempotency_ttl_days``
(8, at least the contract's 7).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from botocore.exceptions import ClientError
from finplan_contracts.canonical import canonical_text, request_hash
from finplan_contracts.keys import IDEMPOTENCY_KEY_PATTERN

from .audit import AuditEvent
from .clock import epoch_seconds, to_timestamp
from .context import OperationContext
from .errors import PlatformError

__all__ = [
    "IndexSpec",
    "TableSpec",
    "TABLES",
    "TRANSITIONS",
    "table_name",
    "table_names",
    "create_tables",
    "Cond",
    "Op",
    "PutNew",
    "PutConditional",
    "HeadMove",
    "Transition",
    "Check",
    "Record",
    "IdempotencyScope",
    "IdempotencyWrite",
    "Mutation",
    "IdempotentOutcome",
    "MetadataRepository",
    "encode_page_token",
    "decode_page_token",
    "split_heads",
    "overlay_heads",
]


# ===================================================================== table specs
@dataclass(frozen=True)
class IndexSpec:
    name: str
    partition_key: str
    sort_key: str | None = None


@dataclass(frozen=True)
class TableSpec:
    logical: str
    logical_role: str  # ownership-matrix logical-role tag value
    id_field: str | None
    sort_key: str | None = None
    indexes: tuple[IndexSpec, ...] = ()
    ttl_attribute: str | None = None
    #: doc fields a Transition may change; everything else is immutable content
    mutable_fields: tuple[str, ...] = ()
    #: top-level attributes that are mutable heads, not part of the doc
    head_attributes: tuple[str, ...] = ()
    #: doc fields copied to top-level attributes (conditions, indexes, sweeps);
    #: ``"attr:dotted.path"`` copies a nested field under another attribute name
    attribute_fields: tuple[str, ...] = ()


TABLES: dict[str, TableSpec] = {
    "portfolio": TableSpec("portfolio", "portfolio-table", "portfolio_id", head_attributes=("revision", "paper_state_revision", "paper_state"), attribute_fields=("portfolio_id", "synthetic")),
    "plan": TableSpec(
        "plan",
        "plan-table",
        "plan_id",
        indexes=(IndexSpec("portfolio-index", "portfolio_id", "plan_id"),),
        head_attributes=("revision", "current_version_id", "publication_revision", "current_publication_id"),
        attribute_fields=("plan_id", "portfolio_id"),
    ),
    "plan_version": TableSpec(
        "plan_version",
        "plan-version-table",
        "plan_version_id",
        indexes=(IndexSpec("plan-index", "plan_id", "plan_version_id"),),
        mutable_fields=("status", "validation_errors"),
        attribute_fields=("plan_version_id", "plan_id", "status", "checksum", "parent_plan_version_id", "origin", "run_id"),
    ),
    "publication": TableSpec(
        "publication",
        "publication-table",
        "publication_id",
        indexes=(IndexSpec("plan-index", "plan_id", "publication_id"),),
        attribute_fields=("publication_id", "plan_id", "plan_version_id"),
    ),
    "execution": TableSpec(
        "execution",
        "execution-table",
        "execution_id",
        indexes=(IndexSpec("publication-index", "publication_id", "execution_id"),),
        mutable_fields=("status",),
        attribute_fields=("execution_id", "publication_id", "status", "mode"),
    ),
    "snapshot_catalog": TableSpec(
        "snapshot_catalog",
        "snapshot-catalog-table",
        "input_snapshot_id",
        indexes=(IndexSpec("dataset-index", "dataset_id", "input_snapshot_id"),),
        mutable_fields=("status", "approval_rule_version"),
        attribute_fields=("input_snapshot_id", "dataset_id:dataset.dataset_id", "status", "created_at"),
    ),
    "staged_output": TableSpec("staged_output", "staged-output-table", "run_id", attribute_fields=("run_id", "plan_id", "outcome")),
    "idempotency": TableSpec("idempotency", "idempotency-table", None, ttl_attribute="expires_at"),
    "audit_event": TableSpec("audit_event", "audit-event-table", None, sort_key="sk"),
    "portfolio_decision": TableSpec(
        "portfolio_decision", "portfolio-decision-table", "decision_id",
        indexes=(IndexSpec("portfolio-index", "portfolio_id", "decision_id"),),
        mutable_fields=("status", "resolution_key", "resolution_checksum"),
        attribute_fields=("decision_id", "portfolio_id", "status", "input_snapshot_id", "portfolio_revision"),
    ),
    "portfolio_history": TableSpec(
        "portfolio_history", "portfolio-history-table", "history_id",
        indexes=(IndexSpec("portfolio-index", "portfolio_id", "history_id"),),
        attribute_fields=("portfolio_id", "history_id"),
    ),
    "activity_event": TableSpec(
        "activity_event", "activity-event-table", "activity_event_id",
        indexes=(IndexSpec("portfolio-index", "portfolio_id", "activity_event_id"), IndexSpec("session-index", "session_id", "activity_event_id")),
        attribute_fields=("activity_event_id", "portfolio_id", "session_id"),
    ),
}

#: Allowed status transitions per table (contract lifecycle; MDS-04).
TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "plan_version": {"pending_validation": frozenset({"validated", "invalid"}), "validated": frozenset(), "invalid": frozenset()},
    "snapshot_catalog": {"committed": frozenset({"approved", "expired"}), "approved": frozenset({"expired"}), "expired": frozenset()},
    # execution status is an open vocabulary in contracts 0.1/1.0; transitions are declared by the API module
    "execution": {},
    "portfolio_decision": {"proposed": frozenset({"accepted", "rejected"}), "accepted": frozenset(), "rejected": frozenset()},
}


def table_name(env: str, logical: str) -> str:
    """Physical table name ``finplan-<env>-financialplanning-<logical-with-dashes>``."""
    if logical not in TABLES:
        raise KeyError(logical)
    return f"finplan-{env}-financialplanning-{logical.replace('_', '-')}"


def table_names(env: str) -> dict[str, str]:
    return {t: table_name(env, t) for t in TABLES}


def create_tables(client: Any, env: str) -> dict[str, str]:
    """Create every table (tests: moto or the fake). Mirrors the CDK metadata stack."""
    names = table_names(env)
    for logical, spec in TABLES.items():
        attrs = {"pk"}
        keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
        if spec.sort_key:
            keys.append({"AttributeName": spec.sort_key, "KeyType": "RANGE"})
            attrs.add(spec.sort_key)
        gsis = []
        for ix in spec.indexes:
            ks = [{"AttributeName": ix.partition_key, "KeyType": "HASH"}]
            attrs.add(ix.partition_key)
            if ix.sort_key:
                ks.append({"AttributeName": ix.sort_key, "KeyType": "RANGE"})
                attrs.add(ix.sort_key)
            gsis.append({"IndexName": ix.name, "KeySchema": ks, "Projection": {"ProjectionType": "ALL"}})
        kwargs: dict[str, Any] = {
            "TableName": names[logical],
            "KeySchema": keys,
            "AttributeDefinitions": [{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = gsis
        client.create_table(**kwargs)
    return names


# ===================================================================== serialization
def _to_av(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, Decimal)):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": repr(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, Mapping):
        return {"M": {k: _to_av(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_av(v) for v in value]}
    raise TypeError(f"cannot store {type(value).__name__} in DynamoDB")


def _from_av(av: Mapping[str, Any]) -> Any:
    ((t, v),) = av.items()
    if t == "NULL":
        return None
    if t == "BOOL":
        return bool(v)
    if t == "N":
        d = Decimal(v)
        return int(d) if d == d.to_integral_value() and "." not in v and "e" not in v.lower() else float(d)
    if t == "S":
        return v
    if t == "M":
        return {k: _from_av(x) for k, x in v.items()}
    if t == "L":
        return [_from_av(x) for x in v]
    if t == "SS":
        return set(v)
    raise TypeError(f"unsupported attribute type {t}")


def _item_to_av(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _to_av(v) for k, v in item.items()}


def _item_from_av(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _from_av(v) for k, v in item.items()}


# ===================================================================== conditions
class Cond:
    """A tiny condition-expression builder (rendered with placeholders, never string-concatenated values)."""

    def __init__(self, kind: str, *args: Any) -> None:
        self.kind = kind
        self.args = args

    @staticmethod
    def not_exists(attr: str = "pk") -> "Cond":
        return Cond("not_exists", attr)

    @staticmethod
    def exists(attr: str = "pk") -> "Cond":
        return Cond("exists", attr)

    @staticmethod
    def eq(attr: str, value: Any) -> "Cond":
        return Cond("cmp", attr, "=", value)

    @staticmethod
    def ne(attr: str, value: Any) -> "Cond":
        return Cond("cmp", attr, "<>", value)

    @staticmethod
    def in_(attr: str, values: Sequence[Any]) -> "Cond":
        if not values:
            raise ValueError("IN needs at least one value")
        return Cond("in", attr, tuple(values))

    def __and__(self, other: "Cond") -> "Cond":
        return Cond("and", self, other)

    def __or__(self, other: "Cond") -> "Cond":
        return Cond("or", self, other)


class _Renderer:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.values: dict[str, Any] = {}

    def name(self, attr: str) -> str:
        for k, v in self.names.items():
            if v == attr:
                return k
        ph = f"#n{len(self.names)}"
        self.names[ph] = attr
        return ph

    def value(self, v: Any) -> str:
        ph = f":v{len(self.values)}"
        self.values[ph] = _to_av(v)
        return ph

    def cond(self, c: Cond) -> str:
        if c.kind == "not_exists":
            return f"attribute_not_exists({self.name(c.args[0])})"
        if c.kind == "exists":
            return f"attribute_exists({self.name(c.args[0])})"
        if c.kind == "cmp":
            return f"{self.name(c.args[0])} {c.args[1]} {self.value(c.args[2])}"
        if c.kind == "in":
            return f"{self.name(c.args[0])} IN ({', '.join(self.value(v) for v in c.args[1])})"
        if c.kind in ("and", "or"):
            return f"({self.cond(c.args[0])}) {c.kind.upper()} ({self.cond(c.args[1])})"
        raise ValueError(c.kind)

    def apply(self, out: dict[str, Any]) -> dict[str, Any]:
        if self.names:
            out["ExpressionAttributeNames"] = dict(self.names)
        if self.values:
            out["ExpressionAttributeValues"] = dict(self.values)
        return out


# ===================================================================== records and ops
@dataclass(frozen=True)
class Record:
    table: str
    doc: dict[str, Any]
    attrs: dict[str, Any]

    def head(self) -> dict[str, Any]:
        """Mutable head attributes (``revision``, ``current_version_id``, ...)."""
        return {k: self.attrs.get(k) for k in TABLES[self.table].head_attributes}

    def view(self) -> dict[str, Any]:
        """The contract record: the immutable doc with the current head overlaid.

        ``plan`` -> ``head {current_version_id, revision}``, ``current_publication_id`` and
        ``publication_revision`` (the contract plan field since contracts 0.2.0);
        ``portfolio`` -> ``revision``.
        """
        return overlay_heads(self.table, self.doc, self.attrs)

    @property
    def id(self) -> str:
        return str(self.attrs["pk"])


ErrorFactory = Callable[[Record | None], PlatformError]


def split_heads(table: str, doc: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a contract record into the immutable stored doc and its mutable head attributes."""
    stored = dict(doc)
    heads: dict[str, Any] = {}
    if table == "plan":
        head = stored.pop("head", None) or {}
        heads = {
            "revision": int(head.get("revision", 0)),
            "current_version_id": head.get("current_version_id"),
            "current_publication_id": stored.pop("current_publication_id", None),
            "publication_revision": int(stored.pop("publication_revision", 0) or 0),
        }
    elif table == "portfolio":
        heads = {"revision": int(stored.pop("revision", 0))}
    return stored, heads


def overlay_heads(table: str, doc: Mapping[str, Any], attrs: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    if table == "plan":
        out["head"] = {"current_version_id": attrs.get("current_version_id"), "revision": int(attrs.get("revision") or 0)}
        out["current_publication_id"] = attrs.get("current_publication_id")
        out["publication_revision"] = int(attrs.get("publication_revision") or 0)
    elif table == "portfolio":
        out["revision"] = int(attrs.get("revision") or 0)
    return out


def _doc_attrs(spec: TableSpec, doc: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in spec.attribute_fields:
        attr, _, path = f.partition(":")
        value: Any = doc
        for part in (path or attr).split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        if value is not None and not isinstance(value, (dict, list)):
            out[attr] = value
    return out


@dataclass
class Op:
    table: str

    def key(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_fail(self, current: Record | None) -> PlatformError:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class PutNew(Op):
    """Insert a new record (``attribute_not_exists(pk)``). Collision -> ``IMMUTABLE_RECORD``."""

    doc: dict[str, Any] = field(default_factory=dict)
    extra_attrs: dict[str, Any] = field(default_factory=dict)
    contract_version: str | None = None
    error: ErrorFactory | None = None

    def item(self) -> dict[str, Any]:
        spec = TABLES[self.table]
        if spec.id_field is None:
            raise ValueError(f"table {self.table} has no id field; use PutConditional")
        rid = self.doc.get(spec.id_field)
        if not isinstance(rid, str) or not rid:
            raise ValueError(f"doc lacks {spec.id_field}")
        stored, heads = split_heads(self.table, self.doc)
        item = {"pk": rid, **_doc_attrs(spec, stored), **heads, **self.extra_attrs, "doc": canonical_text(stored), "record_type": self.table}
        if self.contract_version:
            item["contract_version"] = self.contract_version
        return item

    def key(self) -> dict[str, Any]:
        return {"pk": self.item()["pk"]}

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:
        r = _Renderer()
        out = {"TableName": names[self.table], "Item": _item_to_av(self.item()), "ConditionExpression": r.cond(Cond.not_exists("pk")), "ReturnValuesOnConditionCheckFailure": "ALL_OLD"}
        return {"Put": r.apply(out)}

    def on_fail(self, current: Record | None) -> PlatformError:
        if self.error:
            return self.error(current)
        return PlatformError.immutable(f"{self.table} record already exists and is immutable", record_type=self.table)


@dataclass
class PutConditional(Op):
    """A put with an explicit condition and error (items of tables without an id field, outcome rows)."""

    item: dict[str, Any] = field(default_factory=dict)
    condition: Cond | None = None
    error: ErrorFactory | None = None

    def key(self) -> dict[str, Any]:
        spec = TABLES[self.table]
        k = {"pk": self.item["pk"]}
        if spec.sort_key:
            k[spec.sort_key] = self.item[spec.sort_key]
        return k

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:
        r = _Renderer()
        out: dict[str, Any] = {"TableName": names[self.table], "Item": _item_to_av(self.item), "ReturnValuesOnConditionCheckFailure": "ALL_OLD"}
        if self.condition is not None:
            out["ConditionExpression"] = r.cond(self.condition)
        return {"Put": r.apply(out)}

    def on_fail(self, current: Record | None) -> PlatformError:
        if self.error:
            return self.error(current)
        return PlatformError.conflict(f"{self.table} write condition failed", record_type=self.table)


@dataclass
class HeadMove(Op):
    """``SET <attrs>, revision = :expected + 1`` conditioned on ``revision = :expected``."""

    record_id: str = ""
    expected_revision: int = 0
    set_attrs: dict[str, Any] = field(default_factory=dict)
    revision_attr: str = "revision"
    error: ErrorFactory | None = None
    #: Legacy rows have no optional head; opt-in initialization treats absence as revision zero.
    allow_missing_revision: bool = False

    def key(self) -> dict[str, Any]:
        return {"pk": self.record_id}

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise PlatformError.validation("expected_revision must be a non-negative integer", pointer="/expected_revision")
        r = _Renderer()
        sets = [f"{r.name(k)} = {r.value(v)}" for k, v in self.set_attrs.items()]
        sets.append(f"{r.name(self.revision_attr)} = {r.value(self.expected_revision + 1)}")
        revision_condition = Cond.eq(self.revision_attr, self.expected_revision)
        if self.allow_missing_revision:
            if self.expected_revision != 0:
                raise PlatformError.validation("only revision zero may initialize a missing head", pointer="/expected_revision")
            revision_condition = revision_condition | Cond.not_exists(self.revision_attr)
        cond = r.cond(Cond.exists("pk") & revision_condition)
        out = {
            "TableName": names[self.table],
            "Key": _item_to_av(self.key()),
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": cond,
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
        return {"Update": r.apply(out)}

    def on_fail(self, current: Record | None) -> PlatformError:
        if self.error:
            return self.error(current)
        if current is None:
            return PlatformError.not_found(f"{self.table} record not found", record_type=self.table)
        return PlatformError.conflict(
            "expected_revision does not match the current revision",
            record_type=self.table,
            expected_revision=self.expected_revision,
            current_revision=current.attrs.get(self.revision_attr),
        )


@dataclass
class Transition(Op):
    """Conditional status transition; ``new_doc`` may differ from ``old_doc`` only in mutable fields."""

    record_id: str = ""
    old_doc: dict[str, Any] = field(default_factory=dict)
    new_doc: dict[str, Any] = field(default_factory=dict)
    status_field: str = "status"
    allowed_fields: tuple[str, ...] = ()
    error: ErrorFactory | None = None
    #: extra top-level attributes to set (bookkeeping outside the contract doc, e.g. ``expired_at``)
    extra_attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = TABLES[self.table]
        mutable = set(spec.mutable_fields) | set(self.allowed_fields) | {self.status_field}
        changed = {k for k in set(self.old_doc) | set(self.new_doc) if self.old_doc.get(k, _MISSING) != self.new_doc.get(k, _MISSING)}
        illegal = sorted(changed - mutable)
        if illegal:
            raise PlatformError.immutable(f"{self.table} content is immutable; create a new record instead", record_type=self.table, fields=illegal)
        frm, to = self.old_doc.get(self.status_field), self.new_doc.get(self.status_field)
        allowed = TRANSITIONS.get(self.table)
        if allowed:
            if to not in allowed.get(frm, frozenset()):
                raise PlatformError.precondition(f"transition {frm} -> {to} is not allowed", record_type=self.table, from_status=frm, to_status=to)

    @property
    def from_status(self) -> Any:
        return self.old_doc.get(self.status_field)

    @property
    def to_status(self) -> Any:
        return self.new_doc.get(self.status_field)

    def key(self) -> dict[str, Any]:
        return {"pk": self.record_id}

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:
        spec = TABLES[self.table]
        r = _Renderer()
        attrs = {k: v for k, v in _doc_attrs(spec, self.new_doc).items() if self.old_doc.get(k) != v}
        attrs[self.status_field] = self.to_status
        attrs.update(self.extra_attrs)
        attrs["doc"] = canonical_text(self.new_doc)
        sets = [f"{r.name(k)} = {r.value(v)}" for k, v in attrs.items()]
        cond = r.cond(Cond.exists("pk") & Cond.eq(self.status_field, self.from_status))
        out = {
            "TableName": names[self.table],
            "Key": _item_to_av(self.key()),
            "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": cond,
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
        return {"Update": r.apply(out)}

    def on_fail(self, current: Record | None) -> PlatformError:
        if self.error:
            return self.error(current)
        if current is None:
            return PlatformError.not_found(f"{self.table} record not found", record_type=self.table)
        return PlatformError.precondition(
            "the record's status changed concurrently",
            record_type=self.table,
            expected_status=self.from_status,
            current_status=current.attrs.get(self.status_field),
        )


_MISSING = object()


@dataclass
class Check(Op):
    record_id: str = ""
    condition: Cond = field(default_factory=lambda: Cond.exists("pk"))
    error: ErrorFactory | None = None

    def key(self) -> dict[str, Any]:
        return {"pk": self.record_id}

    def render(self, names: Mapping[str, str]) -> dict[str, Any]:
        r = _Renderer()
        out = {"TableName": names[self.table], "Key": _item_to_av(self.key()), "ConditionExpression": r.cond(self.condition), "ReturnValuesOnConditionCheckFailure": "ALL_OLD"}
        return {"ConditionCheck": r.apply(out)}

    def on_fail(self, current: Record | None) -> PlatformError:
        if self.error:
            return self.error(current)
        if current is None:
            return PlatformError.not_found(f"{self.table} record not found", record_type=self.table)
        return PlatformError.precondition(f"{self.table} precondition failed", record_type=self.table)


# ===================================================================== idempotency
@dataclass(frozen=True)
class IdempotencyScope:
    principal: str
    environment: str
    operation: str

    def pk(self, idempotency_key: str) -> str:
        raw = f"{self.principal}|{self.environment}|{self.operation}|{idempotency_key}"
        return "idem_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyWrite:
    scope: IdempotencyScope
    idempotency_key: str
    request_hash: str
    response: Mapping[str, Any]
    recorded_at: str
    retain_until: str
    expires_at: int

    def record(self) -> dict[str, Any]:
        """The contract ``core/v1/idempotency.json`` document."""
        return {
            "scope": {"principal": self.scope.principal, "environment": self.scope.environment, "operation": self.scope.operation},
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "response": dict(self.response),
            "recorded_at": self.recorded_at,
            "retain_until": self.retain_until,
        }

    def op(self) -> PutConditional:
        item = {
            "pk": self.scope.pk(self.idempotency_key),
            "doc": canonical_text(self.record()),
            "request_hash": self.request_hash,
            "operation": self.scope.operation,
            "expires_at": self.expires_at,
            "record_type": "idempotency",
        }
        return PutConditional("idempotency", item=item, condition=Cond.not_exists("pk"))


@dataclass
class Mutation:
    """What an idempotent operation wants to commit: state ops, audit events and the response."""

    ops: list[Op]
    response: dict[str, Any]
    audit: list[AuditEvent] = field(default_factory=list)


@dataclass(frozen=True)
class IdempotentOutcome:
    response: dict[str, Any]
    replayed: bool


class _IdempotencyRace(Exception):
    pass


# ===================================================================== page tokens
def encode_page_token(last_key: Mapping[str, Any]) -> str:
    raw = json.dumps(_item_from_av(last_key), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") or "x"


def decode_page_token(token: str) -> dict[str, Any]:
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad))
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise ValueError
        return _item_to_av(data)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise PlatformError.validation("invalid page token", pointer="/page_token") from None


# ===================================================================== repository
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}\Z")
_THROTTLE = {"ThrottlingException", "ProvisionedThroughputExceededException", "RequestLimitExceeded", "InternalServerError", "ServiceUnavailable"}


class MetadataRepository:
    def __init__(self, client: Any, env: str, *, names: Mapping[str, str] | None = None, idempotency_ttl_days: int = 8, transaction_retries: int = 3) -> None:
        if idempotency_ttl_days < 7:
            raise ValueError("idempotency records must be retained for at least 7 days")
        self.client = client
        self.env = env
        self.names = dict(names or table_names(env))
        self.idempotency_ttl_days = idempotency_ttl_days
        self.transaction_retries = transaction_retries

    # -------------------------------------------------------------- reads
    def _record(self, table: str, item: Mapping[str, Any] | None) -> Record | None:
        if not item:
            return None
        attrs = _item_from_av(item)
        doc = json.loads(attrs.pop("doc")) if "doc" in attrs else {}
        return Record(table, doc, attrs)

    def get(self, table: str, record_id: str, sort_key: str | None = None) -> Record | None:
        key: dict[str, Any] = {"pk": record_id}
        spec = TABLES[table]
        if spec.sort_key:
            if sort_key is None:
                raise ValueError(f"{table} needs a sort key")
            key[spec.sort_key] = sort_key
        try:
            resp = self.client.get_item(TableName=self.names[table], Key=_item_to_av(key), ConsistentRead=True)
        except ClientError as exc:
            raise self._service_error(exc) from None
        return self._record(table, resp.get("Item"))

    def require(self, table: str, record_id: str) -> Record:
        rec = self.get(table, record_id)
        if rec is None:
            raise PlatformError.not_found(f"{table.replace('_', ' ')} not found", record_type=table)
        return rec

    def query_index(
        self,
        table: str,
        index: str,
        partition_value: str,
        *,
        newest_first: bool = True,
        limit: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[Record], str | None]:
        """One page of an index partition, ordered by the index sort key (ULIDs: newest first)."""
        spec = TABLES[table]
        ix = next((i for i in spec.indexes if i.name == index), None)
        if ix is None:
            raise KeyError(f"{table} has no index {index}")
        kwargs: dict[str, Any] = {
            "TableName": self.names[table],
            "IndexName": index,
            "KeyConditionExpression": "#p = :p",
            "ExpressionAttributeNames": {"#p": ix.partition_key},
            "ExpressionAttributeValues": {":p": {"S": partition_value}},
            "ScanIndexForward": not newest_first,
            "Limit": int(limit),
        }
        if page_token:
            kwargs["ExclusiveStartKey"] = decode_page_token(page_token)
        try:
            resp = self.client.query(**kwargs)
        except ClientError as exc:
            raise self._service_error(exc) from None
        records = [r for r in (self._record(table, it) for it in resp.get("Items", [])) if r is not None]
        lek = resp.get("LastEvaluatedKey")
        return records, (encode_page_token(lek) if lek else None)

    def scan(self, table: str) -> Iterator[Record]:
        kwargs: dict[str, Any] = {"TableName": self.names[table]}
        while True:
            resp = self.client.scan(**kwargs)
            for it in resp.get("Items", []):
                rec = self._record(table, it)
                if rec is not None:
                    yield rec
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return
            kwargs["ExclusiveStartKey"] = lek

    def audit_events(self, record_id: str) -> list[AuditEvent]:
        """Every audit event of a record, oldest first."""
        out: list[AuditEvent] = []
        kwargs: dict[str, Any] = {
            "TableName": self.names["audit_event"],
            "KeyConditionExpression": "#p = :p",
            "ExpressionAttributeNames": {"#p": "pk"},
            "ExpressionAttributeValues": {":p": {"S": record_id}},
            "ScanIndexForward": True,
        }
        while True:
            resp = self.client.query(**kwargs)
            for it in resp.get("Items", []):
                rec = self._record("audit_event", it)
                if rec is not None:
                    out.append(AuditEvent.from_doc(rec.doc))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return out
            kwargs["ExclusiveStartKey"] = lek

    # -------------------------------------------------------------- commit
    @staticmethod
    def audit_op(event: AuditEvent) -> PutConditional:
        item = {"pk": event.record_id, "sk": event.sort_key, "doc": canonical_text(event.to_doc()), "operation": event.operation, "record_type": "audit_event"}
        return PutConditional(
            "audit_event",
            item=item,
            condition=Cond.not_exists("pk"),
            error=lambda _cur: PlatformError.internal("audit event collision"),
        )

    def commit(self, ops: Sequence[Op], *, audit: Sequence[AuditEvent] = (), idempotency: IdempotencyWrite | None = None) -> None:
        """Commit ops + idempotency record + audit events in ONE transaction (all or nothing)."""
        all_ops: list[Op] = list(ops)
        idem_index: int | None = None
        if idempotency is not None:
            idem_index = len(all_ops)
            all_ops.append(idempotency.op())
        all_ops.extend(self.audit_op(e) for e in audit)
        if not all_ops:
            return
        if len(all_ops) > 100:
            raise ValueError("a transaction holds at most 100 items")
        items = [op.render(self.names) for op in all_ops]
        attempt = 0
        while True:
            attempt += 1
            try:
                self.client.transact_write_items(TransactItems=items)
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code != "TransactionCanceledException":
                    raise self._service_error(exc) from None
                reasons = exc.response.get("CancellationReasons") or _reasons_from_message(exc, len(items))
                failed = [i for i, r in enumerate(reasons) if (r or {}).get("Code") == "ConditionalCheckFailed"]
                if idem_index is not None and idem_index in failed:
                    raise _IdempotencyRace() from None
                if failed:
                    i = failed[0]
                    current = self._record(all_ops[i].table, reasons[i].get("Item")) if reasons[i].get("Item") else self._current(all_ops[i])
                    raise all_ops[i].on_fail(current) from None
                codes = {(r or {}).get("Code") for r in reasons}
                if "TransactionConflict" in codes and attempt <= self.transaction_retries:
                    time.sleep(0.01 * attempt)
                    continue
                if codes & {"ThrottlingError", "ProvisionedThroughputExceeded", "TransactionConflict"}:
                    raise PlatformError("DEPENDENCY_UNAVAILABLE", "metadata store is busy; retry", retryable=True) from None
                raise PlatformError.internal("metadata transaction was cancelled") from None

    def _current(self, op: Op) -> Record | None:
        try:
            k = op.key()
        except Exception:  # pragma: no cover - defensive
            return None
        spec = TABLES[op.table]
        return self.get(op.table, k["pk"], k.get(spec.sort_key) if spec.sort_key else None)

    @staticmethod
    def _service_error(exc: ClientError) -> PlatformError:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE:
            return PlatformError("DEPENDENCY_UNAVAILABLE", "metadata store is unavailable; retry", retryable=True)
        if code == "ResourceNotFoundException":
            return PlatformError("DEPENDENCY_UNAVAILABLE", "metadata store is not provisioned in this environment", retryable=False)
        return PlatformError.internal("metadata store error")

    # -------------------------------------------------------------- idempotency
    def get_idempotency(self, scope: IdempotencyScope, idempotency_key: str) -> dict[str, Any] | None:
        rec = self.get("idempotency", scope.pk(idempotency_key))
        return rec.doc if rec else None

    def run_idempotent(
        self,
        ctx: OperationContext,
        *,
        operation: str,
        idempotency_key: Any,
        request_body: Any,
        execute: Callable[[], Mutation],
    ) -> IdempotentOutcome:
        """Run a state-changing operation exactly once per (principal, env, operation, key)."""
        if not _OPERATION_RE.match(operation):
            raise ValueError(f"operation {operation!r} must match the contract idempotency scope pattern")
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
            raise PlatformError.validation("idempotency_key is required: 1-128 characters from [A-Za-z0-9_-]", pointer="/idempotency_key")
        scope = IdempotencyScope(ctx.caller.principal, ctx.env, operation)
        rhash = request_hash(request_body)
        existing = self.get_idempotency(scope, idempotency_key)
        if existing is not None:
            return self._replay(existing, rhash)
        mutation = execute()
        now = ctx.clock.now()
        retain = now + timedelta(days=self.idempotency_ttl_days)
        write = IdempotencyWrite(scope, idempotency_key, rhash, mutation.response, to_timestamp(now), to_timestamp(retain), epoch_seconds(retain))
        try:
            self.commit(mutation.ops, audit=mutation.audit, idempotency=write)
        except _IdempotencyRace:
            existing = self.get_idempotency(scope, idempotency_key)
            if existing is None:  # pragma: no cover - the record vanished (TTL) between cancel and read
                raise PlatformError("DEPENDENCY_UNAVAILABLE", "idempotency record changed concurrently; retry", retryable=True) from None
            return self._replay(existing, rhash)
        return IdempotentOutcome(dict(mutation.response), replayed=False)

    @staticmethod
    def _replay(record: Mapping[str, Any], rhash: str) -> IdempotentOutcome:
        if record.get("request_hash") != rhash:
            raise PlatformError.key_reused()
        return IdempotentOutcome(dict(record["response"]), replayed=True)

    # -------------------------------------------------------------- composite helpers
    def version_create_ops(
        self,
        *,
        plan_id: str,
        expected_revision: int,
        version_doc: dict[str, Any],
        contract_version: str,
        extra_ops: Iterable[Op] = (),
    ) -> list[Op]:
        """The design P3 transaction body: put version (new ID) + move the plan head.

        Combine with :meth:`run_idempotent` (idempotency record) and an audit event; see
        :func:`version_create_mutation`.
        """
        if version_doc.get("plan_id") != plan_id:
            raise PlatformError.validation("version belongs to another plan", pointer="/plan_id")
        return [
            PutNew("plan_version", doc=version_doc, contract_version=contract_version),
            HeadMove("plan", record_id=plan_id, expected_revision=expected_revision, set_attrs={"current_version_id": version_doc["plan_version_id"]}),
            *extra_ops,
        ]

    def transition(
        self,
        ctx: OperationContext,
        table: str,
        record_id: str,
        *,
        to_status: str,
        changes: Mapping[str, Any] | None = None,
        operation: str,
        allowed_fields: tuple[str, ...] = (),
        extra_ops: Iterable[Op] = (),
        audit_details: Mapping[str, Any] | None = None,
        extra_attrs: Mapping[str, Any] | None = None,
    ) -> Record:
        """Read, check and apply a conditional status transition with its audit event (MDS-04).

        Raises ``PRECONDITION_FAILED`` for a disallowed or stale transition and
        ``IMMUTABLE_RECORD`` when ``changes`` touch immutable content; nothing is written then.
        Returns the updated record.
        """
        cur = self.require(table, record_id)
        new_doc = {**cur.doc, **(changes or {}), "status": to_status}
        op = Transition(table, record_id=record_id, old_doc=cur.doc, new_doc=new_doc, allowed_fields=allowed_fields, extra_attrs=dict(extra_attrs or {}))
        from .audit import audit_event

        ev = audit_event(ctx, record_id=record_id, record_type=table, operation=operation, prior={"status": op.from_status}, new={"status": to_status}, **dict(audit_details or {}))
        self.commit([op, *extra_ops], audit=[ev])
        return Record(table, new_doc, {**cur.attrs, **dict(extra_attrs or {}), "status": to_status})


def version_create_mutation(
    repo: MetadataRepository,
    ctx: OperationContext,
    *,
    plan_id: str,
    expected_revision: int,
    version_doc: dict[str, Any],
    contract_version: str,
    response: dict[str, Any],
    operation: str = "create_plan_version",
    extra_ops: Iterable[Op] = (),
) -> Mutation:
    """Mutation for :meth:`MetadataRepository.run_idempotent`: version + head move + audit (P3)."""
    from .audit import audit_event

    ops = repo.version_create_ops(plan_id=plan_id, expected_revision=expected_revision, version_doc=version_doc, contract_version=contract_version, extra_ops=extra_ops)
    ev_plan = audit_event(
        ctx,
        record_id=plan_id,
        record_type="plan",
        operation=operation,
        prior={"revision": expected_revision},
        new={"revision": expected_revision + 1, "current_version_id": version_doc["plan_version_id"]},
        plan_version_id=version_doc["plan_version_id"],
        checksum=version_doc.get("checksum"),
    )
    return Mutation(ops=ops, response=response, audit=[ev_plan])


def _reasons_from_message(exc: ClientError, n: int) -> list[dict[str, Any]]:
    """Fallback when a client omits CancellationReasons: parse ``[A, B, None]`` from the message."""
    msg = str(exc.response.get("Error", {}).get("Message", ""))
    start, end = msg.rfind("["), msg.rfind("]")
    if start == -1 or end == -1:
        return [{} for _ in range(n)]
    parts = [p.strip() for p in msg[start + 1 : end].split(",")]
    return [{"Code": p} if p and p != "None" else {"Code": "None"} for p in parts][:n] + [{} for _ in range(max(0, n - len(parts)))]


__all__ += ["version_create_mutation"]
