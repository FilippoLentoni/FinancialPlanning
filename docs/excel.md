# Excel plan import and export

How a plan version becomes an `.xlsx` workbook and back (OpenSpec change `add-platform-foundation`, spec `excel-plan-import`, tasks 8.1 to 8.5, design P8). Code: `platform/finplan_platform/excel/`. Tests: `tests/unit/test_excel_package.py`, `tests/unit/test_excel_import.py`.

The logical template is the contract schema `finance/v1/excel-plan-template.json`, and the import outcome is the contract `core/v1/import-report.json`. Both come from the pinned `finplan-contracts` package, currently **1.0.0**; see [platform-core.md](platform-core.md). This page describes the physical workbook that carries the logical template.

## Routes

| Route | What it does | Idempotency key |
|---|---|---|
| `POST /v1/plan-versions/{plan_version_id}/exports` | Writes the template workbook for one version and returns a trusted reference (kind `plan_export`) and a time-limited download grant | yes |
| `POST /v1/plans/{plan_id}/imports` | Issues an upload grant: `import_id` plus a presigned POST with a size range and an expiry of at most 15 minutes | yes |
| `POST /v1/plans/{plan_id}/imports/{import_id}/commit` | Checks, parses and maps the uploaded workbook, then creates a child version with origin `excel_import` | yes |

In phase 1 these routes are open only to platform roles: the website path, operators and platform functions. FinanceLambdasTool role classes and FinanceModel roles get `FORBIDDEN`.

The download grant and the upload form are minted on every call. They are never stored in the idempotency record. A replayed grant request after the 15 minutes have passed fails with `PRECONDITION_FAILED` and reason `upload_grant_expired`.

## Template `xlsx-plan-v1`

The workbook has two sheets. Nothing else is allowed: no macros, formulas, defined names, external links or embedded objects. The export writes exactly these parts with fixed timestamps, so the same input always produces the same bytes.

### `meta` sheet

Row 1 is the header `key | value`. Each following row holds one key in column A and its value in column B.

| key | value | editable |
|---|---|---|
| `template_version` | `xlsx-plan-v1` | no |
| `contract_version` | Contract package version at export time (informational) | no |
| `plan_id` | The plan | no |
| `base_plan_version_id` | The exported version. It becomes the parent of the imported child | no |
| `base_checksum` | `sha256:` checksum of the base version's canonical content | no |
| `expected_revision` | The plan head revision at export time (optimistic concurrency) | no |
| `exported_at` | Export timestamp (UTC) | no |
| `synthetic` | `TRUE` for synthetic data | no |

### `allocations` sheet

Row 1 is the header `instrument_id | target_weight | note`. Each row from row 2 holds one instrument:

- `instrument_id` is text. The reserved ID `CASH` carries the cash weight (`allocation.cash_weight`), as in the contract's template fixture.
- `target_weight` is a number from 0 to 1.
- `note` is free text and is never mapped into plan content.

Blank rows are ignored. Cells outside columns A to C fail the import.

`base_currency`, `constraints` and `fees` cannot be edited in `xlsx-plan-v1`. They are copied unchanged from the base version.

### Sample workbook

This is the synthetic sample workbook (`synthetic: TRUE`) as the export generates it. The unit suite exports a version with exactly these allocations, checks that the rows below match, and imports it back (`test_documented_sample_workbook_imports_XLS_8_5`).

<!-- sample-allocations -->
| instrument_id | target_weight | note |
|---|---|---|
| `SPY` | 0.6 | |
| `CASH` | 0.4 | |
<!-- /sample-allocations -->

The `meta` sheet of the sample names the exported version, its checksum and the head revision at export time. All identifiers are platform-minted at test time.

## Safety rules (XLS-02, XLS-03)

All of these checks run **before** any cell is interpreted, in this order:

1. **Size.** An upload above `limits.excel_upload_max_bytes` is rejected with `file_too_large`. S3 already enforces the limit through the grant's `content-length-range`.
2. **Format.** OLE2 files (legacy `.xls`, encrypted workbooks) are rejected with `legacy_format_rejected`. Anything that is not a ZIP package is rejected with `unsupported_format`. When the commit names a `file_name`, it must end in `.xlsx`; `.xlsm`, `.xlsb` and `.xls` names are rejected.
3. **Package structure and decompression bombs.**
   - Limits on part count and part names: no absolute paths, no `..`, no duplicates.
   - No encrypted entries, and only the stored and deflate compression methods.
   - The total declared uncompressed size must stay under `excel_max_uncompressed_bytes`.
   - The uncompressed/compressed ratio must stay under `excel_max_compression_ratio`, both for each part and for the whole package.
   - Parts are read with a bounded reader that stops at the declared size, so a forged directory cannot inflate past the limits.
