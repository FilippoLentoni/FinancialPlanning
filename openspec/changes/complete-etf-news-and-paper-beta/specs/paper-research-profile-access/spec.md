# Spec Delta

## Purpose
Allow portfolio research to honor an isolated paper profile's instrument universe through scoped metadata reads.

## ADDED Requirements
### Requirement: Model profile metadata reads
The model API principal SHALL read portfolio metadata and settings without receiving portfolio state-write permission.

#### Scenario: Resolve an isolated research profile
- **WHEN** the model requests an identified paper portfolio
- **THEN** its synthetic status and configured instrument universe can be read while state updates remain denied

### Requirement: Narrow brokerage credential exception
The contract permission gate SHALL permit only the specifically registered beta Agent brokerage reader's dedicated read-only credential access. Other live-financial credential access, credential writes and live trading permissions SHALL remain rejected.

#### Scenario: Broader permissions are introduced
- **WHEN** a template substitutes another role, secret, environment or a write action
- **THEN** the live-permission gate rejects the access
