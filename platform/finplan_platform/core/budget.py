"""Budget pre-check for platform operations that start paid or provider work (task 9.4; COST-05).

Spec platform-cost-guardrails, "Budget pre-check for on-demand ingestion": before calling a
provider, the operation reads the account-level budget state and refuses with
``BUDGET_EXCEEDED`` (``retryable`` false) while the enforcement action is active. The flag
``/finplan/shared/financialplanning/config/budget-state`` is written only by the budget-state
writer Lambda of the tooling stack (contract D4's single shared runtime writer;
:mod:`finplan_platform.handlers.budget_state`). Reading one SSM parameter is cheaper and faster
than calling the Budgets API per request (design P9).

Public interface (OPS-owned; the ingestion operation and any later paid operation call it):

``budget_precheck(ctx, category, *, estimated_usd=0.0, state=..., reader=None, allocation=None, spend_records=())``
    Raises :class:`~finplan_platform.core.errors.PlatformError`:

    * ``BUDGET_EXCEEDED`` (not retryable) when the state says ``enforced``, or when
      ``estimated_usd`` exceeds the remaining allocation of ``category``;
    * ``VALIDATION_FAILED`` for an unregistered category or an invalid allocation;
    * ``DEPENDENCY_UNAVAILABLE`` (retryable) when the state cannot be read (fails closed).

    The decision is :func:`finplan_contracts.budget.preflight`, so the platform, FinanceModel
    and FinanceAgent interpret the flag and the allocation identically. Pass ``state`` (the
    raw parameter value, ``None`` = never enforced) or a ``reader`` (a callable returning it,
    for example :class:`SsmBudgetStateReader`); with neither, the state is treated as absent.

``SsmBudgetStateReader(ssm_client, parameter=STATE_PARAMETER)``
    Reads the flag; a missing parameter means "not enforced"; any other error fails closed.

The ingestion module ``core/ingestion_budget.py`` (INGEST-owned) applies the same contract
pre-flight with category ``platform_infra`` and a zero estimate; ``budget_precheck(ctx,
"platform_infra", state=...)`` is the equivalent general form (the unit suite asserts both agree).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from finplan_contracts.budget import CATEGORIES, STATE_PARAMETER, preflight

from .context import OperationContext
from .errors import PlatformError

__all__ = ["CATEGORIES", "PLATFORM_CATEGORY", "STATE_PARAMETER", "SsmBudgetStateReader", "budget_precheck", "read_budget_state"]

#: The platform's own spend category (user decision 2026-10-07, contracts D11).
PLATFORM_CATEGORY = "platform_infra"
_UNSET = object()


class SsmBudgetStateReader:
    """Reads the budget-state flag from SSM (``ssm:GetParameter`` only)."""

    def __init__(self, ssm_client: Any, parameter: str = STATE_PARAMETER) -> None:
        self._ssm = ssm_client
        self.parameter = parameter

    def __call__(self) -> Any:
        try:
            resp = self._ssm.get_parameter(Name=self.parameter)
        except Exception as exc:  # noqa: BLE001 - any failure fails closed (botocore ClientError, transport errors)
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
            if code == "ParameterNotFound":
                return None
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "the budget state could not be read; retry", retryable=True) from None
        return (resp.get("Parameter") or {}).get("Value")


def read_budget_state(reader: Callable[[], Any] | None) -> Any:
    return None if reader is None else reader()


def budget_precheck(
    ctx: OperationContext,
    category: str,
    *,
    estimated_usd: float = 0.0,
    state: Any = _UNSET,
    reader: Callable[[], Any] | None = None,
    allocation: Mapping[str, float] | None = None,
    spend_records: Iterable[Any] = (),
    ceiling_usd: float | None = None,
) -> None:
    """Refuse paid or provider work while the budget is enforced (see module docstring)."""
    raw = read_budget_state(reader) if state is _UNSET else state
    result = preflight(
        category,
        float(estimated_usd),
        allocation,
        spend_records,
        ceiling_usd=ceiling_usd,
        budget_state=raw,
        correlation_id=ctx.correlation_id,
    )
    if result.allowed:
        return
    err = result.error or {}
    code = err.get("code", "BUDGET_EXCEEDED")
    details = {k: v for k, v in (err.get("details") or {}).items() if k in ("budget_category", "budget_state", "estimated_usd_upper_bound", "remaining_allocation_usd", "pointer", "field")}
    if code == "BUDGET_EXCEEDED":
        raise PlatformError("BUDGET_EXCEEDED", str(err.get("message") or "the project budget is exhausted"), retryable=False, **details)
    details.setdefault("pointer", "")
    raise PlatformError("VALIDATION_FAILED", str(err.get("message") or "budget pre-check failed"), **details)
