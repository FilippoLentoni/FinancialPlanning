# Spec Delta

## Purpose

Defines the single platform ingestion operation (normalize, validate, persist, dedupe) that produces immutable input snapshots. It is invoked both by a daily America/New_York schedule and on demand, with explicit session-calendar, observation-kind and provider-capability semantics.

## ADDED Requirements

### Requirement: One ingestion operation, two triggers
The platform SHALL implement one ingestion operation used by both the scheduled trigger and on-demand callers (agent tools via FinanceLambdasTool, website, operators), published at `/finplan/<env>/financialplanning/api/ingestion-endpoint`. Both triggers MUST run the identical normalize, validate, persist and dedupe implementation and return the same result contract.

#### Scenario: Same result shape
- **WHEN** the scheduled run and an on-demand call ingest the same dataset and session from the fixture provider
- **THEN** both results validate against the same contract schema, and their curated observations have identical normalized content and checksums

#### Scenario: Trigger recorded
- **WHEN** any ingestion completes
- **THEN** the snapshot catalog records the trigger (`scheduled` or `on_demand`) and the caller principal

### Requirement: Daily schedule in America/New_York
The platform SHALL run the scheduled trigger on weekdays in the `America/New_York` time zone. The local time MUST come from `/finplan/<env>/financialplanning/config/ingest-schedule`, which accepts `09:00` or `09:30`. The default is `09:00`. Daylight-saving transitions MUST NOT shift the local run time.

#### Scenario: Configured time
- **WHEN** the environment configuration sets `09:30`
- **THEN** the deployed schedule fires at 09:30 America/New_York on weekdays

#### Scenario: Daylight-saving change
- **WHEN** US daylight saving time begins or ends
- **THEN** the scheduled run still fires at the configured local time

#### Scenario: Invalid time value
- **WHEN** the configuration contains a value other than `09:00` or `09:30`
- **THEN** the build stage fails before deployment

### Requirement: Exchange session calendar
Ingestion SHALL determine session status for each requested date from a versioned exchange session calendar: `regular`, `early_close`, `holiday` or `weekend`. It MUST record the calendar version used. A scheduled run on a non-session day MUST complete without a provider call and report `no_session` in its result.

#### Scenario: Exchange holiday
- **WHEN** the scheduled trigger fires on a weekday that the calendar marks `holiday`
- **THEN** no provider request is made, the result has quality flag `no_session`, and the most recent snapshot is referenced instead of a new one being created

#### Scenario: Holiday before any snapshot exists
- **WHEN** the scheduled trigger fires on a `holiday` and the dataset has no committed snapshot yet
- **THEN** no provider request is made and the result has quality flag `no_session`, `new_snapshot` false, a null `input_snapshot_id` and no `snapshot`, and it validates against the contract `tools/refresh-market-data-response`

#### Scenario: Calendar coverage gap
- **WHEN** the requested date is outside the calendar's covered range
- **THEN** the ingestion fails with `PRECONDITION_FAILED` and details naming the calendar coverage

### Requirement: Completed daily observations versus intraday bars
Each observation SHALL be marked `completed_daily` or `intraday_partial`. A daily observation MUST be marked `completed_daily` only when its session has closed per the calendar (including early closes) and the provider reports it final. Scheduled runs before the open MUST ingest the most recent completed session.

#### Scenario: Morning scheduled run
- **WHEN** the scheduled run fires at 09:00 ET on a trading day
- **THEN** it ingests the previous completed session's daily observation as `completed_daily` and does not create a daily observation for the current session

#### Scenario: On-demand during the session
- **WHEN** an on-demand call during regular hours requests the current session from a provider that declares intraday support
- **THEN** current-session observations are marked `intraday_partial`, and the snapshot carries quality flag `contains_intraday_partial`

### Requirement: Declared provider capabilities
Each provider adapter SHALL declare its supported datasets, granularities (`daily`, `intraday`), history depth and rate limits. Ingestion MUST reject requests outside the declared capabilities and MUST NOT infer intraday support from daily support.

#### Scenario: Intraday request to a daily-only provider
- **WHEN** an on-demand request asks for intraday bars from a provider that declares only `daily`
- **THEN** it fails with `PRECONDITION_FAILED`, details `provider_capability_unsupported`, and no snapshot is created

#### Scenario: Provider throttling
- **WHEN** the provider rate-limits the request
- **THEN** the ingestion returns `RATE_LIMITED` with `retryable` true, and no partial snapshot is committed

### Requirement: Normalization and validation
Ingestion SHALL normalize provider records to the finance adapter's observation schema (instrument, dataset, session date, observation kind, source timestamp, currency, adjustment basis). It MUST validate them (schema, monotonic dates, non-negative prices and volumes, OHLC consistency, calendar alignment). Every finding becomes a quality flag or a rejection.

#### Scenario: Missing session
- **WHEN** the provider returns no record for a date the calendar marks `regular`
- **THEN** the snapshot is committed with quality flag `missing_sessions` listing the date, and its coverage excludes that date

