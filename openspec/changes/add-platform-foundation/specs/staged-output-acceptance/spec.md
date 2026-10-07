# Spec Delta

## Purpose

Defines how FinanceModel production-worker output enters the platform. Workers write to a platform-owned staging area, and only the platform validates and commits that output as a plan version, so research compute never mutates authoritative plan state.

## ADDED Requirements

### Requirement: Platform-owned run-output staging area
The platform SHALL provide a per-environment staging area, referenced by `/finplan/<env>/financialplanning/config/run-staging-ref`. Production workers write output under a `run_id`-scoped location there. The worker role MUST be able only to create objects in staging, and MUST NOT read other runs' staging data or any committed plan artifact.

#### Scenario: Worker writes its run
- **WHEN** the beta FinanceModel job role writes output for `run_R`
- **THEN** the objects are created under the `run_R` staging location

#### Scenario: Worker reads another run
- **WHEN** the job role attempts to read staging objects of `run_S`
- **THEN** access is denied

### Requirement: Staged output manifest written last
A staged output SHALL be complete only when its manifest exists. The manifest lists every file with its SHA-256, plus `run_id`, `model_version`, `configuration_id`, `input_snapshot_id`, `plan_id`, optional `parent_plan_version_id`, `completion_status`, `solution_status` and `contract_version`. Acceptance MUST NOT start for a run without a manifest.

#### Scenario: Acceptance before manifest
- **WHEN** acceptance is requested for a run whose manifest is not present
- **THEN** it fails with `PRECONDITION_FAILED`, details `staged_output_incomplete`, and no state changes

### Requirement: Structural acceptance checks
Before any commit, acceptance SHALL verify that:
- every listed file exists and matches its checksum;
- no unlisted files exist;
- the manifest validates against the contract;
- `run_id` and `model_version` exist in FinanceModel's published registry reference for the same environment;
- `input_snapshot_id` exists in the snapshot catalog.

Any failure MUST reject the output with no plan version.

#### Scenario: Checksum mismatch
- **WHEN** one staged file's SHA-256 differs from the manifest
- **THEN** the output is rejected with `VALIDATION_FAILED`, a rejection record lists the file, and no plan version is created

#### Scenario: Unknown model version
- **WHEN** the manifest names a `model_version` absent from the environment's FinanceModel registry reference
- **THEN** the output is rejected with `VALIDATION_FAILED` naming `model_version`

#### Scenario: Model service release absent
- **WHEN** no FinanceModel release is recorded in the environment
- **THEN** acceptance fails with `DEPENDENCY_UNAVAILABLE`

### Requirement: Outcome-aware acceptance
Acceptance SHALL honour the contract's separate completion and solution statuses:
- `failed`, `cancelled` or `timed_out` runs MUST be recorded as rejected, with no version;
- `succeeded` with `infeasible` or `unbounded` MUST record the outcome with no version;
- only `succeeded` with `optimal`, `feasible` or `no_effect` MAY proceed to commit.

#### Scenario: Infeasible run
- **WHEN** the manifest reports `completion_status` `succeeded` and `solution_status` `infeasible`
- **THEN** the acceptance result records the infeasible outcome, no plan version is created, and the response is not an error

#### Scenario: Failed run
- **WHEN** the manifest reports `completion_status` `failed`
- **THEN** the run is recorded as rejected with its error envelope, and no plan version is created

#### Scenario: Outcome read by FinanceModel
- **WHEN** the FinanceModel job-API role reads the acceptance outcome for `run_R`
- **THEN** it receives `accepted` with the `plan_version_id`, `rejected` with the error envelope, or `no_version` with the solution status, and it cannot call any write route

### Requirement: Validation gate before plan-version commit
For outputs passing the structural checks, the platform SHALL copy the payload into the immutable `plans` artifacts, commit a version with origin `model_run` and full lineage, and run deterministic validation before the response. Content failing validation (for example, missing required instruments) MUST end as `invalid` and never `validated`.

#### Scenario: Partial output
- **WHEN** a staged allocation omits instruments required by the plan's configuration
- **THEN** the committed version has status `invalid` with a finding listing the missing instruments, and publishing it fails with `PRECONDITION_FAILED`

#### Scenario: Valid output
- **WHEN** a complete, reconciling staged output is accepted
- **THEN** a version with origin `model_run`, the manifest's `run_id`, `model_version`, `configuration_id` and `input_snapshot_id`, and status `validated` is returned

### Requirement: Idempotent and concurrency-safe acceptance
Acceptance SHALL require an `idempotency_key` and the plan head's `expected_revision`. Accepting the same `run_id` twice MUST return the original result, and a `run_id` MUST produce at most one plan version.

#### Scenario: Duplicate acceptance with new key
- **WHEN** acceptance for an already accepted `run_R` is requested with a different `idempotency_key`
- **THEN** it fails with `CONFLICT`, details reference the existing `plan_version_id`, and no second version exists

#### Scenario: Head moved during acceptance
- **WHEN** the plan head revision changes between the request and the commit
- **THEN** acceptance fails with `CONFLICT`, and the staged output remains eligible for a retry with the new revision

### Requirement: Phase 1 fixture worker output
In phase 1, acceptance SHALL be exercised with synthetic staged outputs produced by test fixtures or the FinanceModel CPU stub job. No GPU or real model compute MUST be required to test any acceptance path.

#### Scenario: Fixture acceptance in beta
- **WHEN** the beta integration suite stages the contract package's valid, partial, failed and infeasible fixtures
- **THEN** each yields the outcome its scenario above defines
