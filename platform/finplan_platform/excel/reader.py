"""Data-only workbook reader (task 8.3; XLS-03, XLS-04; design P8).

Reads cell *values* from a pre-checked :class:`~finplan_platform.excel.package.Package` and maps
the two template sheets onto the contract's logical template (``excel-plan-template``):

* sheets are resolved through ``xl/workbook.xml`` and its relationships (never by guessing
  file names); shared strings, inline strings, numbers and booleans are decoded;
* a cell holding a formula (an ``<f>`` element) is **never evaluated**. It is rejected,
  naming the sheet and cell, unless the template marks its column formula-tolerant, in which
  case its stored cached value is used (and the row is flagged ``formula_tolerant``);
* every problem becomes a finding naming the sheet, row and column; findings are collected
  and reported together (``VALIDATION_FAILED``, reason ``workbook_content_rejected``).

The result (:class:`ParsedWorkbook`) carries the contract logical document, validated with the
pinned ``excel-plan-template`` schema, plus the cash weight from the reserved ``CASH`` row.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element

from finplan_contracts.validate import validate as contract_validate

from .package import NS, REL_OFFICE_DOCUMENT, ImportRejected, Package
from .template import (
    ALLOCATION_HEADER,
    ALLOCATIONS_SHEET,
    CASH_INSTRUMENT_ID,
    META_HEADER,
    META_KEYS,
    META_REQUIRED,
    META_SHEET,
    TEMPLATES,
    TemplateSpec,
)

__all__ = ["Cell", "ParsedWorkbook", "parse_workbook", "read_sheets"]

_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_MAX_CELLS = 20000
_REL_WORKSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
_REL_SHARED_STRINGS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"


@dataclass(frozen=True)
class Cell:
    ref: str
    column: str
    row: int
    value: Any
    formula: bool = False


@dataclass
class ParsedWorkbook:
    logical: dict[str, Any]
    cash_weight: float | int | None
    contract_version: str | None = None
    notes: dict[int, str] = field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, Any]:
        return self.logical["metadata"]

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.logical["allocation_rows"]


def _q(tag: str, ns: str = "main") -> str:
    return f"{{{NS[ns]}}}{tag}"


def _col_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_name(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _resolve(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = base_part.rsplit("/", 1)[0] if "/" in base_part else ""
    parts: list[str] = [p for p in base_dir.split("/") if p]
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def _rels(pkg: Package, part: str) -> dict[str, tuple[str, str]]:
    """Relationship id -> (type, resolved target part) for ``part``."""
    if "/" in part:
        d, f = part.rsplit("/", 1)
        rels_name = f"{d}/_rels/{f}.rels"
    else:
        rels_name = f"_rels/{part}.rels"
    tree = pkg.xml(rels_name)
    out: dict[str, tuple[str, str]] = {}
    if tree is None:
        return out
    for rel in tree.findall(_q("Relationship", "rel")):
        out[str(rel.get("Id"))] = (str(rel.get("Type") or ""), _resolve(part, str(rel.get("Target") or "")))
    return out


def _text(el: Element | None) -> str:
    """Concatenated ``<t>`` text of a string item (rich-text runs included, phonetic runs not)."""
    if el is None:
        return ""
    out = []
    for child in el:
        if child.tag == _q("t"):
            out.append(child.text or "")
        elif child.tag == _q("r"):
            t = child.find(_q("t"))
            out.append((t.text or "") if t is not None else "")
    return "".join(out)


def _shared_strings(pkg: Package, wb_part: str, wb_rels: Mapping[str, tuple[str, str]]) -> list[str]:
    target = next((t for (typ, t) in wb_rels.values() if typ == _REL_SHARED_STRINGS), None)
    if target is None:
        return []
    tree = pkg.xml(target)
    if tree is None:
        raise ImportRejected("unsupported_format", "the workbook references a missing shared-strings part")
    return [_text(si) for si in tree.findall(_q("si"))]


def _number(raw: str) -> int | float:
    try:
        if re.fullmatch(r"-?[0-9]+", raw):
            return int(raw)
        value = float(raw)
    except ValueError:
        raise ValueError("not a number") from None
    if math.isnan(value) or math.isinf(value):
        raise ValueError("not a finite number")
    return value


def _cell_value(c: Element, shared: list[str]) -> Any:
    t = c.get("t") or "n"
    v = c.find(_q("v"))
    raw = v.text if v is not None and v.text is not None else None
    if t == "inlineStr":
        return _text(c.find(_q("is")))
    if raw is None:
        return None
    if t == "s":
        idx = int(raw)
        if not 0 <= idx < len(shared):
            raise ValueError("shared string index out of range")
        return shared[idx]
    if t in ("str",):
        return raw
    if t == "b":
        return raw.strip() in ("1", "true")
    if t == "e":
        raise ValueError("error value")
    if t in ("n", "d"):
        return _number(raw.strip()) if t == "n" else raw
    raise ValueError("unknown cell type")


def read_sheets(pkg: Package) -> dict[str, dict[int, dict[str, Cell]]]:
    """Sheet name -> row -> column -> :class:`Cell` (values only; formulas flagged, not evaluated)."""
    root_rels = _rels(pkg, "")  # _rels/.rels
    if not root_rels:
        root_tree = pkg.xml("_rels/.rels")
        if root_tree is None:
            raise ImportRejected("unsupported_format", "the package has no root relationships")
    wb_part = next((t for (typ, t) in root_rels.values() if typ == REL_OFFICE_DOCUMENT), None)
    if wb_part is None or pkg.part(wb_part) is None:
        raise ImportRejected("unsupported_format", "the package has no workbook part")
    wb = pkg.xml(wb_part)
    assert wb is not None
    wb_rels = _rels(pkg, wb_part)
    shared = _shared_strings(pkg, wb_part, wb_rels)
    sheets_el = wb.find(_q("sheets"))
    out: dict[str, dict[int, dict[str, Cell]]] = {}
    cells_seen = 0
    for sheet in list(sheets_el) if sheets_el is not None else []:
        name = str(sheet.get("name") or "")
        rid = sheet.get(_q("id", "r"))
        typ, target = wb_rels.get(str(rid), ("", ""))
        if typ != _REL_WORKSHEET:
            raise ImportRejected("unsupported_format", "the workbook contains a sheet that is not a worksheet (chart or macro sheets are not accepted)", sheet=name[:64] or "(unnamed)")
        tree = pkg.xml(target)
        if tree is None:
            raise ImportRejected("unsupported_format", "a worksheet part is missing", sheet=name[:64] or "(unnamed)")
        rows: dict[int, dict[str, Cell]] = {}
        data = tree.find(_q("sheetData"))
        next_row = 1
        for row_el in list(data) if data is not None else []:
            r_attr = row_el.get("r")
            row_no = int(r_attr) if r_attr and r_attr.isdigit() else next_row
            next_row = row_no + 1
            next_col = 1
            for c in row_el.findall(_q("c")):
                cells_seen += 1
                if cells_seen > _MAX_CELLS:
                    raise ImportRejected("package_too_large", "the workbook has too many cells")
                ref = c.get("r")
                if ref:
                    m = _CELL_REF.match(ref)
                    if not m or int(m.group(2)) != row_no:
                        raise ImportRejected("malformed_xml", "the workbook has an invalid cell reference", sheet=name[:64])
                    col = m.group(1)
                else:
                    col = _col_name(next_col)
                    ref = f"{col}{row_no}"
                next_col = _col_index(col) + 1
                formula = c.find(_q("f")) is not None
                try:
                    value = _cell_value(c, shared)
                except ValueError:
                    value = _Invalid()
                rows.setdefault(row_no, {})[col] = Cell(ref, col, row_no, value, formula)
        out[name] = rows
    return out


class _Invalid:
    """Marker for a cell whose stored value cannot be decoded (error values, bad numbers)."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<invalid>"