#### Scenario: Inconsistent bar
- **WHEN** a record's high is below its low
- **THEN** the record is rejected, the snapshot carries quality flag `rejected_records` with the count, and the rejected record is retained in the `raw` bucket

### Requirement: Deduplicated persistence
Curated observations SHALL be keyed by dataset, instrument, session date, observation kind and source timestamp, and written with conditional creates, so repeated ingestion never duplicates them. A `completed_daily` observation that differs from an already stored one for the same key fields MUST be stored as a revision with quality flag `source_revised`.

#### Scenario: Re-ingesting the same session
- **WHEN** the same completed session is ingested twice with identical provider content
- **THEN** the curated store holds one observation for that key, and the second snapshot's manifest references it

#### Scenario: Provider revises a close
- **WHEN** the provider returns a different close for an already stored `completed_daily` observation
- **THEN** both values are retained, and the new snapshot carries `source_revised` naming the instrument and date

### Requirement: Immutable snapshot results
Each successful ingestion SHALL commit an immutable snapshot and return `input_snapshot_id`, the manifest checksum, source-data timestamps, the retrieval timestamp, coverage start and end, quality flags, provider ID, dataset identity and calendar version. Snapshot artifacts MUST never be rewritten.

#### Scenario: Snapshot returned
- **WHEN** an on-demand ingestion succeeds
- **THEN** the response contains all listed fields, and the website and agent reading that `input_snapshot_id` later receive the same manifest checksum

#### Scenario: Same content, later retrieval
- **WHEN** an ingestion at a later retrieval time yields content identical to the previous snapshot
- **THEN** a new `input_snapshot_id` is minted with the same content checksum, and the response flags `no_new_observations`

### Requirement: Idempotent triggers
On-demand ingestion SHALL require an `idempotency_key`. Scheduled runs MUST derive a deterministic key from environment, dataset and scheduled session date, so scheduler retries or duplicate deliveries return the original result instead of creating another snapshot.

#### Scenario: Duplicate scheduler delivery
- **WHEN** the scheduler delivers the same scheduled invocation twice
- **THEN** exactly one snapshot exists for that scheduled run, and both invocations report the same `input_snapshot_id`

### Requirement: Phase 1 fixture provider
In phase 1, the only enabled provider SHALL be a deterministic fixture provider serving synthetic data flagged `synthetic: true`, plus a programmable provider mock for tests. The `yfinance` provider MUST be enabled only in an environment whose configuration declares phase 2.

#### Scenario: Real provider configured in phase 1
- **WHEN** a phase 1 environment configuration names a provider other than the fixture provider
- **THEN** the build stage fails with a message naming the phase gate

#### Scenario: yfinance enabled in phase 2
- **WHEN** an environment configuration declares phase 2 and provider `yfinance`
- **THEN** the build passes and that environment's scheduled and on-demand ingestion use the `yfinance` adapter

#### Scenario: Prod schedule in phase 1
- **WHEN** the prod schedule fires in phase 1
- **THEN** it ingests from the fixture provider, and the snapshot is flagged `synthetic: true`

### Requirement: Initial instrument is an S&P 500 tracking-ETF daily series
The only enabled dataset in phases 1 and 2 SHALL be the daily series of one S&P 500 tracking ETF (for example SPY) under the `etf-daily` dataset identity, with the ticker taken from configuration (user decision, 2026-10-07). It MUST serve completed daily observations only; configuring an intraday dataset or granularity MUST fail the build.

#### Scenario: Fixture ETF series in phase 1
- **WHEN** phase 1 ingestion runs on a session day
- **THEN** it produces `completed_daily` observations for the configured ETF under the `etf-daily` dataset, flagged `synthetic: true`

#### Scenario: Intraday configured
- **WHEN** environment configuration enables an intraday granularity for any dataset
- **THEN** the build-stage configuration check fails

### Requirement: Separate datasets for index, ETF and constituents
The platform SHALL treat an index level series, a tracking-ETF price series and a constituent universe as distinct datasets with distinct dataset identities. Ingestion of one MUST NOT satisfy a request for another.

#### Scenario: Index requested, ETF available
- **WHEN** a request asks for the index-level dataset and only the ETF dataset is configured
- **THEN** the request fails with `VALIDATION_FAILED` naming the unknown dataset

### Requirement: Snapshot approval status
Each snapshot SHALL carry a `status` of `committed`, `approved` or `expired` and the `approval_rule_version` applied. A snapshot MUST become `approved` only through an audited conditional transition when it matches the versioned approval rule. Only `approved` snapshot artifacts MAY be readable by the FinanceModel job role.

#### Scenario: Clean fixture snapshot
- **WHEN** a phase 1 fixture ingestion commits a snapshot with no blocking quality flag
- **THEN** the snapshot becomes `approved`, an audit event records the rule version, and the FinanceModel job role can read its artifacts

