# Proposal

## Why

The contract baseline (change `establish-cross-repo-contracts`) fixes identifiers, schemas, ownership and the pipeline standard, but nothing yet holds authoritative plan state or produces input snapshots. FinanceModel, FinanceLambdasTool and FinanceAgent all integrate against the platform first (integration order: platform, then model service, then tool wrappers, then agent/Gateway). They need a deployable, fixture-backed platform in beta, gamma and prod before they can promote. The platform must also fit under the user's USD 50 total AWS budget (everything, not split per repo), inside the `platform_infra` category of USD 8, so phase 1 is serverless, on-demand and near-zero cost.

## What Changes

- **Per-environment platform storage.** Six buckets per environment (`raw`, `curated`, `snapshots`, `plans`, `outputs`, `reports`), with SSE-KMS encryption, TLS-only access, Block Public Access, scoped grants, write-once immutable artifacts, and per-environment retention and lifecycle rules. Bucket names are published only through `/finplan/<env>/financialplanning/config/*`.
- **Metadata store.** Holds portfolio, plan, plan version, publication, execution, snapshot catalog, idempotency and audit-event records. Every state change is a conditional write or a transaction. Version creation, head move and the idempotency record commit together.
- **Plan lifecycle API.** IAM-authenticated, conforming to contract package 1.x. It covers creating a portfolio and a plan, creating root and child versions (an override is always a child version), validating, publishing an exact validated version, and recording paper or simulated executions separately from publication. It applies optimistic concurrency (`expected_revision`) and `idempotency_key` on every state-changing call.
- **Shared ingestion operation** (normalize, validate, persist, dedupe) with two triggers:
  - An EventBridge Scheduler schedule in `America/New_York`. The time is configurable (09:00 or 09:30), with a 09:00 default.
  - On-demand invocation (agent tool, website, operator) that runs the same implementation.
  - Every result returns source timestamps, the retrieval timestamp, date coverage, quality flags and an immutable `input_snapshot_id`.
  - The exchange session calendar (holidays, early closes) is explicit. Completed daily observations are distinguished from intraday partial bars.
  - Provider capability (daily only vs intraday) is declared, not assumed.
  - **Provider and calendar (round 2 user decision 2026-10-07, OQ-5 and PQ-5 resolved):** a `yfinance` (Yahoo Finance) provider adapter behind the existing adapter interface for SPY daily completed OHLCV, adjusted close, dividends and splits, plus a session calendar built from the `exchange_calendars` library (XNYS). Both libraries are version-pinned. The adapter adds client-side rate limiting, retry with backoff, quality flags for empty or partial responses, and snapshot lineage with the library version and retrieval timestamp. `yfinance` is unofficial and Yahoo's terms are personal/research use, so retrieved market data is never committed to this public repo; fixtures stay synthetic. CI uses only the mock provider; an optional live-provider test is opt-in, rate-limited and asserts shape, not values.
- **Staged-output acceptance.** FinanceModel production workers write to a platform-owned staging prefix. The platform validates the output (schema, accounting reconciliation, constraints, coverage, lineage) before committing a plan version. Partial or failed outputs never become `validated`.
- **Excel import and export.** `.xlsx` only. Formulas are never evaluated and macros are never executed. Macro-enabled or legacy formats are rejected. The workbook is parsed into the same canonical plan-version contract, and the source file is retained as lineage. Export produces the round-trip template.
- **Read routes added by the cross-repo review:** portfolio read, plan head, paginated version list, bounded snapshot observation read and staged-output outcome read. These serve FinanceLambdasTool `get_plan`, `list_plan_versions` and `query_market_data`, and FinanceModel's staged-run outcome link. Snapshots carry an approval `status`; FinanceModel may read only `approved` snapshots.
- **Single API surface.** The website, Excel import, scheduler and MCP adapters (via FinanceLambdasTool) all go through the same plan and ingestion operations. There is no side door into storage or metadata.
- **Cost guardrails.** An AWS Budgets budget at the USD 50 project cap with alerts at 50%, 80% and 100% (plus a 100% forecast alert) and a deny action at 100%. The bootstrap writes the default, SSM-configurable category allocation (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5; user decision 2026-10-07) that other repos' pre-flight checks read. TypeSafe Jev spend is outside AWS and not counted. No always-on compute in phase 1.
- **The repo's own CodePipeline**, following the contract pipeline standard: source, build/test, beta, gamma, approval, prod/smoke. It is bootstrapped once from the user's existing authenticated AWS CLI session under the user's in-principle approval (2026-10-07; run only after the bootstrap IaC is implemented, with the exact stacks and a cost estimate shown first), reusing an existing AVAILABLE GitHub CodeConnection that a source-stage dry run verifies.
- **Initial dataset (user decision 2026-10-07).** S&P 500 exposure through a single daily series of a tracking ETF (for example SPY), kept as a separate dataset from the S&P 500 index level and from the constituent universe. Daily completed observations only in phases 1 and 2; no intraday. Provider: `yfinance` (RESOLVED 2026-10-07). Phase 1 deploys a deterministic fixture provider modelling that ETF series plus a provider mock; the `yfinance` adapter is built and tested here and is enabled per environment only by a phase 2 configuration.
- **Phase separation.** Phase 1 deployments (this change) use synthetic fixtures only, no real provider calls, no model compute and no live trading. The `yfinance` adapter and the XNYS calendar ship in this change but run against real Yahoo data only after an environment's configuration declares phase 2 (beta first). **Out of scope:** live trading, Coinbase, AgentCore payments, wallet spending, and automated rewriting of risk preferences.

