# Spec Delta

## Purpose

Defines the single versioned contract package that FinancialPlanning publishes and every repository pins: JSON Schemas, semantic-versioning and pinning rules, the shared error and outcome conventions, synthetic fixtures, validators and the conformance tests that producers and consumers run.

## ADDED Requirements

### Requirement: Single published contract package
FinancialPlanning SHALL publish one contract package, the only source of cross-repository schemas. It contains JSON Schema (draft 2020-12) documents, synthetic fixtures, validators and the conformance suite. Consumer repositories MUST depend on a published version and MUST NOT vendor, copy or hand-edit its schema definitions.

#### Scenario: Consumer copies a schema
- **WHEN** a consumer repository contains a JSON Schema whose `$id` belongs to the contract package namespace
- **THEN** the consumer's conformance check fails and reports the copied `$id`

#### Scenario: Consumer depends on the package
- **WHEN** FinanceLambdasTool validates a tool response
- **THEN** it uses the schema and validator from its pinned contract package version

### Requirement: Contract package coverage
The contract package SHALL define schemas for at least: identifiers, input snapshot metadata, plan, plan version, publication, execution, configuration, job submission/status/result, trusted artifact reference, error envelope, capability description, release manifest, domain envelope, staged-output manifest, Excel plan template, import report, tool catalog, one request/response pair per published tool, and the on-behalf-of caller block. Version 1.0.0 MUST contain all of them.

#### Scenario: Schema inventory check
- **WHEN** the contract package build runs
- **THEN** it fails if any listed schema is missing or lacks at least one valid and one invalid fixture

#### Scenario: Staged-output manifest shared by producer and consumer
- **WHEN** FinanceModel writes a staged-output manifest and the platform accepts it
- **THEN** both validate it with the same pinned `core/v1/staged-output-manifest.json` schema, and neither repository holds its own copy

#### Scenario: Refresh on a non-session day
- **WHEN** a market-data refresh targets a holiday or weekend and the dataset has no committed snapshot yet
- **THEN** the response carries quality flag `no_session`, `new_snapshot` false and a null `input_snapshot_id` without a `snapshot`, and it validates against `tools/refresh-market-data-response`; when a committed snapshot exists, the response carries it

#### Scenario: Tool schemas consumed by the Gateway owner
- **WHEN** FinanceAgent registers Gateway targets
- **THEN** it derives each target schema from the tool schemas in its pinned contract package version, named by the tool catalog published by FinanceLambdasTool

### Requirement: Plan lifecycle API route schemas
The contract package SHALL define request and response schemas for the plan lifecycle API routes that no tool schema covers: create portfolio, create plan, create root version, record execution, the snapshot observation read and the staged-output outcome read. Each MUST have valid and invalid fixtures.

#### Scenario: Plan API routes validated against contract schemas
- **WHEN** the platform validates a create-portfolio, create-plan, create-root-version or record-execution request, or answers one of those routes, the observation read or the staged-output outcome read
- **THEN** its pinned contract version provides a `core/v1/api/*` or `finance/v1/api/*` schema for each of them, so the platform needs no platform-internal schema for those routes

### Requirement: Registered vocabularies and phase 2 additions
Contracts 1.0.0 SHALL register: snapshot `status`; the open `quality_flags` and trusted-reference `kind` enums; the job lifecycle `state`, `purpose`, `dry_run` and cost-estimate fields (including `budget_category`); the budget allocation categories; and the cost-allocation tag keys. Explanation-evidence kinds, lineage reproducibility fields, leakage flags and the portfolio-policy schema MUST arrive only as later minors.

#### Scenario: Budget categories registered
- **WHEN** a consumer validates a cost estimate
- **THEN** `budget_category` must be one of `platform_infra`, `cpu_research`, `bedrock_explanations`, `gpu` or `reserve`, and snapshot `status` one of `committed`, `approved` or `expired`

#### Scenario: Unknown value in an open enum
- **WHEN** a 1.0.0 consumer reads a snapshot whose `quality_flags` contains a value added in 1.1.0
- **THEN** validation passes, and the consumer treats the unknown flag as informational

#### Scenario: Job status with non-terminal state
- **WHEN** FinanceModel returns status `awaiting_approval` for a paid run
- **THEN** the response validates against the job status schema, and `completion_status` is absent until the run is terminal

### Requirement: Semantic versioning of contracts
The package SHALL follow semver. A minor release MUST only add optional fields, new enum values in fields whose schema marks them open, or new schemas. Removals, renames, type changes, newly required fields or meaning changes MUST be a major release. Each schema `$id` MUST include its major version.

#### Scenario: Compatibility gate catches a breaking change in a minor
- **WHEN** a pull request marked as a minor release makes an optional field required
- **THEN** the package's compatibility check against the previous release fails the build

#### Scenario: Older document under newer minor
- **WHEN** a document produced under 1.2.0 is validated with 1.3.0
- **THEN** validation passes

#### Scenario: Breaking change before 1.0.0
- **WHEN** a 0.x release (beta-only, before 1.0.0) contains a breaking change
- **THEN** the compatibility gate passes only with its explicit 0.x exemption, the release notes name every breaking change, and from 1.0.0 on the same change needs a major release