4. **Active content.**
   - Rejected with `macro_content_rejected`: `vbaProject.bin`, macro-enabled content types, VBA relationships, Excel 4.0 macro sheets, dialog sheets and `customUI`. The check looks at the package contents, so it applies even to a file with an `.xlsx` extension.
   - Rejected with `embedded_object_rejected`: ActiveX controls, OLE objects and embeddings.
   - Rejected with `external_link_rejected`: `externalLink` parts and any relationship with `TargetMode="External"`, including hyperlinks.
   - Rejected with `unsupported_format`: binary workbooks (`xl/workbook.bin`), templates and add-ins.
5. **XML.** Every XML part is parsed with `defusedxml`, with DTDs, entity declarations and external references forbidden. Violations are rejected with `xml_entities_rejected` (XXE, entity expansion) or `malformed_xml`.

The parser then reads **cell values only**:

- Shared strings, inline strings, numbers and booleans are decoded.
- A formula cell (`<f>`) is **never evaluated**. It fails the import with a finding that names the sheet, row, column and cell (`formula_not_allowed`).
  - The exception is a column the template marks formula-tolerant. There, the stored cached value is used and the row carries `formula_tolerant: true`.
  - `xlsx-plan-v1` marks no column formula-tolerant.
- Nothing is fetched and nothing is executed. The unit tests run the import with outbound sockets blocked and with `subprocess` patched.

Every rejection is a `VALIDATION_FAILED` envelope with `details.reason`. Content problems also carry `details.findings`: contract import-report findings with `sheet`, `row`, `column` and `field`.

## Mapping to the canonical contract (XLS-04)

1. The workbook's `plan_id` must equal the route's plan.
2. `base_plan_version_id` must be a version of that plan, and its checksum must equal `base_checksum`.
3. Every instrument except `CASH` must be covered by the base version's input snapshot. If one is not, the import fails with `VALIDATION_FAILED`, and the details name the `sheet`, `row` and `instrument_id`.
4. The plan head must still be at the workbook's `expected_revision`. If it is not, the import fails with `CONFLICT` and reason `stale_workbook`, and the details name `current_version_id` and `current_revision`. To recover, export the current head and re-apply the edits.
5. The child is created through the **same** create-version operation as the API:
   - origin `excel_import`, with the base as parent and `run_id` null;
   - checksum: SHA-256 of the RFC 8785 canonical content;
   - `no_effect` is true when that checksum equals the parent's.

   The head move, the idempotency record and the audit events commit in one transaction.

   The new version starts as `pending_validation`, like every created version. Validate it with `POST /v1/plan-versions/{id}/validate`.
6. The response is a contract `import-report` (outcome `accepted`, `created_plan_version_id`, `no_effect`). It is extended with `import_id` and a `plan_version` summary.

Re-exporting a version and importing it unchanged creates a no-effect child whose checksum equals the exported version's.

## Lineage and rejection records (XLS-05)

- Every upload that the commit reads is retained write-once as `raw/uploads/excel/<sha256>.xlsx`. This applies to accepted and rejected uploads alike. Retention follows the `raw` bucket rules (PQ-3).
- The created version's `source_artifact` is a trusted reference of kind `excel_source`. Its checksum equals the upload's SHA-256.
- Rejections are recorded:
  - always as an audit event `reject_excel_import` on the `import_id`, with the reason, findings and source checksum;
  - also as a contract `import-report` (outcome `rejected`), written write-once to the `reports` bucket, whenever the base version is known.
- The upload grant itself is recorded as an audit event `issue_import_grant` on the `import_id`. The commit uses it to check that the import belongs to the plan.

## Idempotency (XLS-06)

The commit's idempotency scope is caller, environment and `commit_excel_import`, plus the `idempotency_key`. The request hash covers the plan and the **SHA-256 of the uploaded file**, not the `import_id`. So:

- the same file under the same key returns the original result, even when it was uploaded again through a new grant;
- a different file under the same key fails with `IDEMPOTENCY_KEY_REUSED`;
- a rejected import stores no idempotency record, so a retry rejects again with the same reason.

## Open points

- The cash weight is the reserved `CASH` row, which the contract's `excel-plan-template` documents as reserved since 0.2.0.
- This module runs against the pinned 1.0.0 package (the same `v1` schemas as 0.2.2).
