# Spec Delta

## Purpose

Defines the contracts 1.1.0 minor that adds the research universe, the daily recommendation job kind, the production-strategy key and the production-strategy tool schemas, all backward-compatible with 1.0.0.

## ADDED Requirements

### Requirement: Contracts 1.1.0 additions
Contracts 1.1.0 SHALL add:
- the dataset `equity-etf-daily`;
- `instrument.kind` (`etf`, `equity`, `cash`);
- the snapshot `universe` block;
- `bias_disclosures` on snapshots and staged-output manifests;
- the job kind `daily_recommendation`;
- the FinanceModel-owned production-strategy key and document, with the platform trigger role as reader;
- the `production_strategy` tool request and response.

#### Scenario: Schemas present
- **WHEN** 1.1.0 is built
- **THEN** each addition exists under major `v1` with valid and invalid conformance fixtures, and the ownership check lists the key's single writer and its reader

### Requirement: Backward compatible with 1.0.0
Every 1.1.0 addition SHALL be optional, an open-enum value or a new schema, and the compatibility gate MUST pass without the 0.x exemption. FinancialPlanning MUST publish 1.1.0 and serve it in an environment before any 1.1.0 consumer is promoted there.

#### Scenario: Old document
- **WHEN** a 1.0.0 snapshot or plan version is validated under 1.1.0
- **THEN** it validates unchanged with the same checksum
