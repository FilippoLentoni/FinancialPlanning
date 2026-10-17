# Spec Delta

## Purpose
Make external ETF news auditable and useful for portfolio research without leaking future information or claiming unmeasured improvement.

## ADDED Requirements

### Requirement: Immutable source evidence
The system SHALL preserve payload and payment evidence with checksums, source, query scope, publication, retrieval and availability timestamps; unknown rights SHALL remain visible as ineligible.

#### Scenario: Unknown licensed status
- **WHEN** a successful paid delivery has unknown research rights
- **THEN** it is retained for audit and excluded from algorithm features

### Requirement: Point-in-time feature gate
The system SHALL use only verified research rights, validated source timestamps and complete eligible records available before the decision; bounded features SHALL be versioned and stale or future records excluded.

#### Scenario: Future information
- **WHEN** a record becomes available after the requested decision
- **THEN** that record cannot contribute to optimizer, PPO or LLM inputs

### Requirement: Honest model comparison
The system SHALL expose price-only and news-enriched input provenance and preserve matched constraints; PPO training and claims of accuracy improvement SHALL remain ineligible without compatible artifacts and matched outcome evidence.

#### Scenario: Forward corpus only
- **WHEN** historical point-in-time news coverage is absent
- **THEN** historical news backtest and improved accuracy are reported unavailable
