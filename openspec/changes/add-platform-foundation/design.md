# Design

## Context

See proposal.md (Why). Requirements are in `specs/`: platform-storage, plan-metadata-store, plan-lifecycle-api, market-data-ingestion, staged-output-acceptance, excel-plan-import, platform-cost-guardrails and platform-pipeline.

This change is bound by `establish-cross-repo-contracts` and does not redefine anything from it. It uses that change's:

- identifier formats and minting authority (`platform-identifiers`, design D2);
- contract package `finplan-contracts` / `@finplan/contracts`, JSON Schema `$id`s under `https://contracts.finplan.invalid/`, error envelope and registered codes (`contract-schemas`, D3);
- SSM convention `/finplan/<env|shared>/<repo>/<category>/<name>` and release manifest (D4);
- isolation model (D5) and pipeline standard (D6);
- phase plan (D7), region `us-east-2` (D8) and domain-adapter split (D9);
- open-question register OQ-1..OQ-13 and the 2026-10-07 user decisions (contracts D11).

New questions raised here are numbered `PQ-n`.

### Observed facts (from the 2026-10-07 read-only discovery)

- All project resources and the AgentCore control plane are in us-east-2. There are no AgentCore runtimes, gateways or ECR repositories yet.
- No platform resources exist. The four GitHub repositories are empty and public.
- Six GitHub CodeConnections exist and are AVAILABLE. The pipeline reuses one of them; coverage of FinancialPlanning is proved by the bootstrap's source-stage dry run (OQ-2 non-blocking, contracts D11).
- The discovery CLI caller was the account root principal. The user decided the bootstrap uses that existing authenticated session (OQ-11 resolved, contracts D11).
- The user set a **USD 50 total AWS budget** for everything (2026-10-07), with the default category allocation `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5 (OQ-7 resolved).
- The initial instrument is decided: an S&P 500 tracking-ETF daily series (for example SPY). Round 2 user decision (2026-10-07): the provider is the `yfinance` Python library and the calendar source is the `exchange_calendars` library, XNYS (OQ-5 and PQ-5 resolved, contracts D12). Neither library's behaviour has been exercised by this project yet; facts about them below are the user's recorded caveats.

### Assumptions (unverified; revisit when the named question closes)

- PA-1. Python 3.12 Lambda handlers and AWS CDK (contract assumptions A2/A3).
- PA-2. DynamoDB on-demand, S3 conditional writes (`If-None-Match`), EventBridge Scheduler with IANA time zones, API Gateway REST with IAM auth, and AWS Budgets actions are available in us-east-2. The bootstrap pre-check confirms this.
- PA-3. Phase 1 data volumes are tiny (fixtures of kilobytes per day). All Lambda work fits within synchronous API timeouts.
- PA-4. ~~Assumption~~ **Decided 2026-10-07:** beta, gamma and prod share one account (contracts D5; OQ-1 resolved).

## Goals / Non-Goals

**Goals:**
- A deployable, fixture-backed platform in beta, gamma and prod that consumers can integrate against (ENV-15).
- Every platform state change is conditional, transactional and idempotent. Every artifact is write-once.
- One code path per operation, regardless of caller (website, Excel, scheduler, MCP adapter).
- Near-zero standing cost, with enforceable budget protection.

**Non-Goals:**
- Calling the real provider from any phase 1 deployment, CI run or prod smoke. The `yfinance` adapter is built and tested here (mock-backed) and enabled per environment only by phase 2 configuration. Intraday ingestion is out of scope for phases 1 and 2.
- Model compute of any kind. FinanceModel owns it, and phase 1 uses only its CPU stub or test fixtures.
- Building the website UI (OQ-10). This change provides the API path a website will use, plus a website-path test client.
- Live trading, Coinbase, AgentCore payments, wallet spending, and automated rewriting of risk preferences.
- Changes to the contract package itself. The gaps this change first recorded were resolved in the contracts change (D4, D10) by the cross-repo review on 2026-10-07.

## Decisions

### P1. Component layout

```
 website-path client ─┐
 FinanceLambdasTool ──┼─► API Gateway (REST, IAM auth, per env) ─► plan-api Lambda ──┐
 Excel (via API) ─────┘                                  └─► ingestion Lambda ◄──────┼── EventBridge Scheduler (America/New_York)
                                                                                     │
 FinanceModel job role ─► outputs/staging/<run_id>/ ─(accept call)─► acceptance ─────┤
                                                                                     ▼
                       DynamoDB (on-demand, PITR, KMS) + S3 (6 buckets, KMS, write-once)
