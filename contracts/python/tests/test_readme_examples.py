"""Task 3.4: the JSON examples in contracts/README.md validate with the package validators."""

from __future__ import annotations

import json
import re

import pytest

from finplan_contracts.validate import validate

from conftest import CONTRACTS

README = CONTRACTS / "README.md"
#: identifying field -> schema of a README example
KINDS = {"plan_version_id": "plan-version", "publication_id": "publication", "execution_id": "execution"}


def _examples():
    blocks = re.findall(r"```json\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.S)
    out = []
    for block in blocks:
        doc = json.loads(block)
        # an execution also names its publication; pick the most specific identifying field
        schema = next(KINDS[k] for k in ("execution_id", "publication_id", "plan_version_id") if k in doc)
        out.append((schema, doc))
    return out


EXAMPLES = _examples()


def test_readme_has_lifecycle_examples():
    assert {s for s, _ in EXAMPLES} == set(KINDS.values())


@pytest.mark.parametrize("schema, doc", EXAMPLES, ids=[s for s, _ in EXAMPLES])
def test_readme_example_validates(schema, doc):
    assert doc.get("synthetic") is True
    result = validate(doc, schema)
    assert result.valid, result.issues
