## Purpose

Provide a separately governed research-data payment subsystem for portfolio planning while preserving the current paper-planning deployment and truthful integration readiness.

## ADDED Requirements

### Requirement: Separate payment permission boundary

Research-payment roles SHALL use a dedicated beta-only boundary scoped to the owned payment manager. Existing environment and shared boundaries SHALL retain their payment, wallet and broker denials.

#### Scenario: A payment role is provisioned

- **WHEN** a payment role is provisioned
- **THEN** its boundary SHALL permit only the reviewed payment/identity operations and existing planning roles SHALL remain unable to execute payments

### Requirement: Separated payment duties

Session management SHALL explicitly deny payment execution, and payment execution SHALL deny creating or expanding sessions and instruments. Credential retrieval SHALL be restricted to the AgentCore service and owned manager.

#### Scenario: An execution principal attempts to create a fresh session

- **WHEN** an execution principal attempts to create a fresh session
- **THEN** the operation SHALL be denied

### Requirement: Payment budget accounting

Setup SHALL remain within USD 5 incremental and USD 100 total. Research payment sessions SHALL have explicit finite limits; wallet spend SHALL be accounted separately from delayed AWS billing.

#### Scenario: Project budget evidence is missing or exhausted

- **WHEN** project budget evidence is missing or exhausted
- **THEN** new payment sessions SHALL be refused before authorization

