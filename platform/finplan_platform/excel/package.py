"""Safe OOXML package pre-checks (task 8.2; XLS-02, XLS-03; design P8 "Parser rules").

:func:`open_package` turns uploaded bytes into a :class:`Package` (a bounded, in-memory map of
part name -> bytes) or raises :class:`ImportRejected` (``VALIDATION_FAILED`` with a
``reason``). Everything here runs **before any workbook content is interpreted**, in this
order:

1. size limit (``limits.excel_upload_max_bytes``) -> ``file_too_large``;
2. format sniffing: OLE2 compound files (legacy ``.xls``, encrypted workbooks) ->
   ``legacy_format_rejected``; anything that is not a ZIP -> ``unsupported_format``;
3. ZIP directory: entry count, unsafe or duplicate names, encrypted entries, and the
   **decompression bomb** guards (total declared uncompressed size against
   ``excel_max_uncompressed_bytes``; per-entry and whole-package ratio against
   ``excel_max_compression_ratio``) -> ``package_too_large`` / ``compression_ratio_exceeded``;
4. active content: a VBA project (``vbaProject.bin``, macro-enabled content types,
   Excel 4.0 macro sheets, ``customUI``) -> ``macro_content_rejected``; ActiveX controls, OLE
   objects or embeddings -> ``embedded_object_rejected``; external workbook links
   (``externalLink`` parts) or any relationship with ``TargetMode="External"`` ->
   ``external_link_rejected``; binary (``.xlsb``) or non-workbook packages ->
   ``unsupported_format``;
5. every XML part is parsed with ``defusedxml`` (DTDs, entity declarations and external
   references forbidden) -> ``xml_entities_rejected`` / ``malformed_xml``.

Entries are read with a bounded reader that stops at the declared size, so a forged
directory cannot inflate past the checked limits. Nothing is fetched, nothing executed: there
is no network or subprocess anywhere on this path (proved by the socket-blocked tests).
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as safe_fromstring

from ..core.errors import PlatformError

__all__ = [
    "ImportRejected",
    "Package",
    "PackageLimits",
    "open_package",
    "parse_xml",
    "NS",
]

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
REL_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
WORKBOOK_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
}
_OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_MAX_ENTRIES = 200
_MAX_PART_BYTES = 16 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-.\[\]/ ]{1,255}$")
#: (pattern on the lower-cased part name, reason)
_FORBIDDEN_PARTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)vbaproject\.bin$"), "macro_content_rejected"),
    (re.compile(r"(^|/)vbadata\.xml$"), "macro_content_rejected"),
    (re.compile(r"^xl/macrosheets/"), "macro_content_rejected"),
    (re.compile(r"^xl/dialogsheets/"), "macro_content_rejected"),
    (re.compile(r"^customui/"), "macro_content_rejected"),
    (re.compile(r"(^|/)activex/"), "embedded_object_rejected"),
    (re.compile(r"(^|/)embeddings/"), "embedded_object_rejected"),
    (re.compile(r"oleobject\d*\.bin$"), "embedded_object_rejected"),
    (re.compile(r"(^|/)externallinks/"), "external_link_rejected"),
    (re.compile(r"^xl/workbook\.bin$"), "unsupported_format"),
)
_FORBIDDEN_CONTENT_TYPES: tuple[tuple[str, str], ...] = (
    ("macroenabled", "macro_content_rejected"),
    ("vbaproject", "macro_content_rejected"),
    ("ms-office.vbaproject", "macro_content_rejected"),
    ("macrosheet", "macro_content_rejected"),
    ("activex", "embedded_object_rejected"),
    ("oleobject", "embedded_object_rejected"),
    ("externallink", "external_link_rejected"),
    ("sheet.binary", "unsupported_format"),
)
#: relationship types that mean active or external content wherever they appear
_FORBIDDEN_REL_TYPES: tuple[tuple[str, str], ...] = (
    ("/vbaproject", "macro_content_rejected"),
    ("/externallink", "external_link_rejected"),
    ("/oleobject", "embedded_object_rejected"),
    ("/control", "embedded_object_rejected"),
    ("/activexcontrol", "embedded_object_rejected"),
    ("/package", "embedded_object_rejected"),
)


class ImportRejected(PlatformError):
    """A workbook rejected before or during parsing (``VALIDATION_FAILED`` with a ``reason``)."""

    def __init__(self, reason: str, message: str, *, findings: list[dict[str, Any]] | None = None, **details: Any) -> None:
        self.reason = reason
        self.findings = list(findings or [])
        d: dict[str, Any] = {"reason": reason, **details}
        if self.findings:
            d["findings"] = self.findings[:50]
        super().__init__("VALIDATION_FAILED", message, details=d, pointer="")


@dataclass(frozen=True)
class PackageLimits:
    max_bytes: int
    max_uncompressed_bytes: int
    max_ratio: float

    @classmethod
    def from_config(cls, limits: Mapping[str, Any]) -> "PackageLimits":
        return cls(int(limits["excel_upload_max_bytes"]), int(limits["excel_max_uncompressed_bytes"]), float(limits["excel_max_compression_ratio"]))


@dataclass(frozen=True)
class Package:
    """A pre-checked OOXML package: part name -> bytes (no directory entries)."""

    parts: Mapping[str, bytes]
    content_types: Mapping[str, str]

    def part(self, name: str) -> bytes | None:
        return self.parts.get(name.lstrip("/"))

    def xml(self, name: str) -> Element | None:
        data = self.part(name)
        return None if data is None else parse_xml(data, name)


def parse_xml(data: bytes, name: str = "") -> Element:
    """Parse one XML part with DTDs, entities and external references forbidden."""
    try:
        return safe_fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise ImportRejected("xml_entities_rejected", "the workbook contains XML document type or entity declarations", part=_safe_part(name)) from None
    except (ParseError, ValueError, UnicodeDecodeError):
        raise ImportRejected("malformed_xml", "the workbook contains malformed XML", part=_safe_part(name)) from None


def _safe_part(name: str) -> str:
    return name if _SAFE_NAME.match(name or "") else "(unnamed part)"


def _read_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    limit = min(info.file_size, _MAX_PART_BYTES)
    if info.file_size > _MAX_PART_BYTES:
        raise ImportRejected("package_too_large", "a workbook part exceeds the size limit", part=_safe_part(info.filename))
    out = bytearray()
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > limit:
                raise ImportRejected("compression_ratio_exceeded", "a workbook part inflates beyond its declared size", part=_safe_part(info.filename))
    return bytes(out)


def _check_name(name: str) -> None:
    if not _SAFE_NAME.match(name) or name.startswith("/") or "\\" in name or ".." in name.split("/") or posixpath.normpath(name) != name.rstrip("/"):
        raise ImportRejected("unsupported_format", "the workbook package contains an unsafe part name")


def open_package(data: bytes, limits: PackageLimits) -> Package:
    """Run every pre-parse check and return the bounded part map (see the module docstring)."""
    if len(data) > limits.max_bytes:
        raise ImportRejected("file_too_large", "the workbook exceeds the upload size limit", size_bytes=len(data), max_bytes=limits.max_bytes)
    if data[:8] == _OLE2_MAGIC:
        raise ImportRejected("legacy_format_rejected", "binary or legacy workbooks (.xls, encrypted or OLE2 files) are not accepted; save as .xlsx")
    if data[:4] not in _ZIP_MAGIC:
        raise ImportRejected("unsupported_format", "only Office Open XML workbooks (.xlsx) are accepted")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
        raise ImportRejected("unsupported_format", "the workbook is not a readable .xlsx package") from None
    if len(infos) > _MAX_ENTRIES:
        raise ImportRejected("package_too_large", "the workbook package has too many parts", parts=len(infos))
    names: set[str] = set()
    total = 0
    for info in infos:
        _check_name(info.filename)
        key = info.filename.lower()
        if key in names:
            raise ImportRejected("unsupported_format", "the workbook package contains duplicate part names")
        names.add(key)
        if info.flag_bits & 0x1:
            raise ImportRejected("unsupported_format", "encrypted workbook parts are not accepted")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ImportRejected("unsupported_format", "the workbook uses an unsupported compression method")
        total += info.file_size
        if total > limits.max_uncompressed_bytes:
            raise ImportRejected("package_too_large", "the workbook's decompressed size exceeds the configured limit", max_uncompressed_bytes=limits.max_uncompressed_bytes)
        if info.file_size > 0 and info.file_size / max(info.compress_size, 1) > limits.max_ratio:
            raise ImportRejected("compression_ratio_exceeded", "a workbook part exceeds the configured decompression ratio", part=_safe_part(info.filename), max_ratio=limits.max_ratio)
    if data and total / len(data) > limits.max_ratio:
        raise ImportRejected("compression_ratio_exceeded", "the workbook exceeds the configured decompression ratio", max_ratio=limits.max_ratio)

    # active and external content by part name, before reading anything
    for info in infos:
        low = info.filename.lower()
        for pattern, reason in _FORBIDDEN_PARTS:
            if pattern.search(low):
                raise ImportRejected(reason, _MESSAGES[reason], part=_safe_part(info.filename))

    parts: dict[str, bytes] = {}
    for info in infos:
        if info.is_dir():
            continue
        parts[info.filename] = _read_bounded(zf, info)

    ct_raw = parts.get("[Content_Types].xml")
    if ct_raw is None:
        raise ImportRejected("unsupported_format", "the package has no content types part; it is not an .xlsx workbook")
    # every XML part is parsed safely once (DTD/entity scan) before any content is used
    trees: dict[str, Element] = {}
    for name, raw in parts.items():
        if name.lower().endswith((".xml", ".rels", ".vml")):
            trees[name] = parse_xml(raw, name)
    content_types: dict[str, str] = {}
    for el in trees["[Content_Types].xml"]:
        ctype = str(el.get("ContentType") or "")
        target = el.get("PartName") or ("*." + str(el.get("Extension") or ""))
        content_types[target.lstrip("/")] = ctype
        low = ctype.lower()
        for needle, reason in _FORBIDDEN_CONTENT_TYPES:
            if needle in low:
                raise ImportRejected(reason, _MESSAGES[reason])
    for name, tree in trees.items():
        if not name.endswith(".rels"):
            continue
        for rel in tree:
            rtype = str(rel.get("Type") or "").lower()
            if str(rel.get("TargetMode") or "").lower() == "external":
                raise ImportRejected("external_link_rejected", _MESSAGES["external_link_rejected"])
            for needle, reason in _FORBIDDEN_REL_TYPES:
                if rtype.endswith(needle):
                    raise ImportRejected(reason, _MESSAGES[reason])
    main_types = {t for p, t in content_types.items() if p.lower() == "xl/workbook.xml"}
    if not main_types or not main_types <= WORKBOOK_MAIN_TYPES:
        raise ImportRejected("unsupported_format", "the package is not a standard .xlsx workbook (templates, add-ins and binary workbooks are not accepted)")
    return Package(parts=parts, content_types=content_types)


_MESSAGES = {
    "macro_content_rejected": "the workbook contains macro content (VBA project or macro sheets); macros are never accepted",
    "embedded_object_rejected": "the workbook contains ActiveX controls or embedded OLE objects",
    "external_link_rejected": "the workbook contains external links; external content is never followed",
    "unsupported_format": "only standard .xlsx workbooks are accepted",
}
