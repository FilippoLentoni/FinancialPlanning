# Spec Delta

## Purpose

Defines the single plan lifecycle API: portfolios, plans, root and child versions, validation, publication of an exact validated version, separately recorded executions and checksum-bearing read-back. The website, Excel import, scheduled workflows and MCP adapters all use it.

## ADDED Requirements

### Requirement: Single plan API for all clients
The platform SHALL expose one IAM-authenticated plan lifecycle API per environment, published at `/finplan/<env>/financialplanning/api/plan-endpoint`. The website, Excel import, scheduled workflows and FinanceLambdasTool MCP adapters MUST use these operations. No client MUST write plan state through any other path.

#### Scenario: Website and agent read the same version
- **WHEN** a website-path client and an MCP-adapter client each read `pv_B` from the same environment
- **THEN** both receive the same `plan_version_id`, the same checksum and byte-identical canonical content

#### Scenario: Unauthenticated call
- **WHEN** a request arrives without valid IAM credentials of an allowed principal in that environment
- **THEN** it is rejected with `UNAUTHORIZED` or `FORBIDDEN`, and no state changes

### Requirement: Contract conformance of every operation
Every request and response SHALL validate against the pinned contract package (1.x once 1.0.0 is published; until then the 0.x pre-release, currently 0.2.0, which is beta-only). Every route MUST validate its response before sending it. Errors MUST use the contract error envelope with a `correlation_id`. Requests declaring a contract major the platform does not serve MUST fail with `UNSUPPORTED_CONTRACT_VERSION`.

#### Scenario: Schema violation
- **WHEN** a root create-version request (no parent) omits `input_snapshot_id`
- **THEN** it fails with `VALIDATION_FAILED`, `retryable` false and a `details` JSON pointer to the missing field

#### Scenario: Non-conformant response is never sent
- **WHEN** an operation produces a response that does not validate against the route's response schema
- **THEN** the caller receives `INTERNAL` and the non-conformant response is not sent

#### Scenario: Unsupported major
- **WHEN** a request declares contract major 2 and the platform serves only major 1
- **THEN** it fails with `UNSUPPORTED_CONTRACT_VERSION` listing the served majors

### Requirement: Create portfolio and plan
The API SHALL create portfolios and plans, minting `portfolio_id` and `plan_id`. Phase 1 portfolios MUST be flagged `synthetic: true`. A request that marks a portfolio non-synthetic MUST be rejected in phase 1 (phase scope; the single-account decision does not lift this rule).

#### Scenario: Create synthetic portfolio
- **WHEN** a client creates a portfolio with `synthetic: true` and an `idempotency_key`
- **THEN** a `portfolio_id` is minted and returned with `revision` 1

#### Scenario: Non-synthetic portfolio in phase 1
- **WHEN** a client creates a portfolio with `synthetic: false`
- **THEN** it fails with `OPERATION_NOT_PERMITTED`

### Requirement: Root and child plan versions
The API SHALL create a root version (no parent) or a child version of a named parent, record the full contract lineage, and set status `pending_validation`. An override, a manual edit or an Excel import MUST always create a child version. Client-supplied `plan_version_id` values MUST be rejected.

#### Scenario: Manual override
- **WHEN** a client submits changed allocations with `parent_plan_version_id` `pv_A` and the current `expected_revision`
- **THEN** a child `pv_B` with origin `manual_override` and parent `pv_A` is committed, the head moves to `pv_B`, and `pv_A` is unchanged

#### Scenario: Child inherits its lineage
- **WHEN** a child version is created on the version route from parent `pv_A`
- **THEN** its `input_snapshot_id`, `configuration_id` and `model_version` are those of `pv_A`; only a root request names `input_snapshot_id`, and a child request that names it fails with `VALIDATION_FAILED`

#### Scenario: Parent from another plan
- **WHEN** the named parent version belongs to a different `plan_id`
- **THEN** the request fails with `VALIDATION_FAILED`

#### Scenario: No-effect override
- **WHEN** the submitted content canonically equals the parent's content
- **THEN** a child version is created whose checksum equals the parent's, and the response flags `no_effect: true`

### Requirement: Deterministic validation
The API SHALL validate a version deterministically: schema, accounting reconciliation (weights sum to 1 within the configured tolerance, no negative cash), configured constraints, and instrument coverage against the referenced snapshot. It then transitions the version to `validated` or `invalid` with itemized findings. Repeating the validation MUST yield the same outcome.

