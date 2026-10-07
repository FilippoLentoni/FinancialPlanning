"""Crafted-workbook helpers for the Excel suites (synthetic, generated at test time; no binary fixtures).

Every crafted package starts from a valid ``xlsx-plan-v1`` export and adds or replaces parts,
so each fixture differs from a clean workbook in exactly the property under test.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from typing import Any

from finplan_platform.excel import build_workbook
from finplan_platform.excel.template import TEMPLATE_VERSION

SAMPLE_CONTENT: dict[str, Any] = {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.6}], "cash_weight": 0.4},
    "constraints": {"long_only": True, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5},
}
SAMPLE_META: dict[str, Any] = {
    "template_version": TEMPLATE_VERSION,
    "contract_version": "0.1.0",
    "plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM",
    "base_plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM",
    "base_checksum": "sha256:" + "a" * 64,
    "expected_revision": 2,
    "exported_at": "2026-01-12T14:30:00Z",
    "synthetic": True,
}
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def rezip(parts: Mapping[str, bytes | str], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in parts.items():
            zf.writestr(name, data.encode() if isinstance(data, str) else data)
    return buf.getvalue()


def clean(content: Mapping[str, Any] | None = None, meta: Mapping[str, Any] | None = None) -> bytes:
    return build_workbook(content or SAMPLE_CONTENT, {**SAMPLE_META, **dict(meta or {})})


def craft(base: bytes | None = None, *, add: Mapping[str, bytes | str] | None = None, replace: Mapping[str, tuple[str, str]] | None = None, content_type_overrides: Mapping[str, str] | None = None) -> bytes:
    """Start from ``base`` (default: a clean export) and add parts / textual replacements."""
    parts: dict[str, bytes | str] = dict(unzip(base or clean()))
    for name, (old, new) in (replace or {}).items():
        text = parts[name].decode() if isinstance(parts[name], bytes) else parts[name]
        assert old in text, (name, old)
        parts[name] = text.replace(old, new, 1)
    if content_type_overrides:
        ct = parts["[Content_Types].xml"]
        ct = ct.decode() if isinstance(ct, bytes) else ct
        extra = "".join(f'<Override PartName="/{p}" ContentType="{t}"/>' for p, t in content_type_overrides.items())
        parts["[Content_Types].xml"] = ct.replace("</Types>", extra + "</Types>")
    for name, data in (add or {}).items():
        parts[name] = data
    return rezip(parts)


def sheet_xml(rows: list[list[str]]) -> str:
    """A worksheet from pre-rendered cell XML strings per row."""
    body = "".join(f'<row r="{i}">{"".join(cells)}</row>' for i, cells in enumerate(rows, start=1))
    return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>'
