# Design

## Context

See proposal.md (Why) for motivation. Requirements are in `specs/` (cross-repo-ownership, platform-identifiers, contract-schemas, environment-promotion, domain-adapter-boundary). This document records how those requirements are met and which decisions are still open.

### Observed facts (read-only AWS/GitHub discovery, 2026-10-07)

- One AWS account is in use. The CLI caller during discovery was the **root principal** (acceptable for the one-time bootstrap per D11). All existing project resources are in **us-east-2**.
- The AgentCore control plane responds in us-east-2. No AgentCore runtimes or gateways exist yet, and there are no ECR repositories in us-east-2.
- A previous Qwen3.6-27B deployment exists, owned by a **separate project's CloudFormation stack**. It is not a hosted endpoint. It runs offline vLLM inside SageMaker **Training Jobs** as a batch runner on `ml.g6.12xlarge` (4x L4). Weights (Hugging Face `Qwen/Qwen3.6-27B`, Apache-2.0, pinned revision) are staged once to S3 with a SHA-256 manifest. The last Qwen run took 4754 s. No live endpoints, models or endpoint configs exist in any region. These repos **must not adopt or modify** that stack. FinanceModel may reuse the pattern.
- SageMaker quotas in us-east-2 (partial list): `ml.g6.12xlarge` training 1, **processing 0**, endpoint 1. `ml.g5.xlarge` training 1, processing 0, endpoint 2. `ml.m5.xlarge` training 15, processing 1.
- Six GitHub CodeConnections exist in us-east-2 and all are AVAILABLE. Coverage of the four repos is verified at bootstrap by a source-stage dry run (D11, OQ-2).
- Secrets Manager holds no OpenAI secret. None is needed: the explanation provider is Amazon Bedrock with IAM auth (D11, OQ-3).
- The user stored the TypeSafe Jev API key in Secrets Manager (us-east-2) under the name `finplan/shared/financemodel/jev-api-key` (user decision 2026-10-07). Repo files carry only that name or an SSM pointer to it, never the value.
- The four GitHub repos are public, empty and on branch `main`. FinancialPlanning is the platform repo.
- TypeSafe Jev: identity resolved by the user from the vendor documentation and a live read-only model-list call (D11, OQ-4). It is an external HTTPS API billed by TypeSafe, not an AWS resource.

### Assumptions (not verified; revisit when the related open question closes)

- A1. ~~Assumption~~ **Decided 2026-10-07:** beta, gamma and prod share one account and are isolated by naming, tags and IAM (see D5; OQ-1 resolved).
- A2. Repos use Python for services and models. The contract package therefore ships Python and TypeScript validators (TypeScript covers any web/CDK consumers).
- A3. AWS CDK (CloudFormation) is the IaC tool for all four repos.
- A4. AgentCore Runtime and Gateway, SageMaker Processing/Training, EventBridge Scheduler, DynamoDB, S3, KMS, SSM, CodePipeline V2, CodeBuild and CodeArtifact are all usable in us-east-2. AgentCore was observed responding there. The others are assumed and will be confirmed by bootstrap pre-checks.

## Goals / Non-Goals

**Goals:**
- Fix ownership, identifiers, contract packaging, naming and promotion rules before any implementation, so the four repos can be built in parallel without duplicating resources or schemas.
- Make every rule mechanically checkable (conformance, ownership and identifier-leak checks in each build stage).

**Non-Goals:**
- Implementing platform storage, plan APIs, ingestion or scheduler. A separate FinancialPlanning change covers these.
- Implementing the market-data provider or choosing model sizing. The initial instrument, the provider (`yfinance`, D12) and the default budget allocation are recorded as user decisions (D11, D12) but are implemented by the platform and consumer changes.
- Live trading, Coinbase, AgentCore payments, wallet spending, and automated rewriting of risk preferences.

## Decisions

### D1. Ownership matrix (authoritative)

