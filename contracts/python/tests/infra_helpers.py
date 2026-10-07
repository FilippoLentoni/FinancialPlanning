"""Shared helpers for the INFRA tests (synthetic data under tests/data/infra)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from finplan_contracts.iam import substitute

INFRA = Path(__file__).resolve().parent / "data" / "infra"
#: Placeholders used instead of real identifiers when evaluating generated policies offline.
PSEUDO = {"AWS::Partition": "aws", "AWS::Region": "us-east-2", "AWS::AccountId": "<account-id>"}


def infra(rel: str) -> Any:
    return json.loads((INFRA / rel).read_text(encoding="utf-8"))


def infra_files(rel_dir: str) -> list[Path]:
    return sorted((INFRA / rel_dir).glob("*.json"))


def concrete(policy: Any) -> Any:
    """Resolve CloudFormation pseudo parameters with placeholders for the offline evaluator."""
    return substitute(policy, PSEUDO)


def ssm_arn(path: str) -> str:
    return f"arn:aws:ssm:us-east-2:<account-id>:parameter{path}"
