from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from finplan_contracts.schemas import SchemaStore, contracts_root, load_store

CONTRACTS = contracts_root()
FIXTURES = CONTRACTS / "fixtures"


def fixture(rel: str) -> Any:
    """Load a fixture by its path relative to contracts/fixtures."""
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def store() -> SchemaStore:
    return load_store()


@pytest.fixture(scope="session")
def root() -> Path:
    return CONTRACTS
