"""Deterministic ``xlsx-plan-v1`` workbook writer (task 8.1; XLS-01).

Writes the minimal Office Open XML parts by hand: content types, package and workbook
relationships, a workbook with the ``meta`` and ``allocations`` sheets, and a minimal style
sheet. Strings are inline strings, numbers are written with Python's shortest round-trip
representation, so re-parsing returns exactly the exported values (and therefore the same
JCS checksum). There is no VBA project, no formula, no defined name, no external link and no
document-properties timestamp; ZIP entries carry a fixed date, so the same input always
produces the same bytes.
"""

from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape

from .template import ALLOCATION_HEADER, ALLOCATIONS_SHEET, CASH_INSTRUMENT_ID, META_HEADER, META_KEYS, META_SHEET

__all__ = ["build_workbook", "workbook_rows"]

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)

_CONTENT_TYPES = (
    _DECL
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    _DECL + f'<Relationships xmlns="{_PKG_REL}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK = (
    _DECL + f'<workbook xmlns="{_MAIN}" xmlns:r="{_R}"><sheets>'
    f'<sheet name="{META_SHEET}" sheetId="1" r:id="rId1"/>'
    f'<sheet name="{ALLOCATIONS_SHEET}" sheetId="2" r:id="rId2"/>'
    "</sheets></workbook>"
)
_WORKBOOK_RELS = (
    _DECL + f'<Relationships xmlns="{_PKG_REL}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)
_STYLES = (
    _DECL + f'<styleSheet xmlns="{_MAIN}">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def _col(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, int):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("non-finite numbers cannot be exported")
        return f'<c r="{ref}"><v>{repr(value)}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _sheet(rows: Sequence[Sequence[Any]]) -> str:
    out = [_DECL, f'<worksheet xmlns="{_MAIN}"><sheetData>']
    for r, values in enumerate(rows, start=1):
        cells = "".join(_cell(f"{_col(c)}{r}", v) for c, v in enumerate(values))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def workbook_rows(content: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[list[list[Any]], list[list[Any]]]:
    """(meta rows, allocation rows) including header rows."""
    meta = [list(META_HEADER)] + [[k, metadata[k]] for k in META_KEYS if metadata.get(k) is not None]
    alloc: list[list[Any]] = [list(ALLOCATION_HEADER)]
    allocation = content.get("allocation") or {}
    for w in allocation.get("weights") or []:
        alloc.append([w["instrument_id"], w["weight"]])
    if allocation.get("cash_weight") is not None:
        alloc.append([CASH_INSTRUMENT_ID, allocation["cash_weight"]])
    return meta, alloc


def build_workbook(content: Mapping[str, Any], metadata: Mapping[str, Any]) -> bytes:
    """The ``xlsx-plan-v1`` workbook bytes for plan ``content`` and template ``metadata``."""
    meta_rows, alloc_rows = workbook_rows(content, metadata)
    parts = [
        ("[Content_Types].xml", _CONTENT_TYPES),
        ("_rels/.rels", _ROOT_RELS),
        ("xl/workbook.xml", _WORKBOOK),
        ("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS),
        ("xl/styles.xml", _STYLES),
        ("xl/worksheets/sheet1.xml", _sheet(meta_rows)),
        ("xl/worksheets/sheet2.xml", _sheet(alloc_rows)),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in parts:
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zf.writestr(info, text.encode("utf-8"))
    return buf.getvalue()
