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

### Requirement: First-valid forecast realization has strong completeness evidence
The beta evidence backend SHALL select the first eligible realized observation using a bounded, resumable strong-read proof of deterministic scheduled run and valuation IDs. An observation-time secondary index SHALL NOT establish completeness. Original forecasts, inputs, valuations and portfolio ledger entries SHALL remain immutable while optional search-head attributes advance optimistically.

#### Scenario: Earlier valuation is omitted by index propagation
- **WHEN** a later valid mark is visible in an index but an earlier scheduled slot is missing, running or absent from that index
- **THEN** strong original slot reads SHALL select the true earlier eligible mark or retain explicit pending status until the slot has terminal evidence
- **AND** a later mark with otherwise correct error arithmetic SHALL NOT be permanently accepted without the first-valid proof

#### Scenario: A long horizon requires multiple bounded proof chunks
- **WHEN** more than168 original scheduled slots must be checked
- **THEN** each call SHALL check at most168 slots and persist an immutable checked-prefix receipt plus optimistic continuation
- **AND** restart or concurrent retries SHALL resume the authoritative cursor without changing original forecast bytes or repeating model inference
