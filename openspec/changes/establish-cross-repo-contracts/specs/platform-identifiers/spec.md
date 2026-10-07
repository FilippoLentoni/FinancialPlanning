# Spec Delta

## Purpose

Defines the canonical identifiers shared by every repository: their formats, which service mints each one, immutability and lineage rules (including override child versions and publication versus execution), and the idempotency and optimistic-concurrency semantics of every state-changing operation.

## ADDED Requirements

### Requirement: Canonical identifier set and formats
The system SHALL use exactly these identifiers: `portfolio_id` (`pf_`), `plan_id` (`pl_`), `plan_version_id` (`pv_`), `input_snapshot_id` (`snap_`), `model_version` (`mv_`), `configuration_id` (`cfg_`), `run_id` (`run_`), `publication_id` (`pub_`), `execution_id` (`exe_`). Each is the prefix plus a 26-character Crockford base32 ULID, except `configuration_id`, which is content-addressed.

#### Scenario: Valid identifier
- **WHEN** a contract validator receives `pv_01JA2B3C4D5E6F7G8H9JKMNPQR`
- **THEN** it accepts the value as a `plan_version_id`

#### Scenario: Wrong prefix
- **WHEN** a `plan_version_id` field contains a value starting with `pl_`
- **THEN** validation fails with error code `INVALID_IDENTIFIER` and names the field

### Requirement: Content-addressed configuration identifier
`configuration_id` SHALL be `cfg_` followed by the lowercase hex SHA-256 of the configuration document canonicalized per RFC 8785 (JSON Canonicalization Scheme). Two semantically identical configurations MUST yield the same `configuration_id`.

#### Scenario: Key order differs
- **WHEN** two configuration documents differ only in key order and whitespace
- **THEN** both produce the same `configuration_id`

#### Scenario: Value differs
- **WHEN** a risk-aversion parameter changes from 2.0 to 2.5
- **THEN** a different `configuration_id` is produced

### Requirement: Identifier minting authority
FinancialPlanning SHALL mint `portfolio_id`, `plan_id`, `plan_version_id`, `input_snapshot_id`, `publication_id` and `execution_id`. FinanceModel SHALL mint `model_version` (model registry) and `run_id` (job submission interface). Any party MAY compute `configuration_id`. No other repository MUST mint these identifiers.

#### Scenario: Tool wrapper needs a new plan version
- **WHEN** a FinanceLambdasTool plan operation creates an override
- **THEN** it calls the platform API and returns the `plan_version_id` the platform minted

#### Scenario: Client-supplied plan_version_id
- **WHEN** a create-version request includes its own `plan_version_id`
- **THEN** the platform rejects it with `VALIDATION_FAILED`

### Requirement: Immutable identified records
Records identified by `plan_version_id`, `input_snapshot_id`, `model_version`, `configuration_id`, `run_id` results, `publication_id` and `execution_id` SHALL be immutable in content once committed. Only lifecycle status fields defined by the contract MAY change, and each status change MUST be recorded as an append-only event.

#### Scenario: Attempt to edit a committed plan version
- **WHEN** a client attempts to modify the allocations of an existing `plan_version_id`
- **THEN** the request is rejected with `IMMUTABLE_RECORD` and the client is directed to create a child version

#### Scenario: Snapshot checksum is stable
- **WHEN** the website and the agent each retrieve the same `input_snapshot_id`
- **THEN** both receive the same content checksum

### Requirement: Input snapshot identity
Each `input_snapshot_id` SHALL reference an immutable set of artifacts together with a SHA-256 manifest, source-data timestamps, retrieval timestamp, date coverage, quality flags, dataset identity and domain. Object-store versioning MUST NOT be used as the snapshot or plan-version identity.

#### Scenario: Snapshot metadata returned
- **WHEN** a consumer requests snapshot metadata
- **THEN** the response contains the manifest checksum, source timestamps, retrieval timestamp, coverage start/end and quality flags, with the provider lineage under `lineage` (`provider`, optional `provider_library`, `library_version` and `calendar_version`; the field is `provider`, not `provider_id`)

