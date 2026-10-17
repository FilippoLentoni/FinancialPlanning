## ADDED Requirements

### Requirement: Exact immutable inference evidence
The platform SHALL capture the actual canonical prepared input before inference and retain verified immutable S3 bytes linked to a DynamoDB input record. Shared source evidence SHALL remain separate from approach-specific supplied input. Failed invocations SHALL retain captured inputs and explicit errors. Credential fields and future-available evidence SHALL be rejected.

#### Scenario: Input retry after a crash
- **WHEN** the same experiment, approach and scheduled slot retries
- **THEN** the original input bytes and capture time SHALL be reused and different supplied bytes SHALL be rejected

### Requirement: Permanent slot and isolated authoritative ledger
The platform SHALL permanently reserve experiment/approach/slot uniqueness without TTL. Model invocations SHALL support bounded exclusive leases and stale-lease recovery. Automatic paper updates SHALL operate only on expressly isolated synthetic experiment books and atomically commit the existing portfolio head, decision, immutable history and run execution receipt. Ordinary user portfolio confirmation SHALL remain unchanged.

#### Scenario: Concurrent rebalances
- **WHEN** two recommendations attempt the same portfolio revision
- **THEN** at most one SHALL commit a revision and a repeated committed run SHALL return its original execution without another fill

### Requirement: Bounded evidence queries and environment isolation
Beta evidence tables SHALL expose paginated experiment/day and experiment/approach indexes and preserve KMS encryption and point-in-time recovery. Immutable input/observation resource policies SHALL deny mutations. Gamma/prod SHALL gain no new evidence resources or access exception.

#### Scenario: Historical report lookup
- **WHEN** a report requests an earlier decision's input
- **THEN** it SHALL retrieve and verify that decision's original captured artifact, rather than regenerate a feature or substitute current shared data
