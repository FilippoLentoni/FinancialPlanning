# Proposal

## Why

Four public repositories (FinancialPlanning, FinanceModel, FinanceLambdasTool, FinanceAgent) each get their own CodePipeline and promote independently through beta, gamma and prod, yet they must share plan state, identifiers, schemas and resource references. Without one authoritative contract reference before any implementation starts, repos would duplicate storage, redefine IDs, copy incompatible schemas, or let gamma and research compute reach production data. FinancialPlanning is the shared planning platform repository (decision recorded 2026-10-07), so it owns these contracts and every other repo pins them.

## What Changes

- Define a **dependency/ownership matrix**: every deployable resource (buckets, metadata tables, plan APIs, ingestion, scheduler, SageMaker job definitions, model registry, Lambdas, AgentCore Gateway and Runtime, pipelines, secrets) maps to exactly one owning repo, with named consumers and the reference mechanism each consumer uses.
- Define the **canonical identifier model and lifecycle** for `portfolio_id`, `plan_id`, `plan_version_id`, `input_snapshot_id`, `model_version`, `configuration_id`, `run_id`, `publication_id` and `execution_id`: formats, immutability, parent/child versioning for overrides, idempotency keys and optimistic concurrency.
- Define a **single versioned contract package** (JSON Schema, semver) published by FinancialPlanning and pinned by consumers, with error conventions (codes, `retryable`, execution outcome distinct from optimization status), shared synthetic fixtures, validators and conformance tests.
- Define a **release manifest and SSM parameter naming convention** per environment (beta, gamma, prod) for cross-repo references such as Lambda ARNs, API endpoints, contract versions and release IDs. No account-specific identifiers are written into any repo.
- Define **environment isolation** inside **one AWS account** (decided 2026-10-07): beta, gamma and prod live in the same account in us-east-2 and are isolated by naming, tags, IAM permission boundaries with environment-tag denies, and separate per-environment buckets, tables and endpoints. Gamma never touches prod data, research compute cannot mutate published state, and live-financial permissions stay separate from research and paper deployments. Multi-account is recorded only as a possible future migration.
- Define a **per-repo CI/CD standard**: source, build/test, beta deploy/tests, gamma deploy/tests, manual approval, prod deploy/smoke. The same immutable artifact is promoted and rollback versions are recorded. The one-time IaC bootstrap runs with the operator's existing authenticated AWS CLI session and creates the scoped pipeline, deploy and service roles; moving the human to a scoped/MFA role later is a recommendation, not a prerequisite. Pipelines reuse an existing AVAILABLE GitHub CodeConnection (referenced through SSM, never by ARN), verified by a source-stage dry run; a human extends the GitHub App installation only if that dry run fails.
- Define **integration order and compatibility rules**: platform, then model service, then tool wrappers, then agent/Gateway. Backward-compatible publishes go out before consumer updates, breaking changes need a versioned migration, and end-to-end validation requires compatible releases in the same environment.
- Record the **region decision** (us-east-2) with evidence, quota and cost notes, and an explicit open-questions/blocker list.
- Record the **cost model**: USD 50 is the total AWS budget for everything, split by a default, SSM-configurable category allocation (platform/serverless infra and storage 8, CPU research jobs 7, Bedrock explanation calls 5, GPU 25, reserve 5), enforced by pre-flight checks and an AWS Budgets budget with alerts at 50/80/100% and a deny action at 100%. Every GPU run still needs explicit user approval with a cost estimate. TypeSafe Jev usage is billed by TypeSafe (prepaid credits), tracked separately and not part of the USD 50.
- Record **provider and credential references**: the FinanceAgent explanation provider is Amazon Bedrock (IAM auth, no API key; model ID in SSM), replacing the OpenAI secret reference; the TypeSafe Jev API key is a pre-existing Secrets Manager secret named `finplan/shared/financemodel/jev-api-key`, owned (referenced) by FinanceModel and listed as an account-level shared resource.
- Define a **domain-adapter boundary** so that snapshot, experiment, job and explanation machinery can later serve non-finance (supply-chain) models.
- Separate **phase 1** (fixture-backed deployment, no real model compute, no live trading) from later full modeling. **Out of scope:** live trading, Coinbase, AgentCore payments, wallet spending and automated rewriting of risk preferences.
- This change does not deliver the platform implementation (storage, plan APIs, ingestion). A separate FinancialPlanning change covers that.

