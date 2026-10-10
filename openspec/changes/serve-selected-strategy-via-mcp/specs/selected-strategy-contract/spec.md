# Spec Delta

## Purpose

Defines portable input and provenance contracts for invoking the portfolio strategy selected from offline experimentation.

## ADDED Requirements

### Requirement: Explicit portfolio state
The recommendation contract SHALL require an approved snapshot identifier, a decision date and observed holdings, cash, portfolio value and high-water mark. It MUST reject unknown fields and malformed values before a producer call.

#### Scenario: Missing portfolio state
- **WHEN** a recommendation omits holdings or supplies a negative weight
- **THEN** contract validation rejects it

### Requirement: Selected strategy provenance
The response SHALL carry the selected strategy, configuration, source run, artifact checksum, complete target allocation and buy/sell/hold deltas. It MUST distinguish an advisory decision from a return forecast or an executed trade.

#### Scenario: Strategy changes
- **WHEN** the operator pins a different evaluated strategy
- **THEN** the same request schema returns that strategy's allocation and provenance

### Requirement: Frozen export selector
The export configuration SHALL identify a succeeded source run and an optional evaluated strategy identifier. Selection MUST NOT change training inputs, weights or hyperparameters or authorize another training run.

#### Scenario: Unevaluated strategy
- **WHEN** an export asks for a strategy absent from the source experiment
- **THEN** the producer rejects it without training
