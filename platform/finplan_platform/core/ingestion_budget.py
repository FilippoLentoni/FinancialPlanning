"""Ingestion budget pre-check (platform-cost-guardrails "Budget pre-check"; task 9.4; COST-05).

Before any provider call, ingestion reads the account-level budget-state flag
``/finplan/shared/financialplanning/config/budget-state`` (written only by the budget-state
writer Lambda, contract D4) and refuses with ``BUDGET_EXCEEDED`` (``retryable`` false) while
the enforcement action is active. The decision reuses the contract package's
:func:`finplan_contracts.budget.preflight` (category ``platform_infra``, zero estimate), so the
platform and FinanceModel interpret the flag identically. Reading a flag is cheaper and faster
than calling the Budgets API per request (design P9).

A missing parameter means "not enforced". Any other read failure fails closed with
``DEPENDENCY_UNAVAILABLE`` (retryable): ingestion never runs without knowing the budget state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError
from finplan_contracts.budget import STATE_PARAMETER, preflight

from .context import OperationContext
from .errors import PlatformError

__all__ = ["STATE_PARAMETER", "BudgetGate", "SsmBudgetGate", "StaticBudgetGate", "check_budget_state"]

BudgetGate = Callable[[OperationContext], None]
BUDGET_CATEGORY = "platform_infra"


def check_budget_state(ctx: OperationContext, state: Any) -> None:
    """Raise ``BUDGET_EXCEEDED`` when ``state`` says the enforcement action is active."""
    result = preflight(BUDGET_CATEGORY, 0.0, budget_state=state, correlation_id=ctx.correlation_id)
    if not result.allowed and result.error and result.error.get("code") == "BUDGET_EXCEEDED":
        raise PlatformError(
            "BUDGET_EXCEEDED",
            "the project budget enforcement action is active; ingestion is refused",
            budget_category=BUDGET_CATEGORY,
            budget_state="enforced",
        )


class SsmBudgetGate:
    def __init__(self, ssm_client: Any, parameter: str = STATE_PARAMETER) -> None:
        self._ssm = ssm_client
        self.parameter = parameter

    def read_state(self) -> Any:
        try:
            resp = self._ssm.get_parameter(Name=self.parameter)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise PlatformError("DEPENDENCY_UNAVAILABLE", "the budget state could not be read; retry", retryable=True) from None
        return resp.get("Parameter", {}).get("Value")

    def __call__(self, ctx: OperationContext) -> None:
        check_budget_state(ctx, self.read_state())


class StaticBudgetGate:
    """A fixed budget state (tests and local harnesses)."""

    def __init__(self, state: Any = None) -> None:
        self.state = state
        self.checks = 0

    def __call__(self, ctx: OperationContext) -> None:
        self.checks += 1
        check_budget_state(ctx, self.state)
