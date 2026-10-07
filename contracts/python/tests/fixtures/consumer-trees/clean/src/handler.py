"""SYNTHETIC consumer handler: validates with the pinned contract package, never a copy."""

from finplan_contracts.validate import validate

PLAN_SCHEMA = "https://contracts.finplan.invalid/core/v1/plan.json"  # a reference, not a declaration


def handle(event: dict) -> dict:
    result = validate(event, PLAN_SCHEMA)
    return {"ok": result.valid}
