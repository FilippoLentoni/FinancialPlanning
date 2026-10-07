# Spec Delta

## Purpose

Defines the authoritative metadata store for platform state (portfolios, plans, plan versions, publications, executions, snapshot catalog, idempotency records and audit events) and the conditional-write and transaction guarantees every state change relies on.

## ADDED Requirements

### Requirement: Authoritative metadata records
The platform SHALL store portfolio, plan, plan-version, publication, execution, snapshot-catalog, idempotency and audit-event records in a per-environment metadata store. Record shapes MUST validate against the pinned contract package schemas. No other repository MUST read or write the store directly.

#### Scenario: Consumer reads plan state
- **WHEN** FinanceLambdasTool needs a plan version
- **THEN** it calls the plan lifecycle API, and a direct metadata-store access attempt by its role is denied

#### Scenario: Stored record conforms
- **WHEN** a plan version record is committed
- **THEN** it validates against the contract package plan-version schema for the recorded `contract_version`

### Requirement: Artifact-before-metadata commit ordering
For any record that references immutable artifacts, the platform SHALL write and checksum the artifacts first and commit the metadata record second. A metadata record MUST NOT become visible unless every referenced artifact exists with the recorded checksum.

#### Scenario: Crash between artifact write and commit
- **WHEN** the process fails after writing a plan-version artifact but before the metadata commit
- **THEN** no plan version is visible through the API, and the orphaned artifact is reported by the orphan sweep and removed after the configured grace period

### Requirement: Atomic version creation with head move
Creating a plan version SHALL be one conditional transaction. It inserts the version record (conditioned on the ID not existing), moves the plan head with a condition on `expected_revision`, increments `revision`, and writes the idempotency record and an audit event. Either all parts commit or none does.

#### Scenario: Concurrent child versions
- **WHEN** two requests both create a child of the head with `expected_revision` 4
- **THEN** exactly one commits and moves the head to revision 5, the other fails with `CONFLICT`, and the metadata store contains exactly one new version

#### Scenario: Transaction partial failure
- **WHEN** the idempotency-record condition fails inside the transaction
- **THEN** neither the version record nor the head move is committed

### Requirement: Conditional status transitions
Lifecycle status changes (plan version `pending_validation` to `validated` or `invalid`, execution status, snapshot catalog status) SHALL be conditional on the current status, follow the contract's allowed transitions, and append an audit event in the same transaction. Content fields of committed records MUST NOT change.

#### Scenario: Invalid transition
- **WHEN** a request tries to move a version from `invalid` to `validated`
- **THEN** it fails with `PRECONDITION_FAILED`, and the record and audit log are unchanged

#### Scenario: Content edit refused
- **WHEN** any write attempts to change the allocations or checksum of a committed plan version
- **THEN** it fails with `IMMUTABLE_RECORD`

### Requirement: Idempotency store
The metadata store SHALL hold idempotency records keyed by caller principal, environment, operation and `idempotency_key`. Each record holds the request hash and the original response and is retained for at least 7 days. A repeat with a matching hash MUST return the stored response. A repeat with a different hash MUST fail with `IDEMPOTENCY_KEY_REUSED`.

#### Scenario: Retry after timeout
- **WHEN** a client's create-version call times out after commit and the client retries with the same key and body
- **THEN** the stored response with the original `plan_version_id` is returned and no second version exists

#### Scenario: Same key, different principal
- **WHEN** two different principals use the same `idempotency_key` for the same operation
- **THEN** the two requests are treated independently

### Requirement: Append-only audit events
Every state change SHALL append an audit event with the event ID, record ID, operation, caller principal, `correlation_id`, prior and new status or revision, and timestamp. Audit events MUST NOT be updated or deleted by any application role.

#### Scenario: Audit trail for a publication
- **WHEN** a version is published
- **THEN** an audit event records the `publication_id`, `plan_version_id`, checksum, caller and `correlation_id`

### Requirement: Point-in-time recovery and environment isolation
Each environment's metadata store SHALL be separate, tagged with its environment, encrypted with the environment's platform KMS key, and have point-in-time recovery enabled. Access MUST be limited to the platform roles of the same environment.

#### Scenario: Gamma role reads prod metadata
- **WHEN** a gamma platform role attempts to read the prod metadata store
- **THEN** access is denied
