"""Excel plan import and export (STAGING-owned; tasks 8.1-8.5; spec excel-plan-import).

Public operations (delegated by name from the plan API router):

* :func:`export_plan_version` - ``POST /v1/plan-versions/{plan_version_id}/exports``
* :func:`issue_import_grant` - ``POST /v1/plans/{plan_id}/imports``
* :func:`commit_import` - ``POST /v1/plans/{plan_id}/imports/{import_id}/commit``

Pure helpers: :func:`build_workbook` (deterministic ``xlsx-plan-v1`` writer),
:func:`import_workbook_bytes` (package pre-checks + data-only parse),
:func:`content_from_workbook` (canonical plan content).
"""

from __future__ import annotations

from .package import ImportRejected, PackageLimits, open_package
from .reader import ParsedWorkbook, parse_workbook
from .service import commit_import, content_from_workbook, export_plan_version, import_workbook_bytes, issue_import_grant
from .template import TEMPLATE_VERSION, XLSX_CONTENT_TYPE, TemplateSpec
from .writer import build_workbook

__all__ = [
    "TEMPLATE_VERSION",
    "XLSX_CONTENT_TYPE",
    "ImportRejected",
    "PackageLimits",
    "ParsedWorkbook",
    "TemplateSpec",
    "build_workbook",
    "commit_import",
    "content_from_workbook",
    "export_plan_version",
    "import_workbook_bytes",
    "issue_import_grant",
    "open_package",
    "parse_workbook",
]