## Capabilities

### New Capabilities

- `cross-repo-ownership`: single-owner resource matrix, consumer reference mechanisms, integration order, and cross-repo compatibility and release rules.
- `platform-identifiers`: canonical ID formats, immutability, version lineage (override = child version), publication versus execution, idempotency and optimistic concurrency.
- `contract-schemas`: the versioned JSON Schema contract package, semver and pinning rules, error conventions, shared synthetic fixtures, validators and conformance tests.
- `environment-promotion`: environment isolation, account and role boundaries, the release manifest and SSM naming convention, the per-repo pipeline stages, immutable artifact promotion, rollback recording and bootstrap.
- `domain-adapter-boundary`: domain-neutral snapshot/experiment/job/explanation envelope with finance as the first registered domain adapter.

### Modified Capabilities

None. No specs exist yet in this repository.

## Impact

- **FinancialPlanning (this repo):** owns and publishes the contract package, the release-manifest schema and the SSM naming convention, and maintains the ownership matrix. The follow-up platform implementation change must conform to these specs.
- **FinanceModel, FinanceLambdasTool, FinanceAgent:** each pins a released contract-package version, reads cross-repo references only through the release manifest and SSM parameters, and follows the pipeline standard. Their OpenSpec changes reference this change as the source of truth.
- **AWS (us-east-2):** no resources are created by this change. It fixes naming, boundaries and the bootstrap procedure that later changes execute.
- **Public-repo hygiene:** account IDs, ARNs, bucket names, role names, connection ARNs and secret values stay out of every repo file. They are resolved at deploy time from configuration.
- **User decisions 2026-10-07 (design D11)** resolve OQ-1 (single account), OQ-3 (Bedrock explanation provider), OQ-4 (Jev identity), OQ-7 (budget allocation) and OQ-11 (bootstrap with existing credentials), make OQ-2 non-blocking, and partially resolve OQ-5 (initial instrument: S&P 500 tracking ETF daily series).
- **Round 2 user decisions 2026-10-07 (design D12)** resolve OQ-5 (market-data provider: the `yfinance` Python library behind the platform's provider adapter, with an `exchange_calendars` XNYS calendar; implemented by the platform change) and OQ-13 (explanation model: Claude Opus 5 through the US cross-region inference profile `us.anthropic.claude-opus-5`, in SSM; model-access enablement is a bootstrap step). The user approves the one-time pipeline bootstrap in principle: it runs only after the bootstrap IaC is implemented, and at run time the exact stacks and a cost estimate are shown before it runs under that approval. Nothing is deployed during spec work.
- **Still unresolved** (design.md, Open Questions): schedule time (OQ-6), `ml.g6.12xlarge` processing quota (OQ-8), collaborator access (OQ-9), website owner (OQ-10) and explanation-evidence job kinds (OQ-12).
- **Cross-repo review amendments (2026-10-07, design D10):** the contract gaps reported by the platform, FinanceModel, FinanceLambdasTool and FinanceAgent changes are folded in. That adds matrix rows, registered SSM keys, the 1.0.0 schema additions (staged-output manifest, tool catalog and tool schemas, caller block, job lifecycle fields, snapshot status, open vocabularies, Excel template), the proxied-call idempotency rule, Gateway-principal ordering, the account-level shared-resource rule and the planned phase 2 minors.
- **Platform verification fixes (contracts 0.2.0, design D13):**
  - the matrix covers every resource kind the platform synthesizes, so the platform's templates pass the ownership check with zero problems;
  - each repository gets a per-environment pipeline-role row;
  - the permission boundaries use valid per-service ARN patterns within IAM's size limit;
  - new plan lifecycle API route schemas and optional snapshot, plan and refresh-response fields;
  - new registered SSM keys for the final identity provider (decision 15a), the direct-test principal (15b) and the FinanceModel job roles;
  - two tooling fixes (the boundary renderer and the copied-`$id` YAML handling).

  One response loosening (`tools/refresh-market-data-response` without a snapshot on a non-session day) ships under the 0.x exemption.
