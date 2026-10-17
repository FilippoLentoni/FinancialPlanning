## ADDED Requirements

### Requirement: Configured beta development operators
The system SHALL admit exact IAM role names configured in beta dev_desktop_access to beta table data, storage objects and API routes, including the handler authorization layer. The system SHALL reject nonempty exceptions in gamma and prod. No wildcard or resource ARN SHALL be accepted as a role name.

#### Scenario: EC2 inspects beta
- **WHEN** the configured development operator reads beta tables, objects or APIs
- **THEN** access-control allowlists SHALL admit it subject to its identity permissions

#### Scenario: Higher environments
- **WHEN** a development operator accesses gamma or prod
- **THEN** the beta exception SHALL NOT grant additional access

### Requirement: Integrity controls remain effective
The beta exception SHALL preserve TLS and encryption requirements, immutable records, append-only history, cross-environment denials, compute budget limits, explicit strategy confirmation and submitter/approver separation.

#### Scenario: Operator attempts an integrity violation
- **WHEN** the operator mutates append-only history, deletes an immutable record, bypasses encryption or approves its own compute run
- **THEN** the existing integrity control SHALL reject the action