def _finding(message: str, *, sheet: str, row: int | None = None, column: str | None = None, field: str | None = None, **details: Any) -> dict[str, Any]:
    f: dict[str, Any] = {"severity": "error", "code": "VALIDATION_FAILED", "message": message, "sheet": sheet}
    if row is not None:
        f["row"] = row
    if column is not None:
        f["column"] = column
    if field is not None:
        f["field"] = field
    if details:
        f["details"] = details
    return f


def _value(cell: Cell | None, spec_tolerant: bool, findings: list[dict[str, Any]], sheet: str, field: str) -> Any:
    """The cell's value, applying the formula rule (never evaluated)."""
    if cell is None:
        return None
    if cell.formula and not spec_tolerant:
        findings.append(_finding("the cell contains a formula; formulas are not evaluated and are not accepted in this cell", sheet=sheet, row=cell.row, column=cell.column, field=field, cell=cell.ref, reason="formula_not_allowed"))
        return _Invalid()
    if isinstance(cell.value, _Invalid):
        findings.append(_finding("the cell value cannot be read as data", sheet=sheet, row=cell.row, column=cell.column, field=field, cell=cell.ref))
    return cell.value


def _is_blank(row: Mapping[str, Cell]) -> bool:
    return all((c.value is None or c.value == "") and not c.formula for c in row.values())