#### Scenario: Snapshot with blocking flag
- **WHEN** a snapshot is committed with `stale_source`
- **THEN** it stays `committed`, and a FinanceModel read of its artifacts is denied

### Requirement: yfinance provider adapter
The platform SHALL provide a `yfinance` provider adapter behind the provider adapter interface that fetches the configured S&P 500 tracking ETF's daily completed OHLCV, adjusted close, dividends and splits, declares only `daily` granularity and needs no API key or secret. The `yfinance` library version MUST be pinned exactly, and no repository other than FinancialPlanning MAY call the provider.

#### Scenario: Daily fetch normalized
- **WHEN** the adapter returns daily records for the configured ETF from a mocked library response
- **THEN** ingestion stores unadjusted OHLCV with the recorded adjustment basis, and stores adjusted close, dividend and split ratio as separate observation fields

#### Scenario: Unpinned library
- **WHEN** the dependency lock does not pin `yfinance` to an exact version
- **THEN** the build stage fails

#### Scenario: Finality inferred
- **WHEN** the adapter returns a bar for a session that closed less than the configured settle delay ago
- **THEN** the bar is not marked `completed_daily`, and a bar past the settle delay is marked `completed_daily` with quality flag `finality_inferred`

### Requirement: XNYS session calendar from a pinned library
The versioned session calendar SHALL be generated at build time from the `exchange_calendars` library (exchange `XNYS`) at an exactly pinned version, and the calendar version MUST identify the library, its version and the coverage range. The synthetic fixture calendar MAY be used only with the fixture provider and in tests.

#### Scenario: Calendar version recorded
- **WHEN** a snapshot is committed from the `yfinance` adapter
- **THEN** its manifest records a calendar version naming `XNYS`, the calendar library and its pinned version

#### Scenario: Early close from the library calendar
- **WHEN** the generated calendar marks a session `early_close`
- **THEN** a bar for that session can be `completed_daily` only after the early close time plus the settle delay

### Requirement: Provider lineage with library version
Each snapshot SHALL record provider lineage under `lineage`: `provider`, `provider_library`, `library_version` and the retrieval timestamp, alongside the calendar version. Snapshot results and snapshot reads MUST return these lineage fields.

#### Scenario: Lineage on a yfinance snapshot
- **WHEN** an on-demand ingestion with the `yfinance` adapter succeeds
- **THEN** the result and later snapshot reads show `provider` `yfinance`, `provider_library` `yfinance`, the pinned `library_version` and the retrieval timestamp

### Requirement: Provider rate limiting and backoff
The `yfinance` adapter SHALL enforce a configured minimum interval between provider requests and SHALL retry throttling, transient errors and empty responses with exponential backoff and jitter, up to a configured attempt count that fits within the function timeout. When attempts are exhausted it MUST return `RATE_LIMITED` or `DEPENDENCY_UNAVAILABLE` with `retryable` true and commit no partial snapshot.

#### Scenario: Throttled then succeeds
- **WHEN** the mocked library raises a throttling error twice and then returns data
- **THEN** the adapter waits with increasing backoff, the third attempt succeeds and one snapshot is committed

#### Scenario: Attempts exhausted
- **WHEN** every attempt is throttled
- **THEN** the ingestion returns `RATE_LIMITED` with `retryable` true and no snapshot is created

### Requirement: Empty and partial provider responses are quality flags
After retries, an empty provider response for a range containing `regular` sessions SHALL produce a snapshot flagged `empty_response` and `missing_sessions`, and `empty_response` MUST block approval. A response missing some sessions or fields SHALL produce a snapshot flagged `partial_response` with the gaps listed in `missing_sessions`.

#### Scenario: Empty response
- **WHEN** the provider keeps returning no rows for a regular session after all retries
- **THEN** the snapshot is committed with `empty_response` and `missing_sessions`, stays `committed`, and a FinanceModel read of it is denied

#### Scenario: Missing adjusted close
- **WHEN** the provider returns OHLCV for a session without the adjusted close
- **THEN** the snapshot carries `partial_response` naming the session and the missing field

### Requirement: No retrieved market data in the repository
Retrieved provider data SHALL be stored only in the platform's per-environment buckets and MUST NOT be committed to the repository. Every market-data fixture, including adapter response fixtures, MUST be synthetic and flagged `synthetic: true`. CI MUST use only the mock and fixture providers.

#### Scenario: Non-synthetic fixture committed
- **WHEN** a fixture file lacks the synthetic flag or matches the real-data signature list
- **THEN** the build-stage data-hygiene check fails

#### Scenario: CI run
- **WHEN** the unit, contract, integration-beta, gamma or smoke suites run in the pipeline
- **THEN** no request reaches the real provider

#### Scenario: Optional live shape test
- **WHEN** an operator opts in to the live-provider test
- **THEN** it makes one rate-limited small request and asserts columns, types and calendar alignment, never price values
