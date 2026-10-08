# Spec Delta

## Purpose

Defines the `equity-etf-daily` research-universe dataset (VOO, GOOGL, NFLX, AAPL, NVDA plus cash). It covers adjusted daily observations from 2010-10-01, all-or-nothing approval, mandatory hindsight and survivorship disclosures, and the beta-first enablement of the real provider.

## ADDED Requirements

### Requirement: Universe dataset identity
The platform SHALL register `equity-etf-daily` with a configured instrument list. Initially that is VOO (`etf`), GOOGL, NFLX, AAPL and NVDA (`equity`), and one modeled cash instrument (`cash`, no observations, `return_assumption` `zero_nominal`). The `etf-daily` dataset MUST remain unchanged.

#### Scenario: Both datasets served
- **WHEN** a phase 2 environment runs the scheduled ingestion
- **THEN** it commits one `etf-daily` SPY snapshot and one `equity-etf-daily` snapshot, each with its own `input_snapshot_id`, and the universe snapshot lists the cash instrument with its assumption and no observations

### Requirement: Adjusted daily history from 2010-10-01
Each universe snapshot SHALL hold, for every non-cash instrument, completed daily observations for every XNYS session from 2010-10-01 through the session date. Each observation has unadjusted OHLCV plus `adj_close`, `dividend` and `split_ratio`, with `adj_close` declared as the return basis. Because the provider rewrites history after dividends and splits, each run MUST re-fetch the full history and flag changed values `source_revised`.

#### Scenario: Dividend rewrites history
- **WHEN** a new dividend changes past `adj_close` values returned by the provider
- **THEN** the new snapshot carries the new values flagged `source_revised`, and the previous snapshot's checksum is unchanged

### Requirement: All-or-nothing universe approval
A universe snapshot SHALL become `approved` only under rule `approval-v2-universe`. Every non-cash instrument MUST have the session's `completed_daily` bar and full coverage from 2010-10-01, and the snapshot MUST carry no blocking flag. Otherwise it MUST stay `committed`.

#### Scenario: One ticker missing
- **WHEN** NFLX has no completed bar for the session after retries
- **THEN** the snapshot is committed with `partial_response` naming NFLX, it stays `committed`, and FinanceModel cannot read it

### Requirement: Hindsight and survivorship disclosures
Every universe snapshot SHALL carry `bias_disclosures` with `hindsight_selection` (the instruments were chosen in 2026 knowing they did well) and `survivorship` (failed or delisted companies are absent). Staged outputs derived from it MUST carry the same disclosures. A configuration without both MUST fail the build.

#### Scenario: Snapshot read
- **WHEN** any client reads a universe snapshot's metadata
- **THEN** both disclosures and their texts are present

### Requirement: Beta-first provider enablement
`yfinance` for `equity-etf-daily` SHALL be enabled by `phase: 2` per environment: beta first, then gamma, then prod after the pipeline's manual approval. An environment MUST NOT be promoted to phase 2 before its predecessor has an approved scheduled universe snapshot with `yfinance` lineage.

#### Scenario: Gamma before beta evidence
- **WHEN** gamma declares phase 2 and beta has no approved scheduled universe snapshot
- **THEN** the promotion gate fails naming the missing beta evidence