| Resource | Owner | Consumers | How consumers reference it |
|---|---|---|---|
| Raw/curated input buckets, immutable snapshot artifact bucket | FinancialPlanning | FinanceModel (read approved snapshots) | Trusted artifact refs from the platform API, plus a read grant to the per-env FinanceModel job role. Bucket names are only in `/finplan/<env>/financialplanning/config/*` |
| Plan/output/report artifact buckets | FinancialPlanning | website, FinanceLambdasTool, FinanceAgent (indirectly) | Platform API returns trusted artifact refs or presigned downloads. No raw keys |
| Run-output staging area | FinancialPlanning (bucket/prefix, validation) | FinanceModel production workers (write-only) | `/finplan/<env>/financialplanning/config/run-staging-ref` plus an IAM grant to the job role |
| Platform KMS keys, lifecycle and retention policies | FinancialPlanning | n/a (granted via key policy) | Key policy grants, per env |
| Metadata tables (portfolio, plan, plan version, publication, execution, snapshot catalog, staged-output outcomes, idempotency, audit events) | FinancialPlanning | none directly | Only through the plan API |
| Metadata sweeper (scheduled housekeeping of the metadata tables; D13) | FinancialPlanning | none | Internal |
| Plan lifecycle API (incl. Excel import, publish, paper execution record) | FinancialPlanning | FinanceLambdasTool, website, scheduled workflows | `/finplan/<env>/financialplanning/api/plan-endpoint` plus IAM (SigV4) |
| Ingestion service (normalize/validate/persist/dedupe) | FinancialPlanning | FinanceLambdasTool `refresh_market_data`, scheduler | `/finplan/<env>/financialplanning/api/ingestion-endpoint` |
| Daily EventBridge Scheduler (America/New_York) | FinancialPlanning | n/a | Time comes from `/finplan/<env>/financialplanning/config/ingest-schedule` (OQ-6) |
| Contract package (schemas, fixtures, validators, conformance suite, release-manifest schema, SSM convention) | FinancialPlanning | all repos | Pinned package version plus digest |
| Project AWS Budgets budget (account-level, USD 50 total ceiling, alerts 50/80/100%), budget action deny policy at 100%, budget-state writer Lambda, default category allocation | FinancialPlanning (tooling stack, `shared`) | all repos (their roles are named in the action; FinanceModel, FinanceAgent and the platform read `budget-state` and the allocation) | Ceiling `/finplan/shared/financialplanning/config/cost-ceiling-usd`; allocation `/finplan/shared/financialplanning/config/budget-allocation`; state `/finplan/shared/financialplanning/config/budget-state`; role names `/finplan/<env>/<repo>/config/budget-enforced-role-names` (D10, D11) |
| Cost-allocation tag keys | FinancialPlanning (contract package) | all repos tag resources | Tag keys defined in the contract package (D10) |
| Permission-boundary managed policies (`finplan-<env>-permission-boundary`, `finplan-<env>-research-permission-boundary`, `finplan-shared-permission-boundary`; D5, D13) | FinancialPlanning (tooling stack, `shared`, pipeline tooling) | every role any pipeline creates | Policy names from the contract package; ARNs built at deploy time from pseudo parameters |
| Research workspace storage, evaluation datasets | FinanceModel | none outside FinanceModel | n/a |
| SageMaker job definitions, containers, ECR repos, SageMaker Pipelines | FinanceModel | FinanceLambdasTool (via job interface); the platform names the job-execution role in its snapshot read and staging write grants | `/finplan/<env>/financemodel/job/*`; job-execution role at `/finplan/<env>/financemodel/job/job-role-ref` (D13) |
| Job submission/status/result interface (mints `run_id`) | FinanceModel | FinanceLambdasTool, platform (production runs) | `/finplan/<env>/financemodel/api/job-endpoint` |
| Model artifacts and model registry (mints `model_version`) | FinanceModel | platform (lineage), FinanceLambdasTool | `/finplan/<env>/financemodel/model/registry-ref` |
| Job control plane: job API (API Gateway + Lambda), run/event/idempotency/queue/lease table, SageMaker state-change rules, dispatcher schedule, approver role, orphan sweeper | FinanceModel | FinanceLambdasTool, platform (registry/lineage lookup) | `/finplan/<env>/financemodel/api/job-endpoint`; IAM grants to the consumers' published role references; job API handler role at `/finplan/<env>/financemodel/job/job-api-role-ref` (may read `GET /v1/staged-outputs/*`; D13) |
| Self-hosted Qwen3.6-27B serving lifecycle, concurrency lease, FinanceModel-staged weight copy (inside research storage) | FinanceModel | FinanceModel agent-swarm strategy | Internal. The existing stack from the other project is **external, not adopted** |
| Promotion-criteria store and explanation-evidence artifacts (FinanceModel run outputs) | FinanceModel | FinanceAgent (evidence via tool results) | Trusted artifact references returned by `get_job_result`, resolved by FinanceModel |
| Account-level SageMaker instance quotas | **external** (AWS account limit, not a resource) | FinanceModel | Per-environment lease plus quota-aware requeue (D10); no shared lease resource |
| MCP adapter Lambdas, their aliases and the three role classes (`reader`, `submitter`, `plan-writer`) | FinanceLambdasTool | FinanceAgent Gateway, the direct-test principal; platform and FinanceModel name the roles in resource policies | `/finplan/<env>/financelambdastool/lambda/<tool>-arn`, `/finplan/<env>/financelambdastool/lambda/role-<class>-arn`; the single direct-test principal per environment (the project owner, user decision 15b) at `/finplan/<env>/financelambdastool/config/direct-test-principal-name` |
| Tool catalog (published per environment) | FinanceLambdasTool | FinanceAgent Gateway registration | `/finplan/<env>/financelambdastool/contract/tool-catalog` (manifest `outputs` key `tool-catalog`) |
| AgentCore Runtime, Gateway, Gateway targets/tool registration, Gateway policy engine, Gateway interceptor Lambda, AgentCore Memory resource, agent skills, FinanceAgent ECR repository (account-level, immutable tags) | FinanceAgent | Claude Code/Codex via MCP, website/CLI | `/finplan/<env>/financeagent/agent/*` |
| Per-environment Gateway service role (created by the FinanceAgent bootstrap, before FinanceLambdasTool's Gateway-facing release) | FinanceAgent | FinanceLambdasTool (invoke grant) | `/finplan/<env>/financeagent/agent/gateway-principal-ref` |
| OIDC identity provider for Gateway/Runtime users, roles and CI clients: one Amazon Cognito user pool per environment | FinanceAgent (**final**, user decision 15a of 2026-10-07, FA-OQ-1 resolved; the website must reuse it, OQ-10) | Gateway, Runtime, website, Claude Code/Codex | `/finplan/<env>/financeagent/agent/user-pool-ref`, `/finplan/<env>/financeagent/agent/authorizer-metadata-ref`; CI client secret name at `/finplan/<env>/financeagent/secret-ref/ci-test-client` |
| Explanation-provider configuration: provider selection (default `bedrock`) and Bedrock model ID; no API-key secret (IAM auth) | FinanceAgent | FinanceAgent runtime only | `/finplan/<env>/financeagent/config/explanation-provider`, `/finplan/<env>/financeagent/config/explanation-model-id` (D11; OQ-3 resolved, model ID is OQ-13) |
| Amazon Bedrock model access for the explanation model (Claude Opus 5 via the US cross-region inference profile `us.anthropic.claude-opus-5`, D12) | **external** (account-level model access, enabled by a human/console step or an API call at bootstrap) | FinanceAgent runtime role (`bedrock:InvokeModel*` scoped to the configured inference profile and its underlying foundation-model ARNs in the US regions the profile routes to) | Model ID from the FinanceAgent config parameter above |
| Market-data provider source (Yahoo Finance through the unofficial `yfinance` Python library, D12) | **external** (third-party service, no credential, no AWS resource) | FinancialPlanning ingestion only (provider adapter); no other repo calls it | Pinned library version in the platform's dependency lock; provider selection in `/finplan/<env>/financialplanning/config/*`; no secret |
| TypeSafe Jev API key secret (pre-existing, account-level; value set and rotated by the user, never created, read or written by IaC) | FinanceModel | FinanceModel Jev strategy job roles only (per environment, read-only on this one secret) | Secret name `finplan/shared/financemodel/jev-api-key`, published at `/finplan/shared/financemodel/secret-ref/jev-api-key` (D11) |
| CodePipeline, CodeBuild projects, pipeline artifact bucket and its policy, account-level pipeline/build/deploy roles | each repo for itself (`shared`) | n/a | n/a |
| Per-environment deploy, CloudFormation execution and stage roles (declared in the account-level pipeline stack, tagged with their environment, environment boundary; D13) | each repo for itself (`environment`) | n/a | n/a |
| GitHub CodeConnection (an existing AVAILABLE connection in us-east-2, reused) | **external** (account-level, human-managed) | each pipeline | `/finplan/shared/<repo>/config/codeconnection-ref`, written by the bootstrap and verified by a source-stage dry run (D11; OQ-2 non-blocking) |
| CodeArtifact domain/repository for the contract package | FinancialPlanning | all build stages | `/finplan/shared/financialplanning/contract/registry-ref` |
| Website (plan UI) | **OQ-10**, provisionally FinancialPlanning | users | Uses the plan API only |

External or pre-existing resources are never owned by any repo. They are referenced only through configuration.

**CDK-generated helper resources (task 1.5).** CDK synthesizes resources that no construct in our code names directly: the `AWS::IAM::Policy` it attaches to a role, `AWS::Lambda::Permission` grants, `AWS::CDK::Metadata`, custom-resource provider functions and roles, and log-retention helpers. The ownership check never ignores them. A helper without its own `logical-role` is either attributed to the owner of its parent construct (its type is in the matrix's `cdk_generated_helpers.parent_attributed_types` and every template resource it references is owned by the checked repository), or matched by an entry of the explicit, reviewed `cdk_generated_helpers.allow_list` in `matrix.yaml`. Each entry names the resource types, a logical-ID or `aws:cdk:path` pattern, the reason and the review record. A helper whose parent belongs to another repository, that has no parent in the template, or that matches no entry fails OWN-01, and every attribution is listed in the check's report. Rejected: a fixed list of ignored types, because it would let a policy attached to another repository's role, or an unreviewed provider, pass unseen.

### D2. Identifier model

- **ULID with type prefix** (`pf_`, `pl_`, `pv_`, `snap_`, `mv_`, `run_`, `pub_`, `exe_`). ULIDs sort by time, need no coordination, and the prefix lets validators catch IDs passed in the wrong field. UUIDv4 was rejected because it is not sortable and carries no type signal. Database sequences were rejected because they need a central allocator.
- **Content-addressed `configuration_id`** (RFC 8785 JCS plus SHA-256). Identical configurations deduplicate naturally, and "no-effect" configuration changes can be detected. Snapshots use a ULID plus a separate manifest checksum, because the same bytes ingested at two retrieval times are distinct observations.
- **Lifecycle.** The head of each lineage tree is a plan's current version.

```
portfolio ─┬─ plan ─┬─ plan_version (root; origin model_run | manual_override | excel_import)
           │        │     └─ child plan_version (override; parent_plan_version_id) ...
           │        ├─ publication ──► exactly one validated plan_version (+checksum)
           │        │     └─ execution (paper|simulated, phase 1) ──► publication
           │        └─ head pointer {current_version_id, revision}
input_snapshot ◄── plan_version ──► configuration_id, model_version, run_id
```

- **Atomicity.** Creating a version and moving the head (and its idempotency record) is one conditional metadata transaction, assumed to be a DynamoDB `TransactWriteItems` with a condition on `revision`. Immutable artifacts are written to S3 **before** the metadata commit, under a key derived from the new ID. An orphaned artifact without a metadata row is never visible and is garbage-collected. S3 object versioning is enabled for protection only and is never used as business identity.
- **Idempotency.** The record key is `(principal, environment, operation, idempotency_key)`. It stores the request hash (JCS SHA-256) and the original response, and is retained for at least 7 days.
- **Delegated minting.** FinanceModel mints `run_id` and `model_version` because it owns the job interface and the registry. The platform records them as foreign references and verifies them against the FinanceModel published reference when committing staged output.

### D3. Contract package

- Source lives in this repo at `contracts/` (schemas in JSON Schema 2020-12, `$id` = `https://contracts.finplan.invalid/<domain|core>/v<major>/<name>.json`). The `.invalid` host keeps `$id`s as identifiers only, never fetched URLs.
- Build outputs from one version number: a Python distribution (`finplan-contracts`), an npm package (`@finplan/contracts`) and a raw schema/fixture tarball. Each has a SHA-256 digest recorded in the release manifest.
- **Registry: CodeArtifact (recommended).** Builds authenticate with IAM, so no external registry credentials are needed in public repos. A public registry (PyPI/npm) was rejected for now because it needs publisher credentials and makes pre-1.0 churn public. GitHub release assets were also rejected: there is no git/gh tooling on the build path, and the GitHub connection is unverified. External collaborators without AWS access are covered by OQ-9.
- The package also ships `finplan-conformance` checks used in every repo's build stage: schema conformance, copied-`$id` detection, ownership-matrix check against synthesized templates, identifier/secret leak scan, live-permission policy scan and the domain-neutrality check.
- Compatibility gate: each build diffs the schemas against the previous published version. Any breaking diff under a non-major bump fails.
- Producers declare `served_contract_majors` in their release manifest. Consumers declare their pinned `contract_version`. The end-to-end gate compares the two.

### D4. Release manifest and SSM convention

- Path: `/finplan/<environment>/<repo>/<category>/<name>`, where environment ∈ {beta, gamma, prod}. Account-level, environment-independent values use the reserved segment `shared` in place of the environment (`/finplan/shared/<repo>/<category>/<name>`), written only by the authenticated bootstrap or by the contract-package publish step. Names are lowercase kebab-case.
- Each deploy writes `/finplan/<env>/<repo>/release/manifest` (JSON validated against the manifest schema; an advanced-tier parameter if it exceeds 4 KB) and `/finplan/<env>/<repo>/release/current-release-id`. SSM parameter history plus a copy in the repo's pipeline artifact store form the rollback ledger.
- `release_id` format: `rel_<ULID>`, assigned at the build stage and shared by all environments for that build.
- Manifest fields: as in the spec, plus `served_contract_majors`, `approved_by` and `approved_at` (prod), and `rolled_back_from`.
- Each pipeline role's IAM policy allows `ssm:PutParameter` only on `/finplan/*/<own-repo>/*`. Read access covers `/finplan/<same-env>/*` and `/finplan/shared/*`.
- **Runtime writer exception (review amendment, 2026-10-07).** Exactly one runtime principal may write under `shared`: the FinancialPlanning budget-state writer Lambda, and only `/finplan/shared/financialplanning/config/budget-state`. It stays inside FinancialPlanning's own repo segment, so single ownership holds. Every other `shared` value is written only by the bootstrap or the contract-publish step.
- **Registered cross-repo keys added by the review** (each written only by its owning repo):
  - `/finplan/<env>/<repo>/config/budget-enforced-role-names`: the role names each repo asks the budget action to deny (FinanceModel job-submission/dispatcher roles, FinanceLambdasTool `submitter`, platform ingestion and deploy roles, and the FinanceAgent runtime role so that Bedrock invocations stop at 100%, D11).
  - ~~`/finplan/<env>/<repo>/config/budget-allocation`~~ **RETIRED 2026-10-07** (D11): the user will not split the budget per repo. It is replaced by the single category allocation `/finplan/shared/financialplanning/config/budget-allocation`. Consumers that planned to read the per-repo key read their category from the shared map instead.
  - `/finplan/shared/financialplanning/config/budget-allocation` (D11): JSON map of category → USD. Defaults written by the bootstrap: `platform_infra` 8 (platform/serverless infrastructure and storage, all repos), `cpu_research` 7 (FinanceModel CPU jobs), `bedrock_explanations` 5 (FinanceAgent Bedrock calls), `gpu` 25 (FinanceModel Qwen3.6-27B / RL GPU jobs), `reserve` 5. The categories must sum to at most the ceiling. The user may change values; the category names are registered in the contract package.
  - `/finplan/<env>/financeagent/config/explanation-provider` and `/finplan/<env>/financeagent/config/explanation-model-id` (D11): the explanation provider (default `bedrock`) and the Bedrock model ID or inference-profile ID for that environment.
  - `/finplan/shared/financemodel/secret-ref/jev-api-key` (D11): holds the secret **name** `finplan/shared/financemodel/jev-api-key`, written by the FinanceModel bootstrap.
  - ~~`/finplan/<env>/financeagent/secret-ref/openai-api-key`~~ **RETIRED 2026-10-07**: no OpenAI secret is needed. A future optional OpenAI adapter would register its own key in a later change.
  - `/finplan/<env>/financeagent/agent/gateway-principal-ref`: the per-environment Gateway service role reference that FinanceLambdasTool grants invoke to.
  - `/finplan/<env>/financelambdastool/contract/tool-catalog`, recorded in the FinanceLambdasTool manifest under the `outputs` key `tool-catalog`.
- **Registered keys added in contracts 0.2.0 (D13; user decisions 15a and 15b of 2026-10-07)**, each written only by its owning repo:
  - `/finplan/<env>/financeagent/agent/user-pool-ref`: the environment's Cognito user pool (the identity provider, final).
  - `/finplan/<env>/financeagent/agent/authorizer-metadata-ref`: the user pool's OIDC discovery metadata used by Gateway and Runtime authorizers (already registered; now tied to the final provider).
  - `/finplan/<env>/financeagent/secret-ref/ci-test-client`: the CI test client secret **name** (already registered).
  - `/finplan/<env>/financelambdastool/config/direct-test-principal-name`: the single direct-test principal (the project owner) granted tool invoke; written by the FinanceLambdasTool pipeline or bootstrap.
  - `/finplan/<env>/financemodel/job/job-role-ref` and `/finplan/<env>/financemodel/job/job-api-role-ref`: the FinanceModel job-execution and job-API handler roles named in the platform's grants.

### D5. Environment isolation in a single account (decided 2026-10-07)

- **Decision (OQ-1 resolved):** beta, gamma and prod all live in **one AWS account** in us-east-2. Multi-account (AWS Organizations, one account per environment) is no longer a recommendation; it is recorded only as a possible future migration. All naming already includes the environment, so such a migration would change only environment configuration (target account per env), not contracts.
- **Isolation mechanisms:**
  - naming: every resource name and SSM path carries the environment segment;
  - tags: every resource carries the `environment` tag (`beta|gamma|prod|shared`);
  - IAM permission boundaries attached to every role the pipelines create, with an explicit deny on any action against resources tagged with another environment or named under another environment's prefix (one ARN pattern per service with pseudo parameters, so IAM accepts it, and each boundary within IAM's managed-policy size limit; D13);
  - separate per-environment buckets, tables, API endpoints, Lambdas, Gateway/Runtime instances and job queues.
- Research roles: read-only access to the snapshot prefix of approved snapshots and write access to the staging prefix. An explicit deny covers platform metadata tables and the publication/execution APIs.
- Live-financial separation: a permission-boundary deny list (trading/brokerage secrets, payments, wallet actions) is attached to every role these pipelines create. The live permission scan in D3 enforces it statically.
- Account-level (`shared`) items are the only exception to per-environment separation (spec environment-promotion, "Account-level shared resources"); they hold no environment data. The Jev API key is one such item: all environments' FinanceModel Jev job roles read the same secret, so its TypeSafe usage is not separated per environment (tracked by the run cost records in FinanceModel).
- Prod smoke tests use a dedicated synthetic portfolio flagged `synthetic: true` and never mutate real plans. Phase 1 portfolios are synthetic by phase scope (D7), not because of the account boundary.

### D6. Pipeline standard (per repo)

- CodePipeline V2 + CodeBuild. Stages: Source (CodeConnection, `main`) → Build/Test (unit, contract conformance, scans, `cdk synth`, artifact digest, `release_id`) → Beta (deploy + integration-beta tests) → Gamma (deploy + gamma tests) → Manual approval → Prod (deploy + smoke).
- Stages after the build stage consume the build stage's artifacts only (cloud assembly plus container digests). They never run `cdk synth` against a fresh checkout.
- Bootstrap (D11): the one-time bootstrap runs with the operator's existing authenticated AWS CLI session (no pre-existing scoped human role is required, and a root caller is not refused). It checks the STS account against local untracked configuration and the region, writes the CodeConnection reference to SSM, creates the scoped pipeline, deploy and service roles, and runs a **source-stage dry run** (a pipeline execution that stops after the Source stage) to prove repo access before any deploy stage is enabled. After bootstrap, only those scoped roles deploy. The bootstrap prints a recommendation to move the human operator to a scoped/MFA role later; this is not a blocker.
- Rollback: a pipeline parameter `rollback_to_release_id` redeploys the stored assembly of a recorded release. CloudFormation automatic rollback handles failed deploys. Data is never rolled back: records are immutable, and schema changes are expand-then-contract.
- Real-provider tests are optional, rate-limited, never in prod smoke, and never assert changing market values.

### D7. Integration order and phases

- **Phase 1 (fixture-backed):** contracts 1.0.0 → platform (plan API on fixtures, snapshot catalog, idempotency, publication, paper-execution records) → FinanceModel job interface with a CPU-only stub job returning fixture results → FinanceLambdasTool fixture-backed tools → FinanceAgent Runtime + Gateway. No GPU compute, no live provider dependency, no live trading.
- **Phase 2:** real daily ingestion of the initial instrument (an S&P 500 tracking ETF daily series, D11) through the `yfinance` provider adapter (D12), classical baselines/optimizers and backtests on CPU jobs, production-run staging/validation, Bedrock-backed explanations, and the TypeSafe Jev strategy **enabled behind explicit user approval** (FinanceModel; D11, OQ-4 resolved). Daily completed observations only; no intraday.
- **Phase 3:** RL training (Training Jobs) and the Qwen3.6-27B agent swarm (GPU; OQ-8 and the concurrency lease), each GPU run within the `gpu` allocation and only after explicit user approval with a cost estimate.
- **Later, separate changes:** live trading, Coinbase, AgentCore payments.

### D8. Region: us-east-2

Evidence: all existing project resources are there, and the AgentCore control plane responds there. The vLLM DLC image used by the prior Qwen runs is available in us-east-2, the GPU quotas above exist there, and the CodeConnections are there. The choice was not made from the user's timezone alone.
- Quota notes: `ml.g6.12xlarge` processing quota is 0, so a GPU Processing-Job benchmark needs an increase (OQ-8). The alternatives are the Training-Job batch pattern (quota 1) or a short-lived endpoint (quota 1). With a training quota of 1, at most one Qwen job can run at a time, so a lease is required.
- Cost notes: no prices are recorded here. They must come from current AWS pricing at decision time. Cost drivers include GPU instance-hours for SageMaker jobs/endpoints (billed as managed SageMaker, not raw EC2), S3 storage of about 52 GiB of staged weights, AgentCore usage, CodeBuild minutes, and Bedrock model invocations for explanations (inside the `bedrock_explanations` allocation). TypeSafe Jev calls are billed by TypeSafe from prepaid credits, outside AWS and outside the USD 50. Observed reference: the last Qwen batch run lasted 4754 s on one `ml.g6.12xlarge`.

### D9. Domain-adapter boundary

The core envelope (`core/v1/*`) carries IDs, lineage, timestamps, checksums, status and errors. `finance/v1/*` carries portfolios, instruments, allocations, constraints, fees, market-data observations and explanation evidence. Snapshot catalog, job interface and explanation evidence storage operate only on the envelope plus an opaque, adapter-validated payload. A future `supply_chain` adapter is a new contract minor (new schemas), not a core change.

### D10. Cross-repo review amendments (2026-10-07)

A cross-repo review of all seven planning changes found gaps that consumers recorded as CONTRACT GAP. They are resolved here; the consumers' designs point back to this section.

- **Contract 1.0.0 additions (needed for phase 1):**
  - `core/v1/staged-output-manifest.json` plus a `finance/v1` payload. The manifest is the completion marker: the worker writes it **last** under `<run-staging-ref>/<run_id>/`. It lists every file with its SHA-256 and carries `run_id`, `model_version`, `configuration_id`, `input_snapshot_id`, `plan_id`, optional `parent_plan_version_id`, `completion_status`, `solution_status`, `evaluator_version` and `contract_version`. There is no separate marker file.
  - Staging handoff: an **explicit platform call** (`POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept`), never an S3 event, made by a platform-side caller (the scheduled workflow, an operator or the website path). FinanceLambdasTool exposes no accept tool in phase 1. The outcome is readable at `GET /v1/staged-outputs/{run_id}`, which the FinanceModel job API role may call.
  - Snapshot `status` enum (`committed`, `approved`, `expired`) plus `approval_rule_version`. Only `approved` snapshots may be read by FinanceModel. Phase 1 fixture snapshots are approved automatically when validation raises no blocking quality flag.
  - `quality_flags` as an open enum: `no_session`, `missing_sessions`, `rejected_records`, `source_revised`, `contains_intraday_partial`, `finality_inferred`, `no_new_observations`, `stale_source`.
  - Trusted-reference `kind` as an open enum: `snapshot_manifest`, `snapshot_payload`, `plan_content`, `plan_export`, `excel_source`, `staged_output_manifest`, `validation_report`, `import_report`, `research_dataset`, `run_artifact`, `explanation_evidence`. A research dataset has no new platform identifier: it is a `research_dataset` reference whose `artifact_id` is FinanceModel-issued and whose checksum is the dataset manifest checksum.
  - `finance/v1/excel-plan-template.json` (template `xlsx-plan-v1`) and `core/v1/import-report.json`.
  - Job schemas: the lifecycle `state` enum (`awaiting_approval`, `queued`, `starting`, `running`, `stopping`, plus the terminal `completion_status` values), `purpose` (`research`, `tuning`, `holdout_evaluation`, `production_candidate`), `dry_run`, and the cost-estimate block (`estimated_usd_upper_bound`, `price_retrieved_at`, `remaining_allocation_usd`, plus `budget_category` from D11).
  - `core/v1/tool-catalog.json` and one request/response schema pair per tool (`core/v1/tools/*` for domain-neutral tools, `finance/v1/tools/*` for finance payloads). Each catalog entry carries the tool name, schema `$id`s, `state_changing`, and the role class.
  - `core/v1/caller.json`: an on-behalf-of caller block (`subject_hash`, `roles`, `channel` = `hosted_agent|direct_mcp|ci_test|direct_test|website|scheduler|operator`, `correlation_id`). It is set only by a trusted hop (the FinanceAgent Gateway interceptor, or a tool's resolved invocation source), travels outside the hashed request body (a transport header), so it never changes idempotency request hashes, and is recorded in producer audit events. It never grants authority by itself.
  - Cost-allocation tag keys: `project` (value `finplan`), `owner-repo`, `environment` (`beta|gamma|prod|shared`), `logical-role`, and `run-id` on SageMaker jobs.
- **Idempotency for proxied calls.** When a tool adapter proxies a state-changing call, the producer's idempotency scope principal is the adapter's role. To keep different end callers from colliding, the adapter MUST forward the derived key `lt_` + lowercase hex SHA-256(`caller_identity|env|tool|idempotency_key`) and a deterministic body (spec platform-identifiers).
- **Gateway principal ordering.** The FinanceAgent bootstrap creates the per-environment Gateway service role and publishes `gateway-principal-ref` before FinanceLambdasTool's first Gateway-facing release. Until it exists, FinanceLambdasTool deploys with direct-test grants only. A later configuration-only redeploy of the same release adds the Gateway grant (spec cross-repo-ownership).
- **Account-level quotas and resources.** Account-level SageMaker quotas are an external limit, not a resource, so no shared cross-environment lease is created. FinanceModel uses per-environment leases with quota-aware requeue. The only account-level (`shared`) resources are pipeline tooling (including the permission-boundary policies, D13), the project budget and its action, the contract registry, and ECR repositories with immutable digest-addressed images. Each has exactly one owner and holds no environment data (spec environment-promotion, "Account-level shared resources").
- **Cost ceiling.** The user fixed the total AWS ceiling at **USD 50** (2026-10-07), stored at `/finplan/shared/financialplanning/config/cost-ceiling-usd`. The allocation is decided in D11 (OQ-7 resolved). Per-call tool limits (LT-OQ-3) are set inside the categories by the owning repos.
- **Planned contract minors for phase 2 (not in 1.0.0):**
  - Explanation evidence: one `finance/v1/explanation-evidence/*` schema per evidence kind, matching FinanceAgent `add-explanation-workflows` design E2: `performance_reconciliation`, `gap_decomposition`, `forecast_position`, `data_quality_report`, `difference_inventory`, `controlled_resolve_set`, `grouped_shapley`, `sensitivity_sweep`. Each carries the identifiers it was computed from, the evaluator code version, seeds, declared tolerances and every check result. Explanation envelope fields come in the same minor. Evidence kinds are payload shapes, not job types: the FinanceModel experiment types that produce them (FinanceAgent design E1: `performance_decomposition`, `controlled_resolve`, `grouped_shapley`, `sensitivity_sweep`) come from a follow-up FinanceModel change (OQ-12).
  - Plan-version lineage reproducibility fields, all optional: `solver`, `solver_version`, `tolerance`, `seed`, `code_version`.
  - Finance result flags: per-period `leakage_risk` and `out_of_configuration`.
  - Portfolio-policy (risk preferences and constraints) schema. Agents can read it but never rewrite it.

### D11. User decisions (2026-10-07, after the first spec pass)

Source: the user's decision record of 2026-10-07. These override earlier assumptions in this document.

- **Single AWS account (OQ-1 resolved).** See D5.
- **Bootstrap credentials (OQ-11 resolved, non-blocking).** The one-time bootstrap uses the user's existing authenticated AWS CLI session. No pre-existing scoped human role is required and the "refuse root principal" rule is dropped. Least privilege for automation is unchanged: the bootstrap creates the scoped pipeline, deploy and service roles each pipeline needs. Recommendation (not a blocker): move the human operator to a scoped/MFA role later.
- **GitHub (OQ-2 non-blocking).** Repos `FilippoLentoni/{FinancialPlanning,FinanceModel,FinanceLambdasTool,FinanceAgent}` (public, `main`). Each pipeline reuses an existing AVAILABLE GitHub CodeConnection in us-east-2, referenced through `/finplan/shared/<repo>/config/codeconnection-ref` and never by ARN in repo files. The bootstrap proves access with a source-stage dry run; only if it fails does the user extend the GitHub App installation to the repo.
- **Budget (OQ-7 resolved).** USD 50 is the total AWS budget for everything and is not split per repo. Default category allocation (configurable in `/finplan/shared/financialplanning/config/budget-allocation`): `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5. Enforcement: pre-flight checks by the paying repo against its category, plus the AWS Budgets budget with alerts at 50%, 80% and 100% and a deny action at 100%. Every GPU run still needs explicit user approval with a cost estimate. Jev usage is billed by TypeSafe (prepaid credits), tracked separately and not part of the USD 50.
- **Explanation LLM (OQ-3 resolved).** FinanceAgent uses Amazon Bedrock as the default explanation provider (IAM auth, no API key, billed to AWS inside `bedrock_explanations`). The model ID is SSM configuration and must be a model available in us-east-2, verified read-only at decision time (OQ-13); model access may need enabling by a human/console step or an API call at bootstrap. The provider stays a configurable interface; OpenAI becomes an optional future adapter and needs no secret now. Qwen3.6-27B remains a separate benchmark strategy, not the explanation provider.
- **Market data (OQ-5 partially resolved).** Initial instrument: S&P 500 exposure through a tracking-ETF daily series (for example SPY), kept distinct from the index level and the constituent universe. Daily completed observations only in phases 1 and 2; no intraday. The provider is still open (prefer a free or low-cost daily source with clear terms). Phase 1 uses the fixture and mock providers.
- **TypeSafe Jev (OQ-4 resolved).** External HTTPS decision API (TypeSafe AI "System One"), bearer-token auth, typed `choice` questions with per-option probabilities. Contract-level consequences only: the API key is the pre-existing secret `finplan/shared/financemodel/jev-api-key` (owner FinanceModel, account-level shared, D1); FinanceModel records the exact `model` value returned per response for reproducibility; the strategy moves to phase 2 behind explicit user approval; its spend is outside the AWS budget. Strategy details (one `{buy, hold, sell}` choice question per instrument and decision date, deterministic sizing policy, calibration on own splits, pretraining-leakage flag) belong to the FinanceModel changes.
- **Unchanged:** no live trading, no deployment during spec work, and public repos contain no account IDs, ARNs, bucket names or secret values.

### D12. Round 2 user decisions (2026-10-07)

Source: the "Round 2 decisions" in the user's decision record of 2026-10-07. They override D11 where they differ.

- **Market-data provider (OQ-5 resolved; platform PQ-5).** Phase 2 daily provider = the Python library `yfinance` (Yahoo Finance) behind the platform's existing provider adapter interface, for SPY daily completed OHLCV, adjusted close, dividends and splits. Recorded caveats: unofficial and not affiliated with Yahoo, no API key, rate-limited and can break on upstream changes, Yahoo terms are personal/research use. Consequences: retrieved market data is never committed to any public repo (fixtures stay synthetic); the library version is pinned; snapshot lineage records the library version and the retrieval timestamp; empty or partial responses become quality flags; calls retry with backoff. A fallback (for example Stooq via `pandas-datareader`) can be added later without a contract change. Exchange calendar: `exchange_calendars` (or `pandas_market_calendars`), XNYS, pinned version. It runs inside the platform ingestion Lambda/container; CI uses the mock provider only; an optional live-provider test is rate-limited and asserts shape, not values. Contract consequence (folded into 1.0.0, which is not yet published): the snapshot provider-lineage block (`lineage`, whose provider field is `provider`) gains optional `provider_library` and `library_version` fields next to the retrieval timestamp, the `finance/v1` daily observation gains optional `adj_close`, `dividend` and `split_ratio`, and the open quality-flag vocabulary registers `empty_response` and `partial_response`. No other repository calls the provider; FinanceModel reads only approved platform snapshots.
- **Explanation model (OQ-13 resolved).** Claude Opus 5 through the US cross-region inference profile `us.anthropic.claude-opus-5` (foundation model `anthropic.claude-opus-5`), verified ACTIVE in us-east-2 by a read-only inference-profile listing. The `us.` profile (not `global.`) keeps data routing in the US. Value of `/finplan/<env>/financeagent/config/explanation-model-id`; beta and gamma may set a cheaper model ID through the same key. Opus is a high-cost tier against the USD 5 `bedrock_explanations` allocation, so FinanceAgent enforces per-invocation max-token caps, a per-session budget check, prompt caching where supported, and the fixture provider in CI. IAM allows the inference profile and its underlying regional foundation-model ARNs (written generically in repo files). Model-access enablement for Opus 5 is a bootstrap step.
- **Pipeline bootstrap approval.** The user approves the one-time pipeline bootstrap in principle. It can run only after the bootstrap IaC is implemented (tasks 10.5 and 14.x here, and the platform change's bootstrap tasks). At run time the operator (Claude) still shows the exact stacks and a cost estimate and runs the bootstrap under this approval. No deployment happens during spec work.

### D13. Platform verification fixes (contracts 0.2.0, 2026-10-07)

The platform change verified its synthesized templates against contracts 0.1.0. It found contract gaps that the platform could only accept as a recorded list (334 accepted ownership problems) or work around. Version 0.2.0 fixes them in the contract package. Every change is additive except one in `tools/refresh-market-data-response` (see "Versioning" below). Neither version is published, and 0.x stays beta-only.

- **Ownership matrix.** Rows list every type their constructs synthesize:
  - the plan API row: API Gateway resources, deployment, stage and gateway responses, plus the handler's role and log group;
  - every row whose reference is published in SSM: `AWS::SSM::Parameter`;
  - the scheduler row: its invoke role, dead-letter queue, queue policy and alarm;
  - the budget row: its roles, topic policy and subscriptions;
  - the pipeline rows: the artifact bucket policy.

  New rows: the platform metadata sweeper, the permission-boundary policies (account-level pipeline tooling), and one per-environment pipeline-role row per repository. `AWS::ApiGateway::Method` cannot be tagged, so it is attributed to its parent API like the other CDK helpers. The checker was not relaxed: the platform's synthesized templates now pass with zero problems and no accepted-gap list.
- **Per-environment pipeline roles.** A deploy, CloudFormation execution or stage role serving one environment is declared in the account-level pipeline stack. It is tagged with its environment and carries that environment's boundary, as the single-account isolation requirement asks. A repository's pipeline row (`shared`) and its environment-role row (`environment`) share these logical roles, and the resource's `environment` tag selects the row. Rejected: keeping per-environment deploy roles tagged `shared` with the shared boundary, because then no boundary would deny their actions on another environment.
- **Permission boundaries accepted by IAM.** The name-based cross-environment deny used `arn:*:*:*:*:*finplan-<env>-*`, a wildcard service segment that CloudFormation validation flags (E3510) and that IAM may reject at deploy time. It is now one ARN pattern per service: S3, DynamoDB, Lambda, IAM roles and policies, SSM, Secrets Manager, SQS, SNS, CloudWatch Logs, EventBridge rules and schedules, Step Functions, CloudFormation and SageMaker. The patterns use the `${AWS::Partition}`, `${AWS::Region}` and `${AWS::AccountId}` pseudo parameters, written as `Fn::Sub` in the CloudFormation-ready documents. The research boundary's API-write patterns are compacted (`P*` covers POST, PUT and PATCH; `v1/plans*` covers the plans paths), so every boundary stays below IAM's 6,144-character managed-policy limit, which a unit test checks. The tag-based deny still covers every other service.
- **Tooling:**
  - the boundary check renders pseudo-parameter `Ref`s inside `Fn::Join`, the form CDK writes for a boundary ARN, so no normalization step is needed;
  - the copied-`$id` scanner reads YAML with timestamps kept as text and skips documents that are not JSON-compatible, so planning metadata no longer crashes it.
- **Schemas.**
  - New request/response schemas for the plan lifecycle API routes that had none: create portfolio (finance), create plan, create root version, record execution, the snapshot observation read (finance) and the staged-output outcome (`core/v1/api/*`, `finance/v1/api/*`).
  - New optional fields: `plan.publication_revision`, the snapshot's `lineage.calendar_version`, `quality_details` and `observation_summary`.
  - The Excel template documents `CASH` as the reserved cash-weight row.
  - The provider lineage field stays `lineage.provider`; specs that said `provider_id` are corrected.
- **Versioning.** `tools/refresh-market-data-response` may now omit `snapshot` for a non-session day with no committed snapshot. That answer carries `no_session`, `new_snapshot: false` and a null `input_snapshot_id`. Making a required response field optional is breaking under the compatibility gate's rules. 0.x is semver initial development and beta-only, so the change ships in 0.2.0 under the gate's explicit 0.x exemption (`--allow-zero-major-breaking`). The release notes name it. After 1.0.0 the same change would need a new major.
- **Decisions 15a/15b (2026-10-07).**
  - The identity-provider row is final: FinanceAgent owns one Cognito user pool per environment (FA-OQ-1 resolved).
  - The direct-test principal for FinanceLambdasTool is the project owner only, one per environment, referenced through SSM.
  - Both are registered in the SSM convention (D4).

### D14. Dead deny entries and pipeline log groups (contracts 0.2.1, 2026-10-07)

A patch release before the first bootstrap; no schema changes.

- **Live-financial deny list.** The `bedrock-agentcore-control:*` entries are removed. The AWS Service Authorization Reference documents one IAM service prefix for Amazon Bedrock AgentCore, `bedrock-agentcore`, covering its control-plane and data-plane APIs; `bedrock-agentcore-control` is an SDK client name, so those entries matched no action. The `bedrock-agentcore:*Payment*`, `*Wallet*` and `*Funds*` entries keep the coverage. Rejected: keeping the dead entries "for safety", because a deny that names no action protects nothing and suggests coverage that does not exist.
- **Budget enforcement deny list.** `bedrock:Converse*` is removed: no IAM action has that name. Converse and ConverseStream are authorized by `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, both denied by `bedrock:InvokeModel*`.
- **Ownership matrix.** The `pipeline-financialplanning` row lists `AWS::Logs::LogGroup` for the explicit 30-day CodeBuild project log groups (tagged `pipeline-build-project`). A named log group references no template resource, so it cannot be parent-attributed; the budget-state writer's log group already matches the `project-budget` row. The checker is not relaxed.

## Risks / Trade-offs

- [Single account (decided) has a weaker blast-radius boundary than multi-account] → naming, env tags, IAM permission boundaries with env-tag denies, separate per-env resources and the gamma isolation test suite. Multi-account remains a possible future migration that changes configuration only.
- [CodeArtifact blocks collaborators without AWS access] → the raw schema tarball can be mirrored publicly later (OQ-9). Schemas contain no secrets.
- [Contract churn before 1.0 blocks consumers] → keep 0.x in beta only. Gamma/prod require ≥ 1.0.0.
- [Delegated `run_id`/`model_version` minting may diverge] → the platform verifies both against FinanceModel's published registry reference before committing staged output.
- [SSM parameter size limits for manifests] → advanced tier when needed, with the full manifest also stored in the pipeline artifact store.
- [Bootstrap runs with the user's existing, possibly root, CLI session] → the bootstrap is one-time, approved in principle (D12) and shown as exact stacks plus a cost estimate before it runs; it only creates the scoped automation roles, after which nothing uses the bootstrap identity. It prints a recommendation to move to a scoped/MFA role later (D11).
- [Shared Jev API key across environments] → read access is granted per environment only to FinanceModel Jev job roles on that single secret; usage is bounded by TypeSafe prepaid credits and FinanceModel's approval gate.
- [Budgets data lags actual spend] → pre-flight category checks and GPU approval gates act before spend; the Budgets deny action is the backstop.
- [`yfinance` is unofficial and can break or be rate-limited] → provider adapter boundary, backoff, quality flags, mock provider in CI and a possible later fallback adapter without contract change (D12).
- [Opus 5 cost against a USD 5 allocation] → per-invocation token caps, per-session budget checks, prompt caching, fixture provider in CI, cheaper model allowed in beta/gamma, and the Budgets deny action on the FinanceAgent runtime role (D12).
- [The name-based cross-environment deny lists a fixed set of services (D13)] → the tag-based deny covers every service; adding a service that names resources per environment means adding its ARN pattern to the boundary generator.
- [The research boundary is close to IAM's managed-policy size limit: about 5,900 of 6,144 characters once resolved (D13)] → a unit test checks every boundary against the limit with the longest region names; new deny entries must fit or replace existing ones.
- [Per-environment pipeline roles live in an account-level stack (D13)] → they are tagged with their environment and bounded by its boundary; the ownership check selects their row by that tag, so a wrongly tagged role fails ENV-16 or ENV-18.
- [GPU quota of 1 serializes benchmarks] → concurrency lease plus a queue in FinanceModel. Any quota increase is requested only when a planned GPU run fits the `gpu` allocation (D11).

## Migration Plan

1. Human prerequisites: the user's in-principle approval of the bootstrap (given 2026-10-07, D12), confirmed at run time by showing the exact stacks and a cost estimate; an authenticated AWS CLI session (existing credentials are acceptable, D11); local untracked configuration naming the account and the chosen existing CodeConnection; a budget notification address.
2. Bootstrap the FinancialPlanning pipeline (authenticated CDK deploy of the tooling/pipeline stack only, after an STS account check against local untracked config): write the CodeConnection reference, cost ceiling and default allocation to SSM, create the scoped roles, then run the source-stage dry run. If the dry run fails, the user extends the GitHub App installation and the dry run is repeated. Publish contracts 0.x to beta, then 1.0.0.
3. The platform implementation change deploys through that pipeline.
4. Bootstrap the FinanceModel, FinanceLambdasTool and FinanceAgent pipelines in the integration order. Each pins contracts 1.x.
5. Rollback: redeploy `previous_release_id` per D6. A contract rollback is never a version deletion. Consumers re-pin to an earlier version, and producers keep serving prior majors.

## Open Questions

Specs are written to hold regardless of these answers. Items marked BLOCKER stop the named step until resolved.

| ID | Question | Blocks | Resolved by | Interim |
|---|---|---|---|---|
| OQ-1 | Single account with 3 envs vs multi-account | ~~BLOCKER for real prod data~~ none | **RESOLVED 2026-10-07:** single account in us-east-2, isolation by naming, tags, permission boundaries/env-tag denies and separate per-env resources (D5, D11). Multi-account only a possible future migration | n/a |
| OQ-2 | Do existing GitHub CodeConnections cover the four repos? | None (non-blocking) | **RESOLVED 2026-10-07 (non-blocking):** reuse an existing AVAILABLE connection, referenced via SSM; the bootstrap verifies access with a source-stage dry run; only on failure does the user extend the GitHub App installation (D11) | n/a |
| OQ-3 | Explanation provider and its credentials | None | **RESOLVED 2026-10-07:** Amazon Bedrock (IAM auth, no API key, `bedrock_explanations` allocation); no OpenAI secret; OpenAI only as an optional future adapter (D11). Model ID moves to OQ-13 | n/a |
| OQ-4 | TypeSafe Jev identity, API, version, license | None | **RESOLVED 2026-10-07:** identity and API verified by the user; key in secret `finplan/shared/financemodel/jev-api-key`; strategy phase 2 behind user approval; exact model value recorded per response, pinned when the API accepts an exact ID (D11). Strategy details owned by FinanceModel | n/a |
| OQ-5 | Initial instrument and data provider capabilities | None | **RESOLVED 2026-10-07:** instrument = S&P 500 tracking-ETF daily series (SPY), distinct from index level and constituents; daily completed observations only, no intraday (D11). Provider = `yfinance` (unofficial Yahoo Finance library, no API key, pinned version) behind the platform provider adapter, with an `exchange_calendars` XNYS calendar; no retrieved data committed; lineage records library version and retrieval timestamp (D12). Implemented by the platform change (PQ-5) | Fixture and mock providers in phase 1 and in CI |
| OQ-6 | Daily schedule 09:00 vs 09:30 America/New_York | None (config value) | User choice | Configurable; the default is set by the platform change |
| OQ-7 | Cost ceilings and allocation | None | **RESOLVED 2026-10-07:** USD 50 total, not split per repo; default category allocation in `/finplan/shared/financialplanning/config/budget-allocation` (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5); Budgets alerts at 50/80/100% with a deny action at 100%; every GPU run needs explicit user approval with a cost estimate; Jev billed outside AWS (D11). Per-call tool limits (LT-OQ-3) are set by FinanceLambdasTool within these categories | n/a |
| OQ-8 | `ml.g6.12xlarge` Processing quota is 0 | BLOCKER for a GPU Processing-Job Qwen driver | Quota increase request, or the Training-Job batch / short-lived endpoint pattern | Phase 3 only |
| OQ-9 | Contract package access for collaborators without AWS credentials | None for phase 1 | User decision (public mirror of the schema tarball?) | CodeArtifact only |
| OQ-10 | Which repo owns the website | None for phase 1 | User decision | Provisionally FinancialPlanning, not in phase 1 |
| OQ-11 | Scoped bootstrap role in place of root CLI use | None (non-blocking) | **RESOLVED 2026-10-07:** bootstrap uses the user's existing authenticated CLI session; the refuse-root rule is dropped; the bootstrap still creates scoped automation roles. Recommendation only: move to a scoped/MFA role later (D11) | n/a |
| OQ-12 | Who builds the FinanceModel explanation-evidence experiment kinds needed by FinanceAgent `add-explanation-workflows` (D10)? | BLOCKER for deployed phase 2 explanations only | A follow-up FinanceModel change after contracts adds the evidence schemas | Fixture evidence in FinanceAgent tests |
| OQ-13 | Which Bedrock model (or inference profile) in us-east-2 is the explanation model, and is model access enabled? | None (model-access enablement is a bootstrap step) | **RESOLVED 2026-10-07:** Claude Opus 5 via the US cross-region inference profile `us.anthropic.claude-opus-5` (verified ACTIVE in us-east-2, read-only), stored in `/finplan/<env>/financeagent/config/explanation-model-id`; cheaper model allowed in beta/gamma via the same key; cost controls and IAM in FinanceAgent; model access for Opus 5 enabled at bootstrap (D12) | Fixture provider in CI and until model access is enabled |