#### Scenario: Weights do not reconcile
- **WHEN** a version's allocations sum to 1.07
- **THEN** the version becomes `invalid` with a finding naming the reconciliation rule, and it cannot be published

#### Scenario: Validation is repeatable
- **WHEN** validation is requested again on an already `validated` version
- **THEN** the same `validated` result and findings are returned, and no new transition is recorded

### Requirement: Publish an exact validated version
The API SHALL publish exactly one `validated` `plan_version_id` per publication. It records the checksum, supersedes the plan's previous publication without modifying it, and is guarded by the publication head's `expected_revision`. Publishing a version that is not `validated` MUST fail with `PRECONDITION_FAILED`.

#### Scenario: Publish
- **WHEN** a client publishes `validated` `pv_B` with the current publication `expected_revision`
- **THEN** a `publication_id` referencing `pv_B` and its checksum is returned, and the prior publication remains readable and unchanged

#### Scenario: Concurrent publications
- **WHEN** two clients publish different versions with the same `expected_revision`
- **THEN** one succeeds and the other fails with `CONFLICT`

### Requirement: Executions recorded separately
The API SHALL record an execution as its own record with a minted `execution_id` that references one `publication_id`. The mode MUST be `paper` or `simulated`. Recording an execution MUST NOT modify the publication or plan version. Mode `live` MUST fail with `OPERATION_NOT_PERMITTED`.

#### Scenario: Paper execution
- **WHEN** a client records a paper execution for `pub_X`
- **THEN** an `execution_id` referencing `pub_X` is returned, and reading `pub_X` and its plan version shows them unchanged

#### Scenario: Execution against superseded publication
- **WHEN** a client records an execution for a publication that has since been superseded
- **THEN** the execution is recorded against the exact referenced `publication_id`, and the response flags `publication_superseded: true`

### Requirement: Idempotency and concurrency on all writes
Every state-changing operation SHALL require an `idempotency_key`. Every write to a mutable head MUST require `expected_revision`, following the contract identifier rules.

#### Scenario: Missing idempotency key
- **WHEN** a create-version request has no `idempotency_key`
- **THEN** it fails with `VALIDATION_FAILED`

#### Scenario: Duplicate publish
- **WHEN** a publish request is repeated with the same key and body
- **THEN** the original `publication_id` is returned and only one publication exists

### Requirement: Checksum-bearing reads and trusted references
Reads of versions, publications, executions and snapshots SHALL return platform identifiers, checksum, status, lineage and trusted artifact references. Downloads MUST be served as short-lived, platform-issued grants. Responses MUST NOT expose bucket names or object keys.

#### Scenario: Download plan artifact
- **WHEN** a client requests the artifact of `pv_B`
- **THEN** it receives a trusted artifact reference with checksum and a time-limited download grant, and the downloaded bytes hash to that checksum

### Requirement: Schema upgrade compatibility
Stored records SHALL carry the `contract_version` they were written under. The API MUST read and serve records written under any earlier minor of a served major without rewriting them. A new major MUST be served alongside the previous one.

#### Scenario: Record from older minor
- **WHEN** a version written under contract 1.0.0 is read by a platform release pinned to 1.1.0
- **THEN** it is returned unchanged with `contract_version` 1.0.0, its checksum is unchanged, and it validates under 1.1.0

### Requirement: Plan head and version list reads
The API SHALL provide read operations for a portfolio (with its `synthetic` flag and `revision`), a plan head (`current_version_id`, `revision`, `current_publication_id`, `publication_revision`) and a paginated, newest-first list of a plan's versions (identifier, parent, origin, status, checksum, creation time) with opaque page tokens.

#### Scenario: Read plan head
- **WHEN** a FinanceLambdasTool `reader` role reads plan `pl_P` after its head moved to `pv_B`
- **THEN** the response contains `current_version_id` `pv_B` and the current `revision`, matching what a website-path client reads

#### Scenario: Page through versions
- **WHEN** a client lists a plan's versions in pages while a new child version is committed
- **THEN** no version appears twice across pages, and every listed checksum equals the checksum returned by the single-version read

### Requirement: Snapshot observation reads
The API SHALL provide a bounded, paginated read of a snapshot's observations filtered by instrument and date range. It MUST return only committed snapshot content and MUST include the full payload's trusted artifact reference and checksum.

#### Scenario: Observation read outside coverage
- **WHEN** a client requests observations for dates outside the snapshot's coverage
- **THEN** the response contains only the covered observations and states the uncovered range, and no observation is fabricated

