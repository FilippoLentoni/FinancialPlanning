# Design

## Context

The platform ingestion Lambda and API already own raw/curated storage and metadata; all release pipelines enforce exact contract-wheel pins. FinanceModel has a beta classical inference Lambda, write-once S3 analyses and a separately scheduled research controller. FinanceLambdasTool discovers modules from its fixed catalog; LangGraph offers catalog tools and confirms state changes. AgentCore payment manager and authorized Coinbase connector exist independently, but hosted payments and historical paid-news coverage do not.

## Goals / Non-Goals

Goals: hourly real crypto data, forward paper books, comparable evidence, agent-accessible research, budgeted optional data acquisition.
Non-goals: real orders, withdrawals, autonomous mainnet payments, profit guarantees, silent equity-policy reuse, or hourly retraining.

## Decisions

The first forward collection window ends 2026-10-21 00:00 UTC (at most seven days); schedules stop there unless deliberately extended after cost review.

Use BTC-USD and ETH-USD public Coinbase hourly candles (3600 seconds, bounded 168-hour lookback), collected at minute 5 UTC. Keep missing intervals visible and reject stale snapshots. Store write-once hourly artifacts with checksums under a separate namespace; expose data via IAM-authenticated beta API. Reuse the ingestion function's DLQ and budget enforcement; existing equity schedules stay intact.

At minute 10, the existing FinanceModel research controller advances synthetic books from previously committed targets and subsequently observed opens. Each slot has a deterministic claim and immutable record; retries replay the committed result. Insufficient data, gaps, conflicting parent records and incompatible artifacts fail closed. Each book starts with USD 100 cash. Fees are an explicitly modeled 60 bps and slippage 10 bps, not an assertion of the user's exchange fee tier. Compare cash, BTC buy-and-hold, equal-weight buy-and-hold, minimum variance, mean variance and CVaR; report PPO and LLM ineligibility until compatible validated artifacts exist.

Keep supplemental purchase execution in the existing operator-only AgentCore adapter. The hosted agent can discover resources and identify an approval prerequisite; it cannot sign arbitrary challenges. Imported data must retain provider/receipt checksums, retrieved/published/available timestamps and license scope. Free-vs-enriched comparisons must share periods, costs, seeds and timing; live retrieval cannot backfill historical information availability.

Publish an additive contract release, repin all four repositories, register tools, and add a crypto paper-research skill and routing guidance to LangGraph. Preserve all existing trading/payment denials. Update the conference paper with implementation and pending experiment status.

## Risks / Trade-offs

Hourly activity raises request volume: two bounded public market requests per run, no hourly GPU jobs or paid LLM loops. AWS billing lags: retain the USD 100 project ceiling and USD 5 incremental cap, and record the distinction from simulated capital. Historical candles may have gaps: reject, do not fill. Tiny portfolios are cost-sensitive: costs apply to buys and sells. Synthetic fills approximate execution and cannot establish live profitability.

## Migration Plan

Validate contract fixtures, ingestion timing/replay, next-bar fills, model compatibility and tool/graph integration. Build and deploy beta through owned pipelines, retaining manual promotion gates. Verify hourly schedule, one real data collection, immutable paper evidence and hosted chat. Roll back the recorded release and disable new schedules if necessary; preserve immutable artifacts. Archive only after required deployment checks succeed.
