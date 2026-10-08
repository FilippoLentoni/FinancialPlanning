# Proposal

## Why

The user set a real-data research universe and a daily recommendation loop (decisions 16–22, 2026-10-08). Today the platform serves only the single-ETF `etf-daily` dataset, and nothing triggers FinanceModel after ingestion. This change adds only those two deltas. Ingestion scheduling, the plan lifecycle, publication (the user's approval), staged-output acceptance, experiments and explanations already exist in `add-platform-foundation`, `establish-cross-repo-contracts` and the companion repos, and are not re-specified here.

## What Changes

- **New dataset `equity-etf-daily`** for VOO, GOOGL, NFLX, AAPL and NVDA, plus a modeled cash instrument:
  - daily completed observations from `yfinance`, with adjusted close for splits and dividends, from 2010-10-01;
  - an all-or-nothing universe approval rule;
  - mandatory hindsight and survivorship `bias_disclosures` on every snapshot, carried into staged outputs so reports flag them;
  - `etf-daily` (SPY) is unchanged.
- **Phase 2 enablement for the universe:** beta first, then gamma and prod after the pipeline's manual approval.
- **Post-ingestion daily trigger.** After the 09:00 ET scheduled ingestion approves a universe snapshot, the platform reads the FinanceModel-owned key `/finplan/<env>/financemodel/config/production-strategy`.
  - Only if the key holds a strategy, and the budget deny action is not active, does it submit one `daily_recommendation` job through the FinanceModel job API.
  - It accepts the staged output through the existing acceptance operation.
  - The result is a validated but **unpublished** plan version (origin `model_run`). The user publishes it through the existing publish path, and the trigger role is denied publication.
  - With no strategy set, nothing runs.
- **Contracts 1.1.0 (backward-compatible minor)**, containing only these additions:
  - the dataset identity and `instrument.kind` (`etf`, `equity`, `cash`);
  - the snapshot `universe` block and `bias_disclosures`;
  - the `daily_recommendation` job kind;
  - the production-strategy key and document;
  - the request and response pair for the one FinanceLambdasTool `production_strategy` tool.

## Capabilities

### New Capabilities

- `research-universe-dataset`: the `equity-etf-daily` dataset, its adjusted-price and cash semantics, the universe approval rule, bias disclosures and phased provider enablement.
- `daily-recommendation-trigger`: the post-ingestion, strategy-gated, budget-checked submission of one FinanceModel daily recommendation and the acceptance of its output as an unpublished plan version.
- `recommendation-contracts`: the contracts 1.1.0 additions and their compatibility.

### Modified Capabilities

None. `openspec/specs/` is empty, and existing changes are referenced, not altered.

## Impact

- **Platform:** a dataset registry entry, a multi-ticker fetch, the universe approval rule, one small Step Functions state machine, config (`phase: 2`, a datasets list, `research-plan-ref`), and grants to call the FinanceModel job API and read the strategy key.
- **Consumers:**
  - FinanceModel `add-daily-recommendation-and-on-demand-experiments`;
  - FinanceLambdasTool `add-approval-and-strategy-tools` (one strategy tool);
  - the FinanceAgent default model task in `add-explanation-workflows`.
- **Cost:** ingestion of five tickers is negligible within `platform_infra`. A daily job costs at most about USD 0.12 (about USD 2.50 per month in prod), and only once a strategy is set, charged to `cpu_research`.
- **Data hygiene:** retrieved Yahoo data stays in per-environment buckets and is never committed; fixtures stay synthetic.
