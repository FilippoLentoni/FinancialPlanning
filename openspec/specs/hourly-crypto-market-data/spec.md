# hourly-crypto-market-data Specification

## Purpose
Provide immutable, completed hourly crypto market data for bounded beta research while preserving the daily equity workflow.

## Requirements

### Requirement: Completed hourly crypto data
The system SHALL collect BTC-USD and ETH-USD completed hourly UTC candles on a beta-only 24/7 schedule, retain immutable source evidence and timestamps, reject invalid or missing aligned bars, and serve a bounded latest dataset separately from daily equities.

#### Scenario: An incomplete or stale candle is returned
- **WHEN** An incomplete or stale candle is returned
- **THEN** the snapshot is rejected without inventing or forward-filling observations

### Requirement: Idempotent and bounded ingestion
The system SHALL deduplicate hourly deliveries, enforce the shared USD 100 project budget before new collection, retain retry and DLQ handling, and preserve existing equity schedules.

#### Scenario: The same hourly event is delivered twice
- **WHEN** The same hourly event is delivered twice
- **THEN** the original snapshot is reused without duplicate simulated advancement

### Requirement: Versioned crypto tool contracts
The system SHALL publish additive schemas for crypto dataset reads, model comparison and synthetic portfolio history with explicit data provenance, modeled costs and ineligible model reasons.

#### Scenario: A consumer requests a live execution mode
- **WHEN** A consumer requests a live execution mode
- **THEN** the request fails validation