def parse_workbook(pkg: Package, *, spec: TemplateSpec | None = None) -> ParsedWorkbook:
    """Map a pre-checked package to the template's logical content or raise :class:`ImportRejected`."""
    sheets = read_sheets(pkg)
    findings: list[dict[str, Any]] = []
    for required in (META_SHEET, ALLOCATIONS_SHEET):
        if required not in sheets:
            findings.append(_finding(f"the workbook has no '{required}' sheet", sheet=required))
    if findings:
        raise ImportRejected("workbook_content_rejected", "the workbook does not follow the plan template", findings=findings)

    # ---------------------------------------------------------------- meta
    meta_rows = sheets[META_SHEET]
    header = tuple((meta_rows.get(1, {}).get(c).value if meta_rows.get(1, {}).get(c) else None) for c in ("A", "B"))
    if header != META_HEADER:
        findings.append(_finding("the meta sheet header must be 'key | value'", sheet=META_SHEET, row=1))
    meta: dict[str, Any] = {}
    meta_cells: dict[str, Cell] = {}
    for row_no in sorted(r for r in meta_rows if r > 1):
        row = meta_rows[row_no]
        for cell in row.values():
            if cell.formula:
                findings.append(_finding("the meta sheet must not contain formulas", sheet=META_SHEET, row=row_no, column=cell.column, cell=cell.ref, reason="formula_not_allowed"))
        if _is_blank(row):
            continue
        key_cell = row.get("A")
        key = key_cell.value if key_cell else None
        if not isinstance(key, str) or key not in META_KEYS:
            findings.append(_finding("unknown key in the meta sheet", sheet=META_SHEET, row=row_no, column="A", field="key"))
            continue
        if key in meta:
            findings.append(_finding("duplicate key in the meta sheet", sheet=META_SHEET, row=row_no, column="A", field=key))
            continue
        val = row.get("B")
        meta[key] = val.value if val else None
        if val:
            meta_cells[key] = val
    template_version = meta.get("template_version")
    spec = spec or TEMPLATES.get(str(template_version))
    if spec is None:
        findings.append(_finding("unknown template version", sheet=META_SHEET, field="template_version", reason="unsupported_template"))
        raise ImportRejected("workbook_content_rejected", "the workbook uses an unknown template version", findings=findings)
    for key in META_REQUIRED:
        if meta.get(key) in (None, ""):
            findings.append(_finding(f"the meta sheet is missing '{key}'", sheet=META_SHEET, field=key))
    rev = meta.get("expected_revision")
    if isinstance(rev, float) and rev.is_integer():
        rev = int(rev)
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
        c = meta_cells.get("expected_revision")
        findings.append(_finding("expected_revision must be a whole number", sheet=META_SHEET, row=c.row if c else None, column="B", field="expected_revision"))
    synthetic = meta.get("synthetic")
    if synthetic is not None and not isinstance(synthetic, bool):
        findings.append(_finding("synthetic must be TRUE or FALSE", sheet=META_SHEET, field="synthetic"))

    # ---------------------------------------------------------------- allocations
    alloc = sheets[ALLOCATIONS_SHEET]
    head_row = alloc.get(1, {})
    header = tuple((head_row.get(c).value if head_row.get(c) else None) for c in ("A", "B", "C"))
    if header[:2] != ALLOCATION_HEADER[:2] or header[2] not in (None, ALLOCATION_HEADER[2]):
        findings.append(_finding("the allocations header must be 'instrument_id | target_weight | note'", sheet=ALLOCATIONS_SHEET, row=1))
    rows: list[dict[str, Any]] = []
    notes: dict[int, str] = {}
    cash: float | int | None = None
    seen: dict[str, int] = {}
    data_rows = sorted(r for r in alloc if r > 1)
    if len(data_rows) > spec.max_allocation_rows:
        raise ImportRejected("workbook_content_rejected", "the allocations sheet has too many rows", findings=[_finding("too many allocation rows", sheet=ALLOCATIONS_SHEET)])
    for row_no in data_rows:
        row = alloc[row_no]
        for col, cell in row.items():
            if col not in ("A", "B", "C") and not (cell.value in (None, "") and not cell.formula):
                findings.append(_finding("unexpected cell outside the template columns", sheet=ALLOCATIONS_SHEET, row=row_no, column=col, cell=cell.ref))
        if _is_blank(row):
            continue
        inst = _value(row.get("A"), "instrument_id" in spec.formula_tolerant_columns, findings, ALLOCATIONS_SHEET, "instrument_id")
        tolerant_weight = "target_weight" in spec.formula_tolerant_columns
        weight_cell = row.get("B")
        weight = _value(weight_cell, tolerant_weight, findings, ALLOCATIONS_SHEET, "target_weight")
        note = _value(row.get("C"), "note" in spec.formula_tolerant_columns, findings, ALLOCATIONS_SHEET, "note")
        if isinstance(inst, _Invalid) or isinstance(weight, _Invalid) or isinstance(note, _Invalid):
            continue
        if not isinstance(inst, str) or not inst:
            findings.append(_finding("instrument_id must be text", sheet=ALLOCATIONS_SHEET, row=row_no, column="A", field="instrument_id"))
            continue
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            findings.append(_finding("target_weight must be a number", sheet=ALLOCATIONS_SHEET, row=row_no, column="B", field="target_weight", instrument_id=inst[:64]))
            continue
        if inst in seen:
            findings.append(_finding("instrument appears more than once", sheet=ALLOCATIONS_SHEET, row=row_no, column="A", field="instrument_id", instrument_id=inst[:64], first_row=seen[inst]))
            continue
        seen[inst] = row_no
        if note not in (None, ""):
            notes[row_no] = str(note)[:2000]
        if inst == CASH_INSTRUMENT_ID:
            cash = weight
        entry: dict[str, Any] = {"row": row_no, "instrument_id": inst, "target_weight": weight}
        if weight_cell is not None and weight_cell.formula:
            entry["formula_tolerant"] = True
        rows.append(entry)
    if findings:
        raise ImportRejected("workbook_content_rejected", "the workbook content does not follow the plan template", findings=findings)

    logical: dict[str, Any] = {
        "template_version": spec.version,
        "metadata": {k: (rev if k == "expected_revision" else meta[k]) for k in ("plan_id", "base_plan_version_id", "base_checksum", "expected_revision", "exported_at")},
        "allocation_rows": rows,
    }
    if synthetic is not None:
        logical["synthetic"] = synthetic
    res = contract_validate(logical, "excel-plan-template")
    if not res.valid:
        for issue in res.issues[:50]:
            findings.append(_logical_finding(issue.pointer, issue.message, logical))
        raise ImportRejected("workbook_content_rejected", "the workbook content does not match the plan template contract", findings=findings)
    cv = meta.get("contract_version")
    return ParsedWorkbook(logical=logical, cash_weight=cash, contract_version=cv if isinstance(cv, str) else None, notes=notes)


def _logical_finding(pointer: str, message: str, logical: Mapping[str, Any]) -> dict[str, Any]:
    """Map a contract pointer in the logical document back to a sheet/row/column."""
    parts = [p for p in pointer.split("/") if p]
    if parts[:1] == ["allocation_rows"] and len(parts) >= 2 and parts[1].isdigit():
        i = int(parts[1])
        rows = logical.get("allocation_rows") or []
        row = rows[i]["row"] if i < len(rows) else None
        field = parts[2] if len(parts) > 2 else None
        column = {"instrument_id": "A", "target_weight": "B"}.get(field or "")
        return _finding("allocation row does not match the template contract", sheet=ALLOCATIONS_SHEET, row=row, column=column, field=field, pointer=pointer)
    if parts[:1] == ["metadata"]:
        field = parts[1] if len(parts) > 1 else None
        return _finding("meta value does not match the template contract", sheet=META_SHEET, field=field, pointer=pointer)
    return _finding("workbook does not match the template contract", sheet=META_SHEET, pointer=pointer)
