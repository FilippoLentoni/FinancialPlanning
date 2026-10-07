"""Synthetic contract records for tests (every builder output validates against the pinned package).

Values are synthetic (``synthetic: true``); identifiers are minted by the test context.
"""

from __future__ import annotations

import copy
from typing import Any

from finplan_contracts.canonical import canonicalize, sha256_hex

from finplan_platform.core.context import OperationContext

__all__ = ["DEFAULT_CONTENT", "content_checksum", "portfolio_doc", "plan_doc", "version_doc", "snapshot_doc", "CONFIGURATION_ID", "SNAPSHOT_ID_PLACEHOLDER"]

DEFAULT_CONTENT: dict[str, Any] = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.6}], "cash_weight": 0.4},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}
CONFIGURATION_ID = "cfg_" + "0" * 64
SNAPSHOT_ID_PLACEHOLDER = "snap_01KDVDNAZ83BAMMYCEGWF33DPM"
#: FinanceModel-minted identifiers used as synthetic foreign references (never minted by the platform).
FOREIGN_RUN_ID = "run_01KDVDNAZ83BAMMYCEGWF33DPM"
FOREIGN_MODEL_VERSION = "mv_01KDVDNAZ83BAMMYCEGWF33DPM"


def content_checksum(content: Any) -> str:
    """``sha256:`` + SHA-256 of the RFC 8785 canonical content (design P4)."""
    return "sha256:" + sha256_hex(canonicalize(content))


def portfolio_doc(ctx: OperationContext, *, revision: int = 0) -> dict[str, Any]:
    return {
        "portfolio_id": ctx.new_id("portfolio_id"),
        "name": "Synthetic test portfolio",
        "base_currency": "USD",
        "synthetic": True,
        "revision": revision,
        "created_at": ctx.now_ts(),
    }


def plan_doc(ctx: OperationContext, portfolio_id: str, *, revision: int = 0, current_version_id: str | None = None) -> dict[str, Any]:
    return {
        "plan_id": ctx.new_id("plan_id"),
        "portfolio_id": portfolio_id,
        "name": "Synthetic test plan",
        "head": {"current_version_id": current_version_id, "revision": revision},
        "current_publication_id": None,
        "created_at": ctx.now_ts(),
        "synthetic": True,
    }


def version_doc(
    ctx: OperationContext,
    plan_id: str,
    *,
    parent_plan_version_id: str | None = None,
    content: dict[str, Any] | None = None,
    status: str = "pending_validation",
    origin: str | None = None,
    input_snapshot_id: str = SNAPSHOT_ID_PLACEHOLDER,
) -> dict[str, Any]:
    """A root defaults to origin ``model_run`` (with synthetic foreign run/model IDs); a child to ``manual_override``."""
    body = copy.deepcopy(content if content is not None else DEFAULT_CONTENT)
    origin = origin or ("manual_override" if parent_plan_version_id else "model_run")
    foreign = origin == "model_run"
    return {
        "plan_version_id": ctx.new_id("plan_version_id"),
        "plan_id": plan_id,
        "parent_plan_version_id": parent_plan_version_id,
        "input_snapshot_id": input_snapshot_id,
        "configuration_id": CONFIGURATION_ID,
        "model_version": FOREIGN_MODEL_VERSION if foreign else None,
        "run_id": FOREIGN_RUN_ID if foreign else None,
        "origin": origin,
        "status": status,
        "checksum": content_checksum(body),
        "domain": "finance",
        "domain_schema_version": "1.0",
        "content": body,
        "created_at": ctx.now_ts(),
        "synthetic": True,
    }


def snapshot_doc(ctx: OperationContext, *, status: str = "committed") -> dict[str, Any]:
    """A catalog record based on the package's approved-snapshot fixture shape."""
    from finplan_contracts.schemas import load_store
    import json

    store = load_store()
    base = json.loads((store.fixtures_dir("input-snapshot") / "valid" / "approved-etf-daily.json").read_text())
    base["input_snapshot_id"] = ctx.new_id("input_snapshot_id")
    base["status"] = status
    base["created_at"] = ctx.now_ts()
    if status != "approved":
        base.pop("approval_rule_version", None)
    return base
