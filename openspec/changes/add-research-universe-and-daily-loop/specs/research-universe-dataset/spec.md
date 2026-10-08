# Spec Delta

## Purpose

Defines the `equity-etf-daily` research-universe dataset (VOO, GOOGL, NFLX, AAPL, NVDA plus cash). It covers adjusted daily observations from 2010-10-01, universe-level snapshot approval, mandatory hindsight and survivorship disclosures, and the beta-first rollout of the real provider.

## ADDED Requirements

### Requirement: Universe dataset identity
The platform SHALL register a dataset `equity-etf-daily` whose instrument list comes from environment configuration. The initial list is VOO (`etf`), GOOGL, NFLX, AAPL and NVDA (`equity`), plus one modeled cash instrument (`cash`). The `etf-daily` dataset and its SPY configuration MUST remain unchanged, and a request for one dataset MUST NOT be served by the other.

#### Scenario: Both datasets served
- **WHEN** a phase 2 environment runs the scheduled ingestion
- **THEN** it commits one `etf-daily` snapshot for SPY and one `equity-etf-daily` snapshot for the five tickers, each with its own `input_snapshot_id` and dataset identity

#### Scenario: Unknown ticker in configuration
- **WHEN** the configured universe names an instrument without a declared kind
- **THEN** the build-stage configuration check fails naming the instrument

### Requirement: Adjusted daily observations from 2010-10-01
Each universe snapshot SHALL contain completed daily observations for every non-cash instrument for every XNYS regular session from 2010-10-01 through the snapshot's session date. Each observation holds unadjusted OHLCV plus `adj_close`, `dividend` and `split_ratio`. The snapshot MUST declare `adj_close` as the return basis for splits and dividends.

#### Scenario: Split inside the history
- **WHEN** an instrument had a stock split during the covered range
- **THEN** the snapshot carries that session's `split_ratio`, the `adj_close` series is continuous across the split, and the unadjusted close shows the raw jump

#### Scenario: Range before the start date
- **WHEN** a request asks for universe observations before 2010-10-01
- **THEN** it fails with `VALIDATION_FAILED` naming the dataset start date

### Requirement: Modeled cash instrument
The cash instrument SHALL have no provider observations. The snapshot MUST declare its return assumption (`zero_nominal` in this change), and FinanceModel and the reports MUST read the assumption from the snapshot.

#### Scenario: Cash in a snapshot
- **WHEN** a universe snapshot is committed
- **THEN** its `universe` block lists the cash instrument with kind `cash` and `return_assumption` `zero_nominal`, and it has no observations

### Requirement: Adjustment revisions are recorded
Because the provider rewrites historical adjusted closes after new dividends or splits, each universe ingestion SHALL re-fetch the full history per instrument. Changed historical `adj_close` values MUST be recorded as `source_revised` revisions, and earlier snapshots MUST NOT change.

#### Scenario: Dividend rewrites history
- **WHEN** a new dividend causes the provider to return different `adj_close` values for past sessions
- **THEN** the new snapshot holds the new values with a `source_revised` flag and a revision count, and the previous snapshot's content and checksum are unchanged

### Requirement: Universe snapshot approval rule
A universe snapshot SHALL become `approved` only under rule `approval-v2-universe`. Every non-cash instrument MUST have a `completed_daily` bar for the session and full coverage from 2010-10-01, and the snapshot MUST have no blocking quality flag. If any instrument fails, the whole snapshot MUST stay `committed`.

#### Scenario: One ticker missing today's bar
- **WHEN** NFLX has no completed bar for the session after retries while the other four do
- **THEN** the snapshot is committed with `partial_response` and `missing_sessions` naming NFLX, it stays `committed`, and FinanceModel cannot read it

#### Scenario: Clean universe snapshot
- **WHEN** all five tickers return complete histories and the session bar
- **THEN** the snapshot becomes `approved` with `approval_rule_version` `approval-v2-universe`, and an audit event is recorded

### Requirement: Hindsight and survivorship disclosures
Every universe snapshot SHALL carry `bias_disclosures` containing `hindsight_selection` and `survivorship`. Each entry has a fixed explanatory text: the instruments were chosen in 2026 knowing they performed well, and delisted or failed companies are absent. The platform MUST refuse to commit a universe snapshot without both disclosures.

#### Scenario: Snapshot read shows disclosures
- **WHEN** any client reads a universe snapshot's metadata
- **THEN** the response includes both disclosures with their texts

#### Scenario: Disclosure removed from configuration
- **WHEN** the environment configuration omits a mandatory disclosure for `equity-etf-daily`
- **THEN** the build-stage configuration check fails

### Requirement: Beta-first real-provider rollout
The `yfinance` provider for `etf-daily` and `equity-etf-daily` SHALL be enabled by setting `phase: 2` per environment: beta first, then gamma, then prod after the pipeline's manual approval. An environment MUST NOT be promoted to phase 2 until its predecessor has produced an approved universe snapshot from a scheduled run.

#### Scenario: Gamma promoted before beta verified
- **WHEN** the gamma configuration declares phase 2 and beta has no approved scheduled universe snapshot with `yfinance` lineage
- **THEN** the promotion gate fails naming the missing beta evidence

#### Scenario: Beta phase 2 verified in the deployed environment
- **WHEN** the beta scheduled ingestion runs after the phase 2 deployment
- **THEN** reading the latest universe snapshot through the deployed plan API shows `lineage.provider` `yfinance`, five instruments covered from 2010-10-01, status `approved` and both bias disclosures
