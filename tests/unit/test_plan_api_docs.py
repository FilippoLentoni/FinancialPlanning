"""docs/plan-api.md stays true (task 4.7): examples validate, every route is documented, no leaks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from finplan_contracts.leak_scan import scan_text
from finplan_platform.core.contract_io import PLATFORM_SCHEMAS, validate_document
from finplan_platform.core.snapshot_reads import SNAPSHOT_RESPONSE
from finplan_platform.handlers.api import ROUTES

DOC = Path(__file__).resolve().parents[2] / "docs" / "plan-api.md"
BLOCK = re.compile(r"<!-- schema: (?P<schema>[a-z0-9/-]+) -->\s*```json\n(?P<body>.*?)```", re.DOTALL)


def _schema(name: str) -> object:
    if name in PLATFORM_SCHEMAS:
        return PLATFORM_SCHEMAS[name]
    if name == "snapshot-response":
        return SNAPSHOT_RESPONSE
    return name


def test_documented_examples_validate_with_the_contract_validators() -> None:
    text = DOC.read_text(encoding="utf-8")
    blocks = list(BLOCK.finditer(text))
    assert len(blocks) >= 15
    for m in blocks:
        doc = json.loads(m.group("body"))
        res = validate_document(doc, _schema(m.group("schema")))  # type: ignore[arg-type]
        assert res.valid, (m.group("schema"), [i.message for i in res.issues])
        assert doc.get("synthetic", True) is True or m.group("schema").endswith("request")


def test_every_route_is_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    for r in ROUTES:
        assert f"`{r.method} {r.path}`" in text, (r.method, r.path)


def test_document_passes_the_leak_scan() -> None:
    assert scan_text(DOC.read_text(encoding="utf-8"), "docs/plan-api.md") == []