### Requirement: Plan version lineage and overrides
Every plan version SHALL record `plan_id`, `parent_plan_version_id` (null only for a root), `input_snapshot_id`, `configuration_id`, `model_version`, `run_id` (null for manual or Excel versions), origin (`model_run`, `manual_override`, `excel_import`) and a content checksum. An override MUST create a new child version and never alter its parent.

#### Scenario: Excel override round trip
- **WHEN** a user uploads an edited workbook against version `pv_A`
- **THEN** the platform creates child `pv_B` with parent `pv_A`, origin `excel_import` and lineage to the retained source file, and `pv_A` is unchanged

#### Scenario: No-effect override
- **WHEN** an override's canonical content equals its parent's content
- **THEN** a child version is still created and its checksum equals the parent checksum, so the no-effect outcome is observable

### Requirement: Plan version validation status
A plan version SHALL have status `pending_validation`, `validated` or `invalid`. It MUST enter `validated` only after deterministic platform validation (schema, accounting reconciliation, constraints) passes. Worker output from model runs MUST pass this gate before it becomes a plan version.

#### Scenario: Partial worker output
- **WHEN** a model run produces output missing some required instruments
- **THEN** the platform records the version as `invalid` with error details, or rejects the staging commit, and never marks it `validated`

### Requirement: Publication references an exact validated version
A publication SHALL reference exactly one `plan_version_id` whose status is `validated` and record its checksum. Publishing an unvalidated version MUST fail. A later publication for the same plan supersedes the earlier one without modifying it.

#### Scenario: Publish validated version
- **WHEN** a user publishes `pv_B` that is `validated`
- **THEN** a `publication_id` is minted that references `pv_B` and its checksum

#### Scenario: Publish invalid version
- **WHEN** a user publishes a version with status `invalid` or `pending_validation`
- **THEN** the request fails with `PRECONDITION_FAILED` and no publication is created

### Requirement: Execution is separate from publication
An execution SHALL be a separate record with its own `execution_id` that references one `publication_id`, with mode `paper` or `simulated` in phase 1. Executions MUST NOT modify the publication or plan version. Live execution modes MUST be rejected until a separate change introduces them.

#### Scenario: Paper execution of a publication
- **WHEN** a paper execution is requested for `pub_X`
- **THEN** an `execution_id` is minted referencing `pub_X`, and `pub_X` is unchanged

#### Scenario: Live mode requested
- **WHEN** an execution request specifies mode `live`
- **THEN** it is rejected with `OPERATION_NOT_PERMITTED`

### Requirement: Idempotency keys on state-changing operations
Every state-changing operation SHALL accept a required `idempotency_key` (1–128 characters from `[A-Za-z0-9_-]`) scoped to caller principal, environment and operation, retained for at least 7 days. A repeat with the same key and request hash MUST return the original result. The same key with a different hash MUST fail with `IDEMPOTENCY_KEY_REUSED`.

#### Scenario: Duplicate request
- **WHEN** a client retries a create-version request with the same key and identical body
- **THEN** the original `plan_version_id` is returned and no second version is created

#### Scenario: Key reused with different body
- **WHEN** a client sends the same key with a different body
- **THEN** the request fails with `IDEMPOTENCY_KEY_REUSED` and `retryable` false

#### Scenario: Proxied call from a tool adapter
- **WHEN** two different end callers send the same `idempotency_key` to the same tool, which proxies the request to a producer under its own role
- **THEN** the tool forwards distinct derived keys (`lt_` plus the SHA-256 of caller identity, environment, tool and key), so the producer records two independent operations, and each caller's retry returns its own original result

### Requirement: Optimistic concurrency on mutable heads
Mutable heads (a plan's current-version pointer, current publication, portfolio settings) SHALL carry a monotonically increasing integer `revision`. Writes MUST supply `expected_revision`. A mismatch MUST fail with `CONFLICT` and not apply the change.

#### Scenario: Concurrent override
- **WHEN** two clients both create child versions with `expected_revision` 4 and the first commit moves the head to revision 5
- **THEN** the second request fails with `CONFLICT`, no child version is committed for it (version creation and head move are one conditional transaction), and the client must re-read before retrying
