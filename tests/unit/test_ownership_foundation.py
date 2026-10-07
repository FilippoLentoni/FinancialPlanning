"""Ownership check (contracts OWN-01) on the FOUNDATION templates.

Every resource must map to a FinancialPlanning matrix row. Since contracts 0.2.0 the matrix covers
the platform's own SSM references, the metadata sweeper and the CDK helpers of attributed
constructs, so no problem is accepted.
"""

from __future__ import annotations

from typing import Any

from finplan_contracts.ownership import check_template


def test_foundation_templates_pass_ownership(foundation_synth: dict[str, Any]) -> None:
    problems = []
    for name, template in foundation_synth["templates"].items():
        report = check_template(template, "financialplanning", name=name).to_dict()
        problems += [(name, p["logical_id"], p["resource_type"], p["message"]) for p in report["problems"]]
    assert problems == [], problems
