## Purpose

Retain real, reusable news-derived machine metadata for historical evaluation
and morning model state, while preserving source availability assumptions and
actual platform first-seen timestamps.

## Requirements

### Requirement: Immutable producer-owned numeric metadata

The beta ingestion producer SHALL retrieve GDELT daily GKG v1 archives, retain
the exact compressed bytes and SHA-256 checksums centrally, and freeze the
provider's metadata-use terms with attribution. Numeric derivatives SHALL use
the distinct `gdelt-gkg-v1-organization-tone-v1` feature version. Source article
text SHALL NOT be retrieved by this adapter. Missing archives and invalid rows
SHALL block a complete-coverage claim.

#### Scenario: Daily retained source

- **WHEN** the beta 08:45 America/New_York trigger runs on any calendar day
- **THEN** it freezes the previous UTC day's source and numeric derivatives
- **AND** it uses the existing producer role, project budget gate and retry DLQ
- **AND** weekend and holiday news is retained for the daily report even while equity markets are closed

### Requirement: Distinct historical and platform availability

Every derivative SHALL record the provider object's Last-Modified value, the
actual retrieval time, actual central availability and an explicitly labeled
conservative source-availability proxy. The proxy SHALL be the later of provider
Last-Modified and the next UTC day at 12:00. A retrospective source proxy SHALL
NOT be presented as this platform's original first-seen timestamp.

#### Scenario: Historical-source evaluation

- **WHEN** a caller explicitly requests historical-source-proxy mode
- **THEN** source objects unavailable by the decision cutoff are excluded
- **AND** the response declares the selected availability mode
- **AND** the original actual central availability remains in the provenance

#### Scenario: Platform evaluation

- **WHEN** a caller uses the default platform mode
- **THEN** only objects centrally available by its decision cutoff are returned

### Requirement: Bounded verified read API

The read-only `/v1/gdelt-metadata` route SHALL return immutable checksummed
numeric documents for at most thirty-one calendar source days and nine distinct
supported symbols. The response SHALL declare missing source days and complete
coverage separately from zero organization matches. Reader and model principals
SHALL NOT acquire producer storage write permissions through this route.

#### Scenario: Missing source day

- **WHEN** no eligible frozen archive exists for a requested source day
- **THEN** that day is listed as missing and coverage is incomplete
- **AND** fabricated zero-valued feature rows are not returned


### Requirement: Same-day premarket numeric state
The producer SHALL retain a separately versioned four-batch GKG2.1 source window07:45–08:45
America/New_York, using conservative source availability and fixed09:00 formation. It SHALL
run every calendar day08:45 and expose immutable documents through the beta read-only
`GET /v1/gdelt-intraday-metadata` route. Archive attempts and partial source coverage SHALL
remain explicit; known zero entity matches SHALL differ from unknown missing archives.
Historical missingness SHALL be identified as a current archive audit, not platform first-seen.

#### Scenario: Partial or unavailable window
- GIVEN one or more source archives cannot be retrieved
- WHEN a premarket source window is frozen
- THEN successful source counts remain observed lower bounds, failed attempts are retained,
  source coverage is captured batches divided by four, and wholly unavailable counts are null
- AND live recommendations fail closed if all archives are unavailable

### Requirement: Prices available before formation and for weekend reporting
Beta weekday price ingestion SHALL run08:50 America/New_York before09:00 formation.
A separate Friday17:05 capture SHALL ingest the completed session with an independent
idempotency key and SHALL NOT submit an additional daily model recommendation.


#### Scenario: Malformed row within a captured source archive
- GIVEN a valid source archive contains malformed physical TSV fragments
- WHEN numeric features are derived
- THEN valid records remain usable and the original archive plus each rejected line's
  number, checksum and reason are retained in a quarantine manifest
- AND total raw and rejected row counts remain explicit, quality becomes partial,
  counts remain observed lower bounds, and tone remains a potentially biased subset mean
- AND effective source coverage combines archive capture fraction and accepted row fraction
- AND wholly unusable source input produces unknown null counts and fails closed for live use
