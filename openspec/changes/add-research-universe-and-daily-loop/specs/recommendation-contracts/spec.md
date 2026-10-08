# Spec Delta

## Purpose

Defines the contracts 1.1.0 minor that carries the research universe, the recommendation review state, the daily and benchmark job kinds, benchmark-report references, the production-strategy key and the new tool schemas, all backward-compatible with 1.0.0.

## ADDED Requirements

### Requirement: Contracts 1.1.0 content
Contracts 1.1.0 SHALL add:
- the dataset identity `equity-etf-daily`;
- `instrument.kind` (`etf`, `equity`, `cash`);
- the snapshot `universe` block and `bias_disclosures`;
- the plan-version `review_state`, the publish `approval` block and the review request and response;
- the job kinds `daily_recommendation` and `benchmark`;
- `benchmark-report-summary` and its trusted-reference kind `benchmark_report`;
- the production-strategy document;
- one request and response pair per new tool.

#### Scenario: Schemas present
- **WHEN** the 1.1.0 package is built
- **THEN** each listed schema or field exists with a `$id` under major `v1`, and the conformance suite has valid and invalid fixtures for each

### Requirement: Backward compatibility with 1.0.0
Every 1.1.0 addition SHALL be an optional field, a value in an open enum, or a new schema, and the compatibility gate MUST pass without the 0.x exemption. Documents valid under 1.0.0 MUST validate under 1.1.0, and a 1.0.0 consumer MUST ignore the new fields.

#### Scenario: 1.0.0 plan version read under 1.1.0
- **WHEN** a plan version written under 1.0.0 is validated against 1.1.0
- **THEN** it validates unchanged with no `review_state`, and its checksum is unchanged

#### Scenario: 1.0.0 consumer reads a pending version
- **WHEN** a consumer pinned to 1.0.0 reads a version that has `review_state`
- **THEN** validation passes because the field is ignored as an unknown optional field

### Requirement: Production-strategy document and key
The contract SHALL register the FinanceModel-owned SSM key `/finplan/<env>/financemodel/config/production-strategy`. Its value is a JSON document with `strategy_id`, `model_version`, `configuration_id`, `set_by`, `set_at` and `registry_check` (the registry status at selection time). The platform loop role MUST be listed as a reader.

#### Scenario: Ownership check
- **WHEN** the ownership check runs on 1.1.0
- **THEN** the key's writer is FinanceModel's strategy-selection role only, and the platform loop role is a registered reader

### Requirement: Published before consumers
FinancialPlanning SHALL publish 1.1.0 to the contract registry and deploy the platform release that serves it in an environment before any consumer pinned to 1.1.0 is promoted there.

#### Scenario: Consumer promoted early
- **WHEN** FinanceLambdasTool pinned to 1.1.0 is promoted to gamma while the gamma platform serves only 1.0.0
- **THEN** the consumer's promotion gate fails naming the missing producer contract version
