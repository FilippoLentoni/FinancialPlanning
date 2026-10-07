"""JSON Schema ``pattern`` uses ECMA-262 anchoring in the Python validator (CS-10, ID-01).

Python's ``$`` also matches before a trailing newline; JSON Schema (and the
TypeScript/Ajv validator) do not. Identifiers and keys with a trailing newline
must be rejected in both languages.
"""

from __future__ import annotations

import json

import pytest

from finplan_contracts import keys, ssm
from finplan_contracts.schemas import ecma_pattern
from finplan_contracts.validate import validate

from conftest import FIXTURES


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("^a$", r"^a\Z"),
        ("^[$]x$", r"^[$]x\Z"),
        (r"^a\$b$", r"^a\$b\Z"),
        ("^(a|b)$", r"^(a|b)\Z"),
        ("no-anchor", "no-anchor"),
    ],
)
def test_ecma_pattern_rewrites_only_unescaped_end_anchors(pattern, expected):
    assert ecma_pattern(pattern) == expected


def test_identifier_with_trailing_newline_is_invalid():
    doc = json.loads((FIXTURES / "job-status/valid/running.json").read_text())
    assert validate(doc, "job-status").valid
    doc["run_id"] += "\n"
    result = validate(doc, "job-status")
    assert not result.valid
    assert result.code == "INVALID_IDENTIFIER"


def test_key_helpers_reject_trailing_newline():
    assert keys.IDEMPOTENCY_KEY_PATTERN.match("key-1")
    assert not keys.IDEMPOTENCY_KEY_PATTERN.match("key-1\n")
    assert not keys.TOOL_NAME_PATTERN.match("get_plan\n")
    assert not keys.DERIVED_KEY_PATTERN.match("lt_" + "0" * 64 + "\n")


def test_ssm_names_reject_trailing_newline():
    assert not ssm.NAME_RE.match("plan-endpoint\n")
    assert ssm.NAME_RE.match("plan-endpoint")


def test_anchoring_holds_inside_referenced_schemas():
    """``$ref`` into another contract schema (which declares ``$schema``) keeps ECMA anchoring."""
    doc = json.loads((FIXTURES / "explanation-evidence/valid/allocation-delta.json").read_text())
    assert validate(doc, "explanation-evidence").valid
    doc["source_refs"][0]["checksum"] += "\n"
    assert not validate(doc, "explanation-evidence").valid