```

- **Handler model.** One Python package `platform_core` holds the domain operations. Thin handlers adapt API Gateway events and scheduler events onto the same operation functions. The scheduler invokes the ingestion Lambda directly with the same request envelope the API uses, plus `trigger: scheduled`. This satisfies "same implementation" without the scheduler needing to sign HTTP requests.
- **API type.** REST API rather than HTTP API, because it supports resource policies that restrict callers to same-environment principals (contract D5) together with IAM auth.
- **No VPC.** Lambdas run without a VPC, so no NAT gateway is needed (cost). They reach S3 and DynamoDB through IAM-scoped public endpoints.

### P2. Storage (platform-storage)

| Logical role | SSM parameter (`/finplan/<env>/financialplanning/config/…`) | Contents | Key layout |
|---|---|---|---|
| raw | `bucket-raw` | provider responses as received, rejected records, Excel uploads | `provider/<provider_id>/<dataset>/<retrieved_at>/<ulid>.json`; `uploads/excel/<sha256>.xlsx` |
| curated | `bucket-curated` | normalized observations (one object per dedupe key) | `<dataset>/<instrument>/<session_date>/<kind>/<source_ts>.json` |
| snapshots | `bucket-snapshots` | snapshot manifests + payloads | `<input_snapshot_id>/manifest.json`, `…/payload/*` |
| plans | `bucket-plans` | plan-version content, exports | `<plan_id>/<plan_version_id>/content.json`, `…/export-<template_v>.xlsx` |
| outputs | `bucket-outputs` (+ `run-staging-ref` → staging prefix) | run staging and accepted-output records | `staging/<run_id>/…`, `accepted/<run_id>/…` |
| reports | `bucket-reports` | reports (phase 1: validation and ingestion reports) | `<record_id>/<artifact_id>` |

- **Encryption.** One customer-managed KMS key per environment, with S3 Bucket Keys enabled to cut KMS request volume. Per-bucket keys were rejected: each customer-managed key carries a fixed monthly charge, which matters under the USD 50 cap. **PQ-1:** whether beta uses SSE-S3 instead, to avoid one key's fixed charge. The price must be taken from current AWS pricing at decision time; no price is assumed here.
- **Write-once.** All platform writes of immutable artifacts use S3 conditional `PutObject` with `If-None-Match: *`. Bucket policies deny `s3:DeleteObject*` and `s3:PutObject` without the condition to application roles on `snapshots`, `plans` and `reports`, and on `outputs/accepted/`.
- **S3 Object Lock.** Not enabled in phase 1. Object Lock in compliance mode prevents teardown, and governance mode adds little over the deny policies while the data is synthetic. **PQ-2:** revisit for prod before non-synthetic portfolios are allowed (OQ-1 is resolved as single account, so Object Lock is the remaining hardening option).
- **Lifecycle defaults.** These are proposed values in per-environment configuration and are not user-approved (**PQ-3**):

| | beta | gamma | prod |
|---|---|---|---|
| raw, curated, snapshots, plans, reports | expire 14 d | expire 30 d | no expiry (unset) |
| outputs/staging | 7 d | 7 d | 7 d |
| noncurrent versions | 1 d | 7 d | 30 d |
| incomplete multipart | 1 d | 1 d | 1 d |

  There are no storage-class transitions in phase 1: objects are tiny, and transitions carry per-request charges.

- **Expired snapshot reads.** When a beta or gamma artifact expires, a daily sweep marks the catalog record `expired`, and reads return `NOT_FOUND` with details naming the expiry.
- **Access.** Block Public Access is enabled, object ownership is BucketOwnerEnforced, `aws:SecureTransport` is required, and the KMS key is enforced through `s3:x-amz-server-side-encryption-aws-kms-key-id`.
  - The FinanceModel job role gets read on `snapshots/*` but only for approved snapshots. The catalog status `approved` is mirrored as an object tag, and the grant is conditioned on that tag.
  - The same role gets `PutObject` only on `outputs/staging/${run_id}/*`. Cross-run reads are denied because the role has no `GetObject` on staging at all.
  - The job-role principal name comes from `/finplan/<env>/financemodel/job/*` configuration.

### P3. Metadata store (plan-metadata-store)

- **Tables.** DynamoDB on-demand tables per environment, following the contract D1 list: `portfolio`, `plan` (holds the head pointer `{current_version_id, revision}` and the publication head `{current_publication_id, publication_revision}`), `plan_version`, `publication`, `execution`, `snapshot_catalog`, `staged_output` (acceptance outcomes keyed by `run_id`), `idempotency` (TTL attribute at 8 days, at least the contract's 7), and `audit_event` (append-only).
  - Multiple tables were chosen over a single-table design so that IAM can scope access per table, for example a deny for research roles on everything, and so that PITR and alarms are per table.
  - The `staged_output` table is now listed in the contracts D1 metadata row. It is platform-internal and its only consumer surface is `GET /v1/staged-outputs/{run_id}`.
- **Version creation transaction.** A single `TransactWriteItems` call with four items:
  1. Put `plan_version` with `attribute_not_exists(pk)`.
  2. Update `plan` with `revision = :expected`, setting `current_version_id` and `revision + 1`.
  3. Put `idempotency` with `attribute_not_exists`.
  4. Put `audit_event`.

  A `TransactionCanceledException` is mapped to `CONFLICT` (revision), to an idempotency replay (key exists with the same hash) or to `IDEMPOTENCY_KEY_REUSED`.
- **Status transitions.** Conditional updates on `status = :from`, in the same transaction as the audit event.
- **Orphan sweep.** A scheduled daily Lambda lists artifacts younger than the grace period (24 h) that have no metadata row and deletes them. It is the only principal allowed to delete in `plans` and `snapshots`, and only for keys without a catalog row.
- **Recovery and encryption.** PITR is on, and the environment KMS key encrypts the tables.

### P4. Plan API surface (plan-lifecycle-api)

| Route | Operation | Idempotency key | `expected_revision` |
|---|---|---|---|
| `POST /v1/portfolios` | create synthetic portfolio | yes | n/a |
| `POST /v1/portfolios/{portfolio_id}/plans` | create plan | yes | portfolio |
| `POST /v1/plans/{plan_id}/versions` | create a root version (`origin` model_run, names its lineage) or a child (`origin` manual_override, inherits its lineage) | yes | plan head |
| `GET /v1/plan-versions/{plan_version_id}` | read with checksum, lineage, status | n/a | n/a |
| `POST /v1/plan-versions/{plan_version_id}/validate` | deterministic validation | yes | n/a (status-conditional) |
| `POST /v1/plans/{plan_id}/publications` | publish exact validated version | yes | publication head |
| `GET /v1/publications/{publication_id}` | read | n/a | n/a |
| `POST /v1/publications/{publication_id}/executions` | record paper/simulated execution | yes | n/a |
| `GET /v1/executions/{execution_id}` | read | n/a | n/a |
| `POST /v1/plan-versions/{plan_version_id}/exports` | Excel template export, returns download grant | yes | n/a |
| `POST /v1/plans/{plan_id}/imports` | issue Excel upload grant (`import_id`) | yes | n/a |
| `POST /v1/plans/{plan_id}/imports/{import_id}/commit` | parse + create child version (`origin` excel_import) | yes | from workbook |
| `POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept` | staged output acceptance | yes | plan head |
| `POST /v1/ingestions` | on-demand ingestion (same handler as scheduler) | yes | n/a |
| `GET /v1/snapshots/{input_snapshot_id}` | snapshot metadata (incl. `status`) + trusted refs | n/a | n/a |
| `GET /v1/snapshots/{input_snapshot_id}/observations` | bounded, paginated observation read (filter by instrument and date range) plus the full payload's trusted ref | n/a | n/a |
| `GET /v1/portfolios/{portfolio_id}` | portfolio read (incl. `synthetic`, `revision`) | n/a | n/a |
| `GET /v1/plans/{plan_id}` | plan head: `current_version_id`, `revision`, `current_publication_id`, `publication_revision` | n/a | n/a |
| `GET /v1/plans/{plan_id}/versions` | paginated version list (ID, parent, origin, status, checksum, `created_at`), newest first, opaque page token | n/a | n/a |
| `GET /v1/staged-outputs/{run_id}` | acceptance outcome for a run (`accepted` with `plan_version_id`, `rejected`, `no_version` with solution status) | n/a | n/a |

The six read routes in the lower part of the table were added by the cross-repo review (2026-10-07). They close FinanceLambdasTool gaps G-1 and G-2 and the FinanceModel staged-outcome read (contracts D10).

- **Checksums.** The checksum is SHA-256 over the RFC 8785 (JCS) canonical plan content, as the contract's configuration rule defines. That is why the website path and the agent path agree byte-for-byte.
- **Version lineage.** Only a root version (no parent) names `input_snapshot_id`, and it names its full lineage: `input_snapshot_id`, `configuration_id`, `model_version` and `run_id`. A child version inherits `input_snapshot_id`, `configuration_id` and `model_version` from its parent; the route's child request (the contract `tools/create-override-version-request`) cannot name them. Staged-output acceptance (P7) is the one creator that records a run's own lineage on a child, from the run's manifest.
- **Route schemas.** Requests and responses are validated with the pinned contract validators. Since contracts 0.2.0 the contract carries the route schemas the platform first defined itself (create portfolio, create plan, create root version, record execution, the observation read and the staged-output GET; contracts D13). Platform schemas, built only from contract `$ref`s, remain only where the contract has none (path and query parameters, staged-output acceptance, Excel). Every route validates its response before sending it; a non-conformant response is `INTERNAL` and is never sent. The plan record's `publication_revision` is the contract field (optional since 0.2.0).
- **No-effect detection.** Compares the child checksum with the parent checksum.
- **Validation rules.** These are deterministic platform code and are versioned. The plan version records `validation_ruleset_version`. Default tolerance: weights sum to 1 ± 1e-9 (configurable).
- **Authorization.**
  - The API resource policy allows only same-environment principals named in configuration: the platform's own roles, the FinanceLambdasTool Lambda roles (from `/finplan/<env>/financelambdastool/lambda/role-<class>-arn`), a website-backend role, and operator roles.
  - FinanceModel principals (from `/finplan/<env>/financemodel/job/*`) get read-only access: the job role may call `GET /v1/snapshots/*` only, and the job-API role may also call `GET /v1/staged-outputs/*`. Both are denied every write route (contracts ENV-04).
  - Per-route method grants follow the FinanceLambdasTool role classes: `reader` gets GET routes; `submitter` adds `POST /v1/ingestions`; `plan-writer` adds version create, validate and publish. No tool role may call execution, import, export or staged-output accept routes in phase 1.
  - **PQ-4:** how human website users obtain those IAM credentials (for example, a Cognito identity pool). It is deferred with OQ-10. Phase 1 tests use a "website-path client" role that calls the same routes.
- **Publication superseded.** The `publication_superseded` flag on executions is computed at write time from the publication head.
- **Schema upgrade.**
  - Records store `contract_version`. Handlers use the contract package's validators for that recorded version's major.
  - Writers write only the current minor.
  - Readers never rewrite stored records. Additive fields are absent on older records and defaulted at read time without changing the stored checksum.

### P5. Ingestion (market-data-ingestion)

- **Pipeline inside one invocation.** The steps are:
  1. idempotency check;
  2. budget pre-check (P8);
  3. calendar resolution;
  4. provider capability check;
  5. provider fetch, with the raw response persisted to `raw` before parsing;
  6. normalize to the `finance/v1` observation schema;
  7. validate, producing quality flags and rejections;
  8. dedupe-persist curated observations (conditional put per dedupe key);
  9. write the snapshot manifest and payload (write-once);
  10. commit the catalog row, idempotency record and audit event in one transaction.
- **Scheduled idempotency key.** `sched-<env>-<dataset>-<scheduled_session_date>`. A duplicate delivery or a retry returns the original result.
- **Schedule.** An EventBridge Scheduler schedule with expression `cron(0 9 ? * MON-FRI *)` or `cron(30 9 ? * MON-FRI *)` and `ScheduleExpressionTimezone` `America/New_York`. The expression is generated from the `ingest-schedule` value at synth time, and only `09:00` or `09:30` are accepted. The default is **09:00**, so the run completes before the 09:30 regular open and the previous session's completed bar is available for pre-open planning. The final choice is OQ-6, a configuration change only. The scheduler uses a retry policy and a dead-letter queue. DLQ depth over 0 raises an alarm.
- **Session calendar.**
  - A versioned calendar artifact (JSON: date → `regular | early_close | holiday`, plus close time) is shipped with the service and recorded by version in each snapshot.
  - Weekends are derived. Dates outside the calendar's coverage fail with `PRECONDITION_FAILED`.
  - **Calendar source (PQ-5 RESOLVED 2026-10-07):** the `exchange_calendars` library (alternative `pandas_market_calendars`), exchange `XNYS`, pinned version. The build generates the versioned calendar artifact from the pinned library for a configured date range, so the ingestion function does not import the library at run time. The calendar version is `xnys-<library>-<library_version>-<coverage>` and is recorded in every snapshot. The library licence is checked and recorded when the version is pinned.
  - The synthetic fixture calendar (at least one holiday and one early close) is still used with the fixture provider and in unit tests.
- **Observation kinds.**
  - A daily bar is `completed_daily` iff `now >= session_close(date)` per the calendar (early closes included) **and** the provider marks the record final.
  - A provider adapter that cannot assert finality marks it final only after a configurable settle delay, recorded as quality flag `finality_inferred`.
  - `intraday_partial` comes only from adapters that declare `intraday`.
- **Quality flags.** These are envelope-level and domain-neutral: `no_session`, `missing_sessions`, `rejected_records`, `source_revised`, `contains_intraday_partial`, `finality_inferred`, `no_new_observations`, `stale_source`. They are registered as an open enum in the contract package (contracts D10; formerly a CONTRACT GAP; pinned 0.2.0, carried into 1.0.0).
- **Snapshot status.** A committed snapshot has `status` `committed`. It becomes `approved` when it matches the versioned approval rule (`approval_rule_version`); the phase 1 rule approves a snapshot that has no blocking flag (`rejected_records` above the configured ratio, or `stale_source`). Approval is an audited conditional transition, mirrored as the object tag that conditions the FinanceModel read grant (P2). Expiry sets `expired`. The enum and rule version are part of the contract package (contracts D10; pinned 0.2.0, carried into 1.0.0).
- **Holiday behaviour.** No provider call is made and no new snapshot is created. The result carries `no_session` and references the latest committed snapshot for the dataset. If none exists, the result has a null `input_snapshot_id` and `no_session`.
- **Provider adapter interface.** It exposes `describe()` (datasets, granularities, history depth, rate limits, finality support) and `fetch(request)`. This change ships three implementations:
  - **fixture provider:** deterministic, synthetic, `synthetic: true`, daily only, covering roughly 2 years of synthetic sessions;
  - **mock provider:** programmable for tests, for throttling, revisions, gaps, bad bars, empty and partial responses, and an intraday-capable variant;
  - **`yfinance` provider (P6a):** daily only, enabled only by phase 2 configuration.
  A later fallback adapter (for example Stooq via `pandas-datareader`) plugs into the same interface without a contract change.
- **Synchronous path.** On-demand ingestion is synchronous in phase 1 (PA-3). **PQ-6:** whether real-provider ingestion needs an asynchronous job form (`202` plus status) in phase 2. That would be an additive contract change.

### P6. Initial dataset (instrument decided 2026-10-07; provider RESOLVED 2026-10-07, OQ-5)

- **Decision:** S&P 500 exposure through a single daily OHLCV series for one US-listed ETF that tracks the S&P 500 (for example SPY). Use completed daily observations only, in USD, with the adjustment basis recorded explicitly. The exact ticker is configuration (`finance/etf-daily/<instrument>`), so choosing SPY or another tracker does not change specs.
- **Why:** a tradable instrument maps directly to paper execution (an index level is not tradable). One series keeps phase 2 ingestion small and cheap. It is a separate dataset (`finance/etf-daily/<instrument>`) from the index level (`finance/index-level/<index>`) and from the constituent universe (`finance/universe/<index>`), so later expansion does not change identity semantics.
- **Granularity:** daily completed observations only in phases 1 and 2. No intraday dataset may be enabled; the `intraday_partial` path exists only for the intraday-capable mock in tests.
- **Provider: RESOLVED 2026-10-07 (OQ-5).** `yfinance` (P6a). It needs no API key or secret and adds no AWS charge beyond the ingestion Lambda's own usage inside `platform_infra`.
- **Phase 1:** fixture provider only, serving a synthetic series under the ETF dataset identity. A phase 1 configuration that names any other provider fails the build (market-data-ingestion spec).
- **Phase 2:** an environment's configuration sets `phase: 2` and `provider: yfinance`; beta is enabled first, then gamma, then prod through the normal pipeline promotion.

### P6a. `yfinance` provider adapter (round 2 user decision, 2026-10-07)

- **What it fetches.** SPY (the configured ticker) daily completed OHLCV, adjusted close, dividends and splits. Unadjusted OHLC is stored with `adjustment_basis` recorded; adjusted close, dividends and split ratios are stored as separate fields, never folded into the unadjusted prices.
- **Recorded caveats (user decision record).** Unofficial and not affiliated with Yahoo; no API key; rate-limited; can break when Yahoo changes upstream; Yahoo's terms are personal/research use. Consequences below.
- **No market data in the repo.** Retrieved data lives only in the platform's private per-environment buckets (`raw`, `curated`, `snapshots`). Test fixtures, including any provider-response fixtures for the adapter, are synthetic and shape-only, flagged `synthetic: true`. A build-stage data-hygiene check fails if a fixture lacks the synthetic flag or matches a real-data signature list.
- **Pinning.** `yfinance` and `exchange_calendars` are pinned by exact version in the dependency lock. Upgrades are ordinary changes that go through beta and gamma.
- **Lineage.** Each snapshot's provider lineage (`lineage`) records `provider: yfinance`, `provider_library: yfinance`, `library_version`, the retrieval timestamp (`retrieved_at`), and the calendar version (`calendar_version`). These are optional lineage fields of the contract package: `provider_library` and `library_version` from contracts D12, `calendar_version` from contracts 0.2.0 (contracts D13).
- **Rate limiting and backoff.** A client-side minimum interval between provider requests (configuration), and retry with exponential backoff and jitter on throttling, transient network errors and empty responses, bounded by a configured maximum attempt count that fits inside the function timeout. After the attempts are exhausted the result is `RATE_LIMITED` (or `DEPENDENCY_UNAVAILABLE` for repeated non-throttle failures) with `retryable` true, and no partial snapshot is committed.
- **Empty and partial responses as quality flags.** After retries, an empty response for a range that contains `regular` sessions commits a snapshot with `empty_response` (blocking for approval, so the snapshot stays `committed`) and `missing_sessions`. A response missing some sessions or fields (for example no adjusted close) commits with `partial_response` and `missing_sessions` naming the gaps. Both flags join the open quality-flag vocabulary.
- **Finality.** The library gives no explicit finality marker, so a bar is final only after the session close plus the configured settle delay, flagged `finality_inferred`.
- **Runtime.** The adapter runs inside the platform ingestion Lambda (a container-image Lambda if the pinned dependencies exceed the zip package limit). The zip form fits: the build stage's arm64 bundle with the `providers` extra is 178 MiB unzipped (limit 250 MB; P10 "Lambda bundles"). No other repository calls `yfinance`; FinanceModel reads only approved snapshots and FinanceLambdasTool calls the ingestion and snapshot API.
- **Tests.** CI (unit, contract, integration-beta and gamma suites) uses the mock provider only; the adapter's parsing is tested against synthetic shape fixtures. An optional live-provider test is opt-in (never in CI by default, never in prod smoke), rate-limited to a single small request, and asserts shape (columns, types, calendar alignment), not values.

### P7. Staged-output acceptance

- The worker writes files, then `manifest.json` last, under `outputs/staging/<run_id>/`. This mirrors the write-manifest-last pattern observed in the earlier Qwen weight-staging job.
- Acceptance is an explicit API call from a platform-side caller (the scheduled workflow, an operator, or the website path), not an S3 event trigger. FinanceLambdasTool exposes no accept tool in phase 1, and its role classes are denied the route. The manifest is the only completion marker; FinanceModel writes no separate marker file (contracts D10). An explicit call carries the caller identity, `idempotency_key` and `expected_revision`, which S3 events cannot. An event-driven trigger can be added later on top of the same operation.
- **Steps:**
  1. Verify the manifest.
  2. Resolve `run_id` and `model_version` against `/finplan/<env>/financemodel/model/registry-ref`. If FinanceModel has no release in the environment, fail with `DEPENDENCY_UNAVAILABLE`.
  3. Copy the payload to `plans` (write-once).
  4. Run the version-creation transaction with origin `model_run`.
  5. Validate.
  6. Write the `staged_output` outcome row keyed by `run_id`, with a conditional put that enforces at most one version per run.
  7. The outcome is readable through `GET /v1/staged-outputs/{run_id}`, so FinanceModel can link its run result to the platform decision.
- Lineage verification in step 2 calls FinanceModel's registry lookup through its job API (explicit invoke grant from FinanceModel to the platform acceptance role).
- The staged-output manifest schema (`core/v1/staged-output-manifest.json` plus the `finance/v1` payload) is in the contract package (contracts D10; formerly a CONTRACT GAP; pinned 0.2.0, carried into 1.0.0).

### P8. Excel import

- **Two steps.** First an upload grant: a presigned POST with `content-length-range` and an expiry of 15 minutes, into `raw/uploads/excel/`. Then a commit call.
- **Parser rules.** Read as a ZIP/OOXML package with an XML parser that has external entities disabled:
  - reject on `vbaProject.bin`, macro content types, `activeX`, `oleObject`, `externalLink` parts, or an uncompressed/compressed ratio or total size above the configured limits;
  - read cell values only, never evaluating formulas;
  - a formula cell is rejected unless the template marks it formula-tolerant, in which case the cached value is used.

  The library choice is an implementation detail, configured read-only and data-only.
- **Template.** Template version `xlsx-plan-v1` has two sheets:
  - `meta`: `plan_id`, `base_plan_version_id`, `base_checksum`, `expected_revision`, `template_version`, `contract_version`;
  - `allocations`: `instrument_id`, `target_weight`, and optional `note`.

  The column contract belongs in the contract package: `finance/v1/excel-plan-template.json` and `core/v1/import-report.json` are in the contract package (contracts D10; formerly a CONTRACT GAP; pinned 0.2.0, carried into 1.0.0). The template documents `CASH` as reserved for the cash-weight row (0.2.0).
- **Lineage.** The upload is stored at `uploads/excel/<sha256>.xlsx`, and the version lineage holds a trusted artifact reference of kind `excel_source`. The trusted-reference `kind` vocabulary, including `excel_source`, `snapshot_manifest`, `plan_content`, `staged_output_manifest` and `validation_report`, is an open enum in the contract package (contracts D10; pinned 0.2.0, carried into 1.0.0).

### P9. Cost guardrails

- **Budget.** One AWS Budgets cost budget, deployed in the tooling (pipeline) stack because it is account-level. USD 50 is the total for everything (user decision 2026-10-07). It is scoped by the cost-allocation tag `project` once activated; until tag activation it covers the whole account. Alerts at 50%, 80% and 100% of actual spend, plus 100% of forecast. Notifications go to an SNS topic whose subscriber address is set by a human and never committed.
- **Allocation.** The bootstrap writes `/finplan/shared/financialplanning/config/budget-allocation` with the defaults `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5 (contracts D4/D11) unless the parameter already exists, and refuses an allocation whose categories sum to more than the ceiling. The user may edit it later. The platform's own spend belongs to `platform_infra`; FinanceModel and FinanceAgent pre-flight checks read their categories. TypeSafe Jev spend is billed outside AWS and not part of the budget.
- **Budget action.** At 100% of actual spend, the action applies an IAM deny policy (generated by this repo) to the roles named in configuration: deploy roles, the ingestion role, and FinanceModel job-submission roles (those names are published by FinanceModel under its SSM namespace).
  - The deny policy blocks `sagemaker:Create*`/`Start*`, `bedrock:InvokeModel*` (FinanceAgent runtime role, explanation calls), `codepipeline:StartPipelineExecution` and `codebuild:StartBuild`.
  - Ingestion invoke is deliberately **not** denied, so callers receive the contract `BUDGET_EXCEEDED` envelope from the pre-check below instead of a raw access-denied error.
  - Other repos publish the role names for the budget action at `/finplan/<env>/<repo>/config/budget-enforced-role-names` (registered in contracts D4).
- **Lifting the cap and the 2026-10-07 incident.** The action fired and applied the deny policy to 11 pipeline roles; its reset then failed with `RESET_FAILURE` because the shared boundary on the action's own execution role denied the detach. Contracts 0.2.2 (contracts D15) exempts only that role (`aws:PrincipalArn` like `finplan-shared-*-budget-action-role`) from the shared boundary's detach deny; every other principal stays denied and `budgets:ExecuteBudgetAction` stays denied to automation. Lifting the cap is a human decision: `REVERSE_BUDGET_ACTION` (now works) or a manual detach (`docs/bootstrap.md`). A Budgets action stuck in `RESET_FAILURE` is not repaired by an update, so the action's logical ID becomes `BudgetEnforcementActionV2`: the next bootstrap creates a fresh action in `STANDBY` and CloudFormation deletes the old one. Deleting it does not detach the policy, so a human detaches it from the affected roles once.
- **Pre-check.** The ingestion handler reads a `budget-state` flag (`/finplan/shared/financialplanning/config/budget-state`, set by the budget SNS → Lambda path) and refuses with `BUDGET_EXCEEDED` when it is set. This is cheaper and faster than calling the Budgets API per request.
  - Contract D4 now grants this budget-state writer Lambda the single runtime-writer exception for `/finplan/shared/financialplanning/config/budget-state`. FinanceModel's pre-flight check also reads it.
- **Cost drivers (no prices recorded; take them from current AWS pricing at decision time):**
  - the per-environment KMS key fixed charge (largest standing item; PQ-1);
  - CodePipeline V2 action minutes and CodeBuild minutes (dominant during development; keep builds small and cache dependencies);
  - API Gateway requests, Lambda, DynamoDB on-demand and S3 (near zero at fixture volume);
  - Scheduler invocations, SSM advanced parameters (avoid unless a manifest exceeds 4 KB), and the AWS Budgets action-enabled budget.

### P10. Pipeline

- The pipeline follows contract D6 exactly: CodePipeline V2 + CodeBuild, CDK cloud assembly built once, and stages Beta → Gamma → approval → Prod.
- **Lambda bundles.** The build stage builds one code bundle per function from `uv.lock` (python3.12, arm64 `manylinux_2_28` wheels, `--require-hashes`, `--only-binary :all:`). Each bundle holds the pinned vendored contract wheel, the runtime dependencies (`providers` extra for ingestion and the plan API), the platform package and `config/`. The synth then runs in release mode, where a function without a complete bundle fails the build, and a post-synth gate rejects any source-only or non-arm64 code asset. The first deployments shipped the bare source tree and failed at init (incident 2026-10-07, `docs/pipeline.md`). Every platform function role can write its own explicit log group.
- Beta runs the integration-beta suite. Gamma runs isolation denials and full lifecycle tests. Prod runs smoke on the `synthetic: true` smoke portfolio.
- The account-level stacks are deployed only by the authenticated bootstrap. There are two, because of template size: `finplan-shared-financialplanning-pipeline-store` (the pipeline store bucket, small enough to deploy inline) and `finplan-shared-financialplanning-tooling` (permission boundaries, budget, alerts and action, budget-state writer, and the pipeline with its roles). The tooling template exceeds CloudFormation's 51,200-byte inline limit, so the CLI stages it in the store under `bootstrap/`, which is why the store is a separate stack deployed first. Neither needs the CDK bootstrap stack. The contract package's CodeArtifact domain and repository (a FinancialPlanning matrix row, referenced by `/finplan/shared/financialplanning/contract/registry-ref`) is not declared in either stack yet; it is needed before contracts 1.0.0 is published (contracts task 7.3) and stays an open item of this change.
- **Account-level retention.** The pipeline store is versioned and kept on stack deletion (`Retain`; `docs/bootstrap.md` "Teardown" empties every version and deletes it by hand). Pipeline artifacts, `assets/` and `bootstrap/` expire after 30 days, the build cache after 14, noncurrent versions after 7, and incomplete multipart uploads are aborted after 7; the release ledger (`releases/`) never expires. A rollback to a release older than 30 days therefore needs its assets republished first. The budget-state writer and the four CodeBuild projects log to explicit log groups (30-day retention, deleted with the stack), attributed by their logical roles (contracts D14).
- **Pipeline roles.** The pipeline and build roles are account-level (`environment=shared`, shared boundary). The deploy, CloudFormation execution and stage roles of one environment are declared in the tooling stack but tagged with that environment and bounded by its permission boundary (contracts D13, ENV-21), so the boundary denies them every other environment's resources.
- **Bootstrap (contracts D11, D12).** Run once from the user's existing authenticated AWS CLI session under the user's in-principle approval (2026-10-07). It runs only after the bootstrap IaC is implemented, and the exact stacks and a cost estimate are shown before it runs. There is no refuse-root check. It verifies the STS account against local untracked configuration and the region, writes `/finplan/shared/financialplanning/config/codeconnection-ref` for the chosen existing AVAILABLE connection, creates the scoped pipeline/deploy/service roles, and runs a source-stage dry run on `main` before enabling the Beta stage. If the dry run fails, the user extends the GitHub App installation to FinancialPlanning and reruns it. The bootstrap prints a recommendation to move to a scoped/MFA role later.
- The contracts change's build steps (package build, publish) and this change's service build share the same pipeline, with the contract package built and published before service tests pin it.

### P11. Test strategy

- **Unit tests** run offline against fixtures, with moto-style or in-memory fakes for storage.
- **Contract tests** run the conformance suite against handler responses.
- **Integration-beta and gamma tests** hit deployed resources.
- **Website vs agent equivalence (PLAT-API-01):**
  - In beta and gamma, run against the deployed API using two distinct principals: the website-path role and a stand-in for the FinanceLambdasTool role (a test role with the same policy).
  - The cross-repo version (a real MCP call through Gateway) is ENV-15 in the contracts change.
- **Provider mock** drives throttling, revision, gap, bad-bar, empty, partial and intraday cases. CI never calls the real provider. The optional live `yfinance` shape test (P6a) is opt-in and rate-limited.

## Risks / Trade-offs

- [Single account (decided)] → env-qualified names, env tags, permission boundaries with env-tag denies, separate per-env resources and the gamma isolation suite. Phase 1 uses only synthetic data, and non-synthetic portfolios are rejected by phase scope.
- [Bootstrap with the user's existing, possibly root, credentials] → one-time, approved in principle and confirmed with exact stacks and a cost estimate at run time, creates only scoped automation roles; afterwards only those roles deploy.
- [Budgets data lags actual spend by hours, so the cap action can trigger after the overrun] → no GPU or always-on resources in phase 1. The forecast alert fires at 100%, and paid FinanceModel jobs carry their own pre-flight checks (FinanceModel change).
- [Budget deny policy could lock out the pipeline needed to fix things] → the deny never targets the bootstrap/admin role or read actions. A human detaches it.
- [Synchronous ingestion may exceed API timeouts with a real provider] → phase 1 is fixtures only; PQ-6 decides an asynchronous form before phase 2.
- [`yfinance` is unofficial: rate limits, upstream breakage, terms limited to personal/research use] → adapter boundary with a possible fallback adapter, pinned version, backoff, `empty_response`/`partial_response` flags that block approval of bad snapshots, no retrieved data in the public repo, mock-only CI.
- [Calendar errors misclassify `completed_daily`] → a versioned calendar, finality required from the provider or `finality_inferred` flagged, and a coverage-gap failure instead of guessing.
- [Orphan sweep deleting a slow in-flight commit] → a 24 h grace period, far above the Lambda maximum duration.
- [Excel parser vulnerabilities (XXE, zip bombs)] → XML parsing with external entities disabled, size and ratio limits, and no formula evaluation.
- [Contracts 1.0.0 grows with the review additions (staged manifest, Excel template, quality flags, artifact kinds, tool schemas, route schemas) and is published later] → the platform pins the 0.x pre-release (currently 0.2.2) by exact version and digest; it is beta-only, and gamma and prod still require 1.0.0. Every version reference in this change reads 0.x until 1.0.0 is published.

## Migration Plan

1. Prerequisites (human): the user's in-principle approval of the bootstrap (given 2026-10-07), confirmed by showing the exact stacks and a cost estimate at run time, an authenticated AWS CLI session (existing credentials are acceptable), local untracked configuration naming the account and the chosen existing CodeConnection, and a notification address for the budget.
2. Bootstrap the two account-level stacks, after the STS account and region checks: the pipeline store, then the tooling stack (boundaries, pipeline, budget plus alerts and action), and write the allocation defaults. The contract CodeArtifact repository is added before contracts 1.0.0 is published (P10). Run the source-stage dry run; if it fails, the user extends the GitHub App installation and reruns it.
3. Contracts 0.x to beta (the platform pins 0.2.2 today), then 1.0.0. The platform re-pins 1.0.0 once it is published (task 1.2).
4. The pipeline deploys the platform to beta (storage, metadata, API, ingestion with the fixture provider, scheduler), then gamma, then prod after approval.
5. Consumers integrate in contract order.
6. **Rollback:** redeploy `previous_release_id` (contract D6). Records are never rolled back. Table and record changes are expand-then-contract within a major.
7. **Teardown of beta/gamma:** buckets are emptied by lifecycle expiry and stacks deleted through the pipeline. Prod buckets and tables use `RETAIN` removal policies.

## Open Questions

Deferrable items that do not change specs or tasks. Items registered in the contracts change keep their OQ numbers; resolved items are marked RESOLVED 2026-10-07.

| ID | Question | Blocks | Resolved by | Interim |
|---|---|---|---|---|
| OQ-5 | Instrument and provider (P6) | None | **RESOLVED 2026-10-07:** instrument = S&P 500 tracking-ETF daily series (SPY), daily completed observations only, no intraday; provider = `yfinance` adapter (P6a), pinned, no API key, no retrieved data committed, enabled per environment by phase 2 configuration | Fixture provider and mock in phase 1 and CI |
| OQ-6 | 09:00 vs 09:30 ET | None (configuration) | User | 09:00 default |
| OQ-1 | Account boundary | None | **RESOLVED 2026-10-07:** single account, isolation by naming, tags, permission boundaries and per-env resources (contracts D5). Non-synthetic portfolios remain out of phase 1 by phase scope | n/a |
| OQ-2 | Connection coverage | None (non-blocking) | **RESOLVED 2026-10-07:** reuse an existing AVAILABLE connection via SSM; source-stage dry run at bootstrap; user extends the GitHub App installation only on failure | n/a |
| OQ-11 | Scoped bootstrap role | None | **RESOLVED 2026-10-07:** bootstrap with the user's existing CLI session; refuse-root rule dropped; scoped/MFA role is a later recommendation | n/a |
| OQ-7 | Budget allocation | None | **RESOLVED 2026-10-07:** USD 50 total; default allocation 8/7/5/25/5 in SSM; alerts 50/80/100%; deny action at 100% (P9) | n/a |
| OQ-10 | Website owner | None for phase 1 | User | Website-path client role |
| PQ-1 | Beta SSE-S3 vs per-env KMS key | None | User cost decision with current pricing (within `platform_infra`) | KMS in all envs |
| PQ-2 | Object Lock (governance) for prod before real data | None for phase 1 | Decide before non-synthetic portfolios are allowed | Deny policies |
| PQ-3 | Retention durations per env and bucket | None (configuration) | User / records policy | P2 defaults |
| PQ-4 | Human website authentication to IAM | None for phase 1 | Decide with OQ-10 | Role-based test client |
| PQ-5 | Exchange-calendar source and licence | None | **RESOLVED 2026-10-07:** `exchange_calendars` (alternative `pandas_market_calendars`), XNYS, pinned version; calendar artifact generated at build time; licence recorded at pin time (P5) | Synthetic fixture calendar with the fixture provider |
| PQ-6 | Async ingestion form for real providers | None for phase 1 | Measured `yfinance` latency in beta (phase 2) | Synchronous, retries bounded by the function timeout |
