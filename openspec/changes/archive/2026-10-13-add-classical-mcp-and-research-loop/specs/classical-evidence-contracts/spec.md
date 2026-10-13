# Spec Delta

## Purpose

Provide reproducible classical evidence contracts for auditable beta portfolio decisions, explanations and controlled research.

## ADDED Requirements

### Requirement: Additive classical interfaces
The contract package SHALL define strict requests and immutable analysis responses for traditional recommendations, attribution, performance, retrieval, feedback and bounded research without changing the PPO 1.3 interface.

#### Scenario: Invalid input
- **WHEN** an unknown algorithm or malformed analysis identifier is supplied
- **THEN** validation fails before invocation: VALIDATION_FAILED for an unknown algorithm and INVALID_IDENTIFIER for a malformed analysis identifier, consistent with the existing identifier convention.

### Requirement: Evidence identity and provenance
Each persisted analysis SHALL identify its kind, immutable analysis ID, trusted artifact reference and checksum. Recommendation evidence MUST bind market snapshot, completed decision date, portfolio context and solver settings.

#### Scenario: Evidence retrieval
- **WHEN** a stored analysis is retrieved
- **THEN** its identity, original inputs and checksum match the original issued record.

### Requirement: Bounded research and feedback
Research submission SHALL expose dry-run, explicit paid-run confirmation and idempotency. Feedback SHALL be immutable caller-attributed text linked to an analysis. Missing actuals or forecast evidence MUST be represented explicitly.

#### Scenario: Research estimate
- **WHEN** a reader requests dry-run research
- **THEN** a cost estimate is returned and no paid job starts.

### Requirement: Version immutability
The new schemas SHALL publish as one reproducible 1.4.0 package. Previously published 1.3.0 bytes MUST remain unchanged and every consumer MUST use the same 1.4.0 checksum.

#### Scenario: Consumer installation
- **WHEN** all four beta consumers install the new contract
- **THEN** their package digests match the published 1.4.0 artifact.
