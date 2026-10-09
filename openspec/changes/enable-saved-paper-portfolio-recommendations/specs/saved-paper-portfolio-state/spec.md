# Saved paper portfolio state

## Purpose

Persist hypothetical portfolio positions once so recommendation tools can load a consistent portfolio state and approved market reference without asking users to repeat holdings.

## ADDED Requirements

### Requirement: Audited portfolio paper state
The platform SHALL persist validated USD paper positions, cash, peak value, state date and provenance with an independent optimistic revision, idempotency record and audit event. Only operator or platform service principals SHALL write it.

#### Scenario: Initialize existing portfolio
- **WHEN** an operator initializes a synthetic portfolio with expected revision zero
- **THEN** state revision one and the complete paper state are atomically stored and audited

#### Scenario: Replay and conflict
- **WHEN** an identical initialization is replayed with its original idempotency key
- **THEN** the original result is returned without another state mutation
- **WHEN** a different update uses a stale state revision
- **THEN** a typed conflict is returned

#### Scenario: Read-only inference
- **WHEN** a reader or inference principal requests saved state
- **THEN** the complete state and revision are returned
- **WHEN** that principal attempts an update
- **THEN** access is denied

### Requirement: Latest approved snapshot lookup
The platform SHALL expose the newest readable approved snapshot for a validated dataset identity. Unapproved and expired snapshots SHALL be excluded, and absence SHALL return a typed not-found error.

#### Scenario: Ignore newer unapproved data
- **WHEN** the newest catalog entry is committed and an older approved entry exists
- **THEN** the approved entry is returned through the static latest route

### Requirement: Saved recommendation contracts
Recommendation requests SHALL accept saved-default or identified paper portfolios and optional paired snapshot/date overrides while retaining explicit supplied holdings. Responses SHALL support state provenance and fractional share changes. Ambiguous saved and supplied holdings SHALL be invalid.

#### Scenario: Default tool invocation
- **WHEN** a recommendation request is an empty object
- **THEN** it is valid and requests the saved default portfolio

### Requirement: Explicit beta paper initialization
The beta operator initializer SHALL use the existing plan allocation and approved closing prices to initialize the user-approved $10,000 paper portfolio once. It SHALL preserve existing state and SHALL never execute recommendations or broker trades.

#### Scenario: Safe repeated initialization
- **WHEN** initialization runs against an existing saved state
- **THEN** it returns that state without changing positions or revision