### Requirement: Immutable, pinned contract releases
Published contract versions SHALL be immutable and carry a SHA-256 digest. Consumers MUST pin an exact version and digest in their build configuration and MUST record the pinned version in their release manifest entry.

#### Scenario: Republish attempt
- **WHEN** the pipeline tries to publish a version number that already exists with an artifact whose digest differs from the published one
- **THEN** publication fails and the existing version remains unchanged

#### Scenario: Unchanged version rebuilt
- **WHEN** a later build produces the already published version with the same digest
- **THEN** nothing is published, the existing version remains unchanged and the build continues

#### Scenario: Digest mismatch
- **WHEN** a consumer build downloads the pinned version and its digest differs from the pinned digest
- **THEN** the consumer build fails

### Requirement: Standard error envelope
Every cross-repository API, Lambda and tool error SHALL use the error envelope: `code` (from the registered code list), `message`, `retryable` (boolean), `details` (object), `correlation_id` and `contract_version`. Errors MUST NOT include secrets, stack traces or raw storage locations.

#### Scenario: Error envelope shape
- **WHEN** any producer returns an error
- **THEN** the body validates against the error envelope schema and carries a `correlation_id` that appears in the producer's logs

### Requirement: Registered error codes
The contract package SHALL register at least these codes, each with a default `retryable` value: `VALIDATION_FAILED`, `INVALID_IDENTIFIER`, `NOT_FOUND`, `CONFLICT`, `IDEMPOTENCY_KEY_REUSED`, `IMMUTABLE_RECORD`, `PRECONDITION_FAILED`, `UNAUTHORIZED`, `FORBIDDEN`, `OPERATION_NOT_PERMITTED`, `BUDGET_EXCEEDED`, `RATE_LIMITED`, `DEPENDENCY_UNAVAILABLE`, `UNSUPPORTED_CONTRACT_VERSION`, `INTERNAL`. Adding a code is a minor release.

#### Scenario: Throttled dependency
- **WHEN** a tool Lambda is rate-limited by a data provider
- **THEN** it returns code `RATE_LIMITED` with `retryable` true and no partial state change

#### Scenario: Validation error is not retryable
- **WHEN** a request fails schema validation
- **THEN** the error has code `VALIDATION_FAILED`, `retryable` false and `details` naming the failing JSON pointer

#### Scenario: Unsupported contract major
- **WHEN** a request declares a contract major the producer does not serve
- **THEN** the producer returns `UNSUPPORTED_CONTRACT_VERSION` listing the majors it serves

### Requirement: Completion status separate from solution status
Job and run results SHALL report `completion_status` (`succeeded`, `failed`, `cancelled`, `timed_out`) separately from `solution_status` (`optimal`, `feasible`, `infeasible`, `unbounded`, `no_effect`, `not_applicable`). An infeasible or no-effect outcome MUST be reported as `completion_status` `succeeded`, not as an error.

#### Scenario: Infeasible constraints
- **WHEN** an optimization job finishes and proves the constraint set infeasible
- **THEN** the result has `completion_status` `succeeded` and `solution_status` `infeasible`, and no plan version is created

#### Scenario: Job crashes
- **WHEN** a job's container exits abnormally
- **THEN** the result has `completion_status` `failed`, `solution_status` absent, and an error envelope in `error`

### Requirement: Trusted artifact references
Contracts SHALL represent stored artifacts as trusted references (`artifact_id`, owning domain, kind, checksum, `content_type`) resolved by the owning service. Requests MUST NOT accept caller-chosen storage paths, and responses MUST NOT expose raw bucket names or object keys.

#### Scenario: Caller supplies an S3 URI
- **WHEN** a tool request includes an `s3://` URI as an input location
- **THEN** it is rejected with `VALIDATION_FAILED`

### Requirement: Shared synthetic fixtures
The package SHALL ship deterministic synthetic fixtures for every schema, flagged `synthetic: true`. They MUST cover valid, invalid, duplicate-request, conflict, partial-output, infeasible, no-effect and schema-upgrade cases. Fixtures MUST contain no real holdings, account exports, credentials or live market values.

#### Scenario: Fixture hygiene scan
- **WHEN** the package build runs
- **THEN** it fails if any fixture lacks `synthetic: true` or matches the credential/identifier scan

#### Scenario: Colleague tests a Lambda directly
- **WHEN** a developer invokes a tool Lambda locally with a package fixture
- **THEN** the call needs no AWS credentials or provider access and returns a result that validates against the pinned schema

### Requirement: Validators and conformance suite
The package SHALL provide validators usable from Python and TypeScript, plus a conformance suite that producers run against their responses and consumers run against their requests and fixture handling. Each repository's build stage MUST run the suite for its pinned version.

#### Scenario: Producer conformance
- **WHEN** the platform build runs the conformance suite against its plan API handlers using package fixtures
- **THEN** every response validates and every error matches the error envelope

#### Scenario: Consumer conformance failure blocks build
- **WHEN** a FinanceAgent tool-call request built from a fixture fails validation
- **THEN** the FinanceAgent build stage fails before any deployment
