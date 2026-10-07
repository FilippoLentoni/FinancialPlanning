"""Operation context: who is calling, in which environment, under which correlation ID and clock.

Every core operation takes an :class:`OperationContext` as its first argument. Handlers
build it from the transport (API Gateway IAM identity, scheduler event, test harness);
operations never read globals for identity, time or environment.

* ``caller.principal`` is the authenticated IAM principal (from the API Gateway request
  context ``userArn``/``caller``), never a value taken from the request body. It scopes
  idempotency records and is recorded in audit events.
* ``caller.on_behalf_of`` is the optional contract ``core/v1/caller.json`` block a trusted
  hop forwards in a transport header. It is recorded for audit and never grants authority.
* ``correlation_id`` matches the contract pattern and is echoed in every error envelope.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from .clock import Clock, FrozenClock, SystemClock, to_timestamp
from .ids import IdFactory

__all__ = ["Caller", "OperationContext", "new_correlation_id", "CORRELATION_ID_RE", "ENVIRONMENTS", "TRIGGERS"]

ENVIRONMENTS = ("beta", "gamma", "prod")
CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")
TRIGGERS = ("on_demand", "scheduled")


def new_correlation_id() -> str:
    return "cor_" + secrets.token_hex(12)


@dataclass(frozen=True)
class Caller:
    """The authenticated caller.

    ``principal``: IAM principal ARN or a stable principal name (scheduler: the schedule's
    role; tests: any non-empty string). ``role_class`` is the FinanceLambdasTool role class
    (``reader``/``submitter``/``plan-writer``) or a platform class (``website``,
    ``operator``, ``platform``, ``financemodel-job``, ``financemodel-job-api``) when the
    handler resolved it; operations may use it for defence-in-depth checks.
    """

    principal: str
    role_class: str | None = None
    channel: str | None = None
    on_behalf_of: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal:
            raise ValueError("caller principal must be a non-empty string")

    def audit_view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"principal": self.principal}
        if self.role_class:
            out["role_class"] = self.role_class
        if self.channel:
            out["channel"] = self.channel
        if self.on_behalf_of:
            out["on_behalf_of"] = dict(self.on_behalf_of)
        return out


@dataclass(frozen=True)
class OperationContext:
    caller: Caller
    env: str
    correlation_id: str
    clock: Clock = field(default_factory=SystemClock)
    ids: IdFactory | None = None
    trigger: str = "on_demand"
    synthetic: bool = False

    def __post_init__(self) -> None:
        if self.env not in ENVIRONMENTS:
            raise ValueError(f"env must be one of {ENVIRONMENTS}, got {self.env!r}")
        if not CORRELATION_ID_RE.match(self.correlation_id or ""):
            raise ValueError("correlation_id does not match the contract pattern")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"trigger must be one of {TRIGGERS}")
        if self.ids is None:
            object.__setattr__(self, "ids", IdFactory(self.clock))

    # ------------------------------------------------------------- helpers
    def now(self) -> datetime:
        return self.clock.now()

    def now_ts(self) -> str:
        return to_timestamp(self.clock.now())

    def new_id(self, kind: str) -> str:
        assert self.ids is not None
        return self.ids.new(kind)

    def with_trigger(self, trigger: str) -> "OperationContext":
        return replace(self, trigger=trigger)

    @classmethod
    def for_test(
        cls,
        *,
        principal: str = "arn:aws:iam::<account-id>:role/finplan-beta-financialplanning-test",
        env: str = "beta",
        correlation_id: str | None = None,
        clock: Clock | None = None,
        role_class: str | None = None,
        channel: str | None = "direct_test",
        seed: int | None = None,
        trigger: str = "on_demand",
    ) -> "OperationContext":
        clk = clock or FrozenClock()
        return cls(
            caller=Caller(principal=principal, role_class=role_class, channel=channel),
            env=env,
            correlation_id=correlation_id or new_correlation_id(),
            clock=clk,
            ids=IdFactory(clk, seed=seed),
            trigger=trigger,
            synthetic=True,
        )
