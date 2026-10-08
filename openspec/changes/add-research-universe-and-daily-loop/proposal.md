# Proposal

## Why

The user has set the first real-data research universe and a daily recommendation loop (decisions 16–22, 2026-10-08), but the platform serves only one ETF series (`etf-daily`). It cannot ingest a mixed ETF and single-stock universe, cannot trigger FinanceModel after ingestion, and commits model-run versions as plain `validated` versions with no approval step. This change adds those three behaviors. Models and agents run only in the deployed AWS stack (decision 16), so every acceptance check here runs against deployed beta, gamma and prod.

## What Changes

- **New multi-instrument dataset `equity-etf-daily`** (decision 17). The research universe is VOO, GOOGL, NFLX, AAPL and NVDA plus a modeled cash instrument. It holds daily completed observations from `yfinance` with adjusted close (splits and dividends), starting 2010-10-01. The existing `etf-daily` dataset (SPY) is unchanged.
- **Universe snapshot approval rule** (`approval-v2-universe`): a universe snapshot is `approved` only when every instrument has a completed bar for the session, full coverage from the start date and no blocking flag. Otherwise the whole snapshot stays `committed`.
- **Hindsight and survivorship disclosure.** Every universe snapshot carries `bias_disclosures` (`hindsight_selection`, `survivorship`). Plan versions and staged outputs derived from it carry the same disclosures, so reports and the agent must show them.
- **Phase 2 rollout.** `yfinance` is enabled in beta first, then in gamma and prod after the pipeline's manual approval. Each environment is verified against its own deployed scheduled snapshot.
- **Daily recommendation loop** (decisions 18, 20, 22):
  - After the 09:00 ET scheduled ingestion produces an approved universe snapshot, the platform reads the FinanceModel-owned SSM key `/finplan/<env>/financemodel/config/production-strategy`.
  - If the key holds a strategy, the platform submits ONE `daily_recommendation` job through the FinanceModel job API.
  - If no strategy is set, nothing runs: no job, no benchmark, no plan version and no SageMaker cost. The skip is audited.
- **Budget pre-check.** Before submitting, the loop refuses when the AWS Budgets deny action is active, and it records a `skipped_budget` outcome. FinanceModel's `cpu_research` check stays authoritative.
- **Pending-approval plan versions** (decision 19). The loop accepts the run's staged output as a new plan version (origin `model_run`) with `review_state: pending_approval`. Nothing auto-publishes:
  - Publishing such a version requires an explicit user approval block on the publish route.
  - A new review route records a rejection.
  - A newer accepted recommendation marks older pending ones `superseded`.
  - The scheduler and workflow roles are denied the publish and review routes.
- **Contracts 1.1.0 (backward-compatible minor).** It adds:
  - the `equity-etf-daily` dataset identity and the `instrument.kind` values `etf`, `equity` and `cash`;
  - the snapshot `universe` block and `bias_disclosures`;
  - the plan-version `review_state` and the publish `approval` block;
  - the review request and response schemas;
  - the `daily_recommendation` and `benchmark` job kinds, and the benchmark-report summary and reference schemas;
  - the production-strategy SSM key;
  - the request and response schemas for the new FinanceLambdasTool tools.

  Every addition is optional or a new schema. 1.0.0 documents still validate.
- **Out of scope:** live trading, real-holdings portfolios, intraday data, scheduled benchmarks or experiments (decision 22), automatic strategy selection, and any publication without the user's approval.

## Capabilities

### New Capabilities

- `research-universe-dataset`: the `equity-etf-daily` multi-instrument dataset, its adjusted-price semantics, start date, universe approval rule, bias disclosures and phased real-provider rollout.
- `daily-recommendation-loop`: the post-ingestion trigger that submits one FinanceModel `daily_recommendation` job only when a production strategy is configured. It also covers the budget pre-check, idempotency per session, run tracking and the acceptance of the staged output.
- `recommendation-approval`: the `pending_approval` review state of model-run plan versions, approval-gated publication, rejection, supersession and the denial of publication to automated principals.
- `recommendation-contracts`: the contracts 1.1.0 minor that carries the universe, review, job-kind, benchmark-report, strategy-key and tool schemas, and its compatibility rules.

### Modified Capabilities

None. `openspec/specs/` is empty. This change builds on `add-platform-foundation` and `establish-cross-repo-contracts` without altering their requirements:
- `etf-daily` stays as is;
- the 1.0.0 publish rule still holds for versions that have no `review_state`.

## Impact

- **Platform code (future):** dataset registry and `yfinance` multi-ticker fetch, the universe approval rule, a Step Functions Standard state machine for the daily loop, the review route, approval checks on publish, staged-output acceptance setting `review_state`, and environment config (`phase: 2`, datasets list, `research-plan-ref`).
- **Contracts:** `finplan-contracts` 1.1.0 is published from `contracts/` before any consumer pins it. FinanceModel, FinanceLambdasTool and FinanceAgent pin 1.1.0 in their changes `add-daily-recommendation-and-on-demand-experiments`, `add-approval-and-strategy-tools` and `add-recommendation-review-flow`.
- **Cross-repo grants:**
  - the platform loop role needs `submit_job`/`get_job_status` and the `production_candidate` purpose on the FinanceModel job API;
  - it needs read access to the FinanceModel production-strategy key;
  - the FinanceLambdasTool `plan-writer` role needs the review route.
- **AWS (us-east-2, single account):** serverless only. The new resources are one state machine, one Lambda and a small amount of S3 per environment. Each daily FinanceModel job costs at most about USD 0.12 (`ml.m5.xlarge`, 30 min cap), and only when a strategy is set: about USD 2.50 per month in prod, charged to `cpu_research`. Ingestion of five tickers adds negligible Lambda and S3 cost within `platform_infra`.
- **Data hygiene:** retrieved Yahoo data stays in per-environment buckets and is never committed; fixtures stay synthetic.
