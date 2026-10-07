# Spec Delta

## Purpose

Keeps the snapshot, experiment, job, run-output and explanation machinery domain-neutral, with finance as the first registered domain adapter, so later domains such as supply-chain models can reuse that machinery without changing the shared contracts.

## ADDED Requirements

### Requirement: Domain-neutral envelope
Snapshot, configuration, job submission, run result and explanation-request contracts SHALL be a domain-neutral envelope (identifiers, lineage, timestamps, checksums, status, error, `domain`, `domain_schema_version`) plus a domain payload validated by the registered domain adapter's schema. Envelope schemas MUST NOT contain finance-specific fields.

#### Scenario: Finance payload
- **WHEN** a job submission has `domain` `finance` and a portfolio-optimization payload
- **THEN** the envelope validates against the core schema and the payload validates against the finance adapter schema

#### Scenario: Finance field in envelope
- **WHEN** a pull request adds a field such as `ticker` to an envelope schema
- **THEN** the contract package's domain-neutrality check fails

### Requirement: Domain adapter registration
Each domain SHALL be registered in the contract package with a domain key, payload schemas, fixtures and a deterministic validator. Producers MUST reject envelopes whose `domain` is unregistered or whose `domain_schema_version` they do not serve.

#### Scenario: Unregistered domain
- **WHEN** a job submission arrives with `domain` `supply_chain` before that adapter is registered
- **THEN** it is rejected with `VALIDATION_FAILED` naming the unknown domain

### Requirement: Finance is the initial domain adapter
The initial release SHALL register exactly one domain adapter, `finance`, which owns portfolios, instruments, allocations, constraints, fees and market-data observation payloads. Market-data semantics (completed daily observation versus intraday bar, session status) MUST be expressed in the finance adapter, not in the envelope.

#### Scenario: Daily observation flag
- **WHEN** a finance snapshot payload contains daily bars
- **THEN** each observation is marked `completed_daily` or `intraday_partial` and the envelope carries only domain-neutral coverage and quality flags

### Requirement: Domain-neutral explanation evidence
Explanation requests and results SHALL reference deterministic numerical evidence artifacts (by trusted artifact reference) separately from narrative text. The evidence schema MUST be defined by the domain adapter and narrative MUST NOT be the sole carrier of numeric results.

#### Scenario: Explanation result structure
- **WHEN** an explanation of a new versus previous recommendation is produced
- **THEN** the result contains evidence artifact references with checksums and a separate narrative field, and the numbers in the evidence are reproducible from the referenced versions