## Capabilities

### New Capabilities

- `platform-storage`: per-environment buckets, encryption, access policy, immutability, retention and lifecycle for raw, curated, snapshot, plan, output and report artifacts.
- `plan-metadata-store`: authoritative metadata records, conditional writes and transactions, the idempotency store and the append-only audit events behind every platform state change.
- `plan-lifecycle-api`: the plan operations (portfolio, plan, version, override, validation, publication, execution, read-back with checksum) shared by the website, Excel, scheduler and MCP clients.
- `market-data-ingestion`: the shared normalize/validate/persist/dedupe ingestion operation, its scheduled and on-demand triggers, session-calendar and observation-kind semantics, provider capability declaration and snapshot results.
- `staged-output-acceptance`: the run-output staging area and the validation gate that turns model-worker output into a plan version, or rejects it.
- `excel-plan-import`: safe workbook import into the canonical plan contract, source-file lineage, and template export for round trips.
- `platform-cost-guardrails`: the project budget, notifications, the budget action, and the phase 1 no-always-on-compute rule for platform resources.
- `platform-pipeline`: this repository's CodePipeline/CodeBuild delivery of the platform through beta, gamma and prod per the contract pipeline standard.

### Modified Capabilities

None. `openspec/specs/` is empty, and this change does not alter the requirements of `establish-cross-repo-contracts`. It only references them.

## Impact

- **Code (future implementation):** a platform service (Python handlers, CDK stacks) in this repo, which consumes the contract package (`finplan-contracts`) built from `contracts/` at a pinned version.
- **APIs published:** `/finplan/<env>/financialplanning/api/plan-endpoint`, `/finplan/<env>/financialplanning/api/ingestion-endpoint`, `/finplan/<env>/financialplanning/config/run-staging-ref`, `/finplan/<env>/financialplanning/config/ingest-schedule` and the release manifest. Consumers are FinanceLambdasTool, FinanceModel (staging and snapshot reads) and FinanceAgent (indirectly).
- **AWS (us-east-2, once the bootstrap blockers clear):**
  - Serverless and on-demand only: S3, KMS, DynamoDB on-demand, Lambda, API Gateway, EventBridge Scheduler, AWS Budgets, SSM, CodePipeline and CodeBuild.
  - No GPU, endpoints or always-on compute.
  - This planning task creates nothing.
- **Dependencies:**
  - Contracts 1.x must be published first.
  - Deploying needs only the bootstrap, approved in principle on 2026-10-07: OQ-11 is resolved (existing CLI credentials) and OQ-2 is non-blocking (existing connection, source-stage dry run).
  - Real-data ingestion is no longer blocked: OQ-5 and PQ-5 are RESOLVED 2026-10-07 (`yfinance` provider, `exchange_calendars` XNYS). New Python dependencies: `yfinance` and `exchange_calendars`, pinned; the ingestion function may need a container-image Lambda if the dependency size exceeds the zip package limit.
  - OQ-1 is resolved: beta, gamma and prod share one account, isolated by naming, tags, permission boundaries and per-env resources. Non-synthetic portfolios stay out of phase 1 by phase scope.
- **Public-repo hygiene:** no account IDs, ARNs, bucket or role names, connection ARNs or secret values in any file. Everything resolves from configuration.
