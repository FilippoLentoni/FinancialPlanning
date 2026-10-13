## Purpose

Raise the project ceiling and explicit category allocations through the existing shared tooling infrastructure; retain enforcement and distinguish project from account spend.

## ADDED Requirements

### Requirement: Authorized project ceiling

The project SHALL enforce the user-authorized USD 100 total ceiling with explicit category allocations totaling at most 100, preserving unrelated account budgets and distinguishing delayed billed costs from estimates.

#### Scenario: Budget configuration changes

- **WHEN** the authorized revision is deployed
- **THEN** AWS Budget and the published ceiling SHALL agree at 100 while enforcement remains enabled

### Requirement: Bounded research accounting

The system SHALL retain independent job, category and project limits; increasing the project ceiling SHALL NOT authorize unlimited training or expand GPU approvals.

#### Scenario: Research estimates exceed a limit

- **WHEN** a proposed experiment exceeds any active limit
- **THEN** submission SHALL fail before compute is created

