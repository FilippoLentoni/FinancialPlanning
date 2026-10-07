"""Append-only audit events (plan-metadata-store, "Append-only audit events"; task 3.5, MDS-06).

Every state change appends one :class:`AuditEvent` in the same transaction as the change
(the repository adds it to the ``TransactWriteItems`` call). Events are keyed by
``record_id`` (partition) and ``<timestamp>#<event_id>`` (sort), written with
``attribute_not_exists`` so an event can never be overwritten, and the audit table's
resource policy denies ``UpdateItem``/``DeleteItem``/``BatchWriteItem`` and PartiQL
writes to every principal (``infra/stacks/metadata.py``; policy simulation in the unit
suite). No application code path updates or deletes an event.

An event records: ``event_id``, ``record_id``, ``record_type``, ``operation``, the caller
(principal plus optional role class, channel and on-behalf-of block), ``correlation_id``,
the prior and new state (status and/or revision), free-form ``details`` (for example a
publication's ``plan_version_id`` and checksum) and the timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .context import OperationContext

__all__ = ["AuditEvent", "audit_event", "AUDIT_SORT_SEPARATOR"]

AUDIT_SORT_SEPARATOR = "#"


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    record_id: str
    record_type: str
    operation: str
    caller: Mapping[str, Any]
    correlation_id: str
    environment: str
    at: str
    prior: Mapping[str, Any] | None = None
    new: Mapping[str, Any] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    trigger: str = "on_demand"

    @property
    def sort_key(self) -> str:
        return f"{self.at}{AUDIT_SORT_SEPARATOR}{self.event_id}"

    def to_doc(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "record_id": self.record_id,
            "record_type": self.record_type,
            "operation": self.operation,
            "caller": dict(self.caller),
            "correlation_id": self.correlation_id,
            "environment": self.environment,
            "at": self.at,
            "prior": dict(self.prior) if self.prior is not None else None,
            "new": dict(self.new) if self.new is not None else None,
            "details": dict(self.details),
            "trigger": self.trigger,
        }

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> "AuditEvent":
        return cls(
            event_id=doc["event_id"],
            record_id=doc["record_id"],
            record_type=doc["record_type"],
            operation=doc["operation"],
            caller=doc["caller"],
            correlation_id=doc["correlation_id"],
            environment=doc["environment"],
            at=doc["at"],
            prior=doc.get("prior"),
            new=doc.get("new"),
            details=doc.get("details") or {},
            trigger=doc.get("trigger", "on_demand"),
        )


def audit_event(
    ctx: OperationContext,
    *,
    record_id: str,
    record_type: str,
    operation: str,
    prior: Mapping[str, Any] | None = None,
    new: Mapping[str, Any] | None = None,
    **details: Any,
) -> AuditEvent:
    """Build the audit event for a state change performed under ``ctx``."""
    return AuditEvent(
        event_id=ctx.new_id("audit_event_id"),
        record_id=record_id,
        record_type=record_type,
        operation=operation,
        caller=ctx.caller.audit_view(),
        correlation_id=ctx.correlation_id,
        environment=ctx.env,
        at=ctx.clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        prior=prior,
        new=new,
        details=details,
        trigger=ctx.trigger,
    )
