# Spec Delta

## Purpose

Defines safe Excel import of plan overrides into the canonical plan-version contract, with the uploaded file retained as lineage and no macro execution, and the template export that makes override round trips possible.

## ADDED Requirements

### Requirement: Template export for round trips
The plan API SHALL export any plan version as an `.xlsx` workbook in the versioned platform template. The template carries the base `plan_version_id`, its checksum, the plan head `expected_revision`, the template version and the editable allocation table. The export MUST contain no macros, external links or formulas.

#### Scenario: Export a version
- **WHEN** a client exports `pv_A`
- **THEN** it receives a time-limited download grant for an `.xlsx` workbook whose metadata sheet names `pv_A`, its checksum and the current head revision

### Requirement: Accepted formats only
Import SHALL accept only Office Open XML workbooks (`.xlsx`) within the configured size limit. Macro-enabled, binary or legacy formats (`.xlsm`, `.xlsb`, `.xls`) and any package containing a VBA project, ActiveX or OLE objects MUST be rejected before parsing content.

#### Scenario: Macro-enabled workbook
- **WHEN** a user uploads a workbook containing a VBA project, even with an `.xlsx` extension
- **THEN** the import fails with `VALIDATION_FAILED`, details `macro_content_rejected`, and no version is created

#### Scenario: Oversized or decompression-bomb file
- **WHEN** the upload exceeds the size limit or its decompressed size exceeds the configured ratio
- **THEN** the import fails with `VALIDATION_FAILED` before parsing

### Requirement: No code or formula execution
The importer SHALL treat the workbook as data only. It MUST NOT execute macros, evaluate formulas, follow external links or fetch remote content. A cell containing a formula MUST use its stored cached value only if the template marks the cell as formula-tolerant. Otherwise the import fails.

#### Scenario: Formula in allocation cell
- **WHEN** an allocation cell contains a formula
- **THEN** the import fails with `VALIDATION_FAILED` naming the cell, and no formula is evaluated

#### Scenario: External link
- **WHEN** the workbook contains an external workbook link
- **THEN** the import fails with `VALIDATION_FAILED`, and no network request is made

### Requirement: Canonical contract mapping
A valid workbook SHALL be parsed into the same canonical plan-version content used by the API. Plan content MUST be produced only through the same create-version operation, with origin `excel_import`, as a child of the base version named in the workbook, under the workbook's `expected_revision`.

#### Scenario: Excel override round trip
- **WHEN** a user exports `pv_A`, edits allocations and imports the workbook
- **THEN** child `pv_B` is created with parent `pv_A` and origin `excel_import`, `pv_A` is unchanged, and re-exporting `pv_B` then re-importing it unchanged creates a no-effect child whose checksum equals that of `pv_B`

#### Scenario: Stale workbook
- **WHEN** the plan head has moved since export, so the workbook's `expected_revision` is stale
- **THEN** the import fails with `CONFLICT`, and the response names the current head version

#### Scenario: Unknown instrument
- **WHEN** the workbook lists an instrument absent from the version's snapshot
- **THEN** the import fails with `VALIDATION_FAILED` naming the sheet, row and instrument

### Requirement: Source file lineage
The platform SHALL retain each imported workbook as an immutable artifact identified by its SHA-256. The resulting plan version MUST reference it by trusted artifact reference. Rejected uploads MUST be retained under the same retention rules with a rejection record.

#### Scenario: Lineage lookup
- **WHEN** a client reads `pv_B` created by import
- **THEN** its lineage includes a trusted artifact reference to the source workbook with a checksum equal to the uploaded file's SHA-256

### Requirement: Idempotent import
Each import SHALL require an `idempotency_key`. Re-importing an identical file with the same key MUST return the original result.

#### Scenario: Double submit
- **WHEN** the same upload is submitted twice with the same key
- **THEN** one child version exists and both responses return its `plan_version_id`
