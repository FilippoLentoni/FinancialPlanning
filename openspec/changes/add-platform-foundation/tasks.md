# Tasks

## Scope and conventions

- **Scope:** phase 1 of the platform, plus the `yfinance` provider adapter and the XNYS calendar (round 2 user decision 2026-10-07; OQ-5 and PQ-5 RESOLVED). Phase 1 deployments and CI use the fixture provider and mock only; `yfinance` is enabled per environment by phase 2 configuration. No model compute, no live trading.
- **Dependencies:** contracts from `establish-cross-repo-contracts` (`finplan-contracts`, pinned by exact version and digest). Until 1.0.0 is published the pin is the 0.x pre-release, currently **0.2.0**, which is beta-only; version references below that name 1.0.0 mean the release the platform re-pins to in task 1.2. Error codes, IDs and SSM paths come from that change.
- **Bootstrap-dependent tasks:** tasks that deploy wait on the bootstrap (task 10.6). The user approved it in principle on 2026-10-07; it runs only after the bootstrap IaC is implemented, with the exact stacks and a cost estimate shown first. No deployment happens during spec work. OQ-11 is resolved (existing CLI credentials) and OQ-2 is non-blocking (existing connection plus source-stage dry run), per contracts D11. Until the bootstrap runs, the tasks before it are verified offline.
- **Test IDs:** STO (platform-storage), MDS (plan-metadata-store), API (plan-lifecycle-api), ING (market-data-ingestion), STG (staged-output-acceptance), XLS (excel-plan-import), COST (platform-cost-guardrails) and PIPE (platform-pipeline). See the mapping table at the end.

## 1. Service scaffolding and environment configuration

- [x] 1.1 Create the service layout and verify that `pytest` and `cdk synth` both run on an empty skeleton offline:
  - `platform/core/` (operations)
  - `platform/handlers/` (API, scheduler, sweep adapters)
  - `platform/providers/`
  - `platform/excel/`
  - `infra/` (CDK app: tooling, storage, metadata, api, ingestion stacks)
  - `config/{beta,gamma,prod}.json`
  - `tests/{unit,contract,integration,smoke}/`
- [ ] 1.2 Pin `finplan-contracts` 1.0.0 by exact version and digest once it is published (until then the pin is the 0.x pre-release, currently 0.2.0, beta-only; the pin file, `scripts/check_contracts_pin.py` and the digest check are in place). Verify that a digest mismatch fails the build (CS-04 consumer case).
- [x] 1.3 Define the environment configuration schema: region, ingest schedule (`09:00`/`09:30`, default `09:00`), phase (`1` or `2`), provider (`fixture` in phase 1; `fixture` or `yfinance` in phase 2), provider rate-limit and backoff settings, settle delay, dataset (`etf-daily` with the configured S&P 500 tracking-ETF ticker, daily only), retention values, staging window, size limits and consumer principal reference keys. All values must be placeholders or SSM keys, never identifiers. Verify unit tests that reject an invalid schedule value (ING-02), a non-fixture provider in a phase 1 configuration (ING-10) and an intraday granularity (ING-13), and that the leak scan passes on `config/`.

## 2. Platform storage

- [x] 2.1 Implement the storage stack: one KMS key per environment, six buckets with Block Public Access, BucketOwnerEnforced, versioning, Bucket Keys, and TLS-only and key-enforcement policies, with tags and the `config/bucket-*` SSM outputs. Verify STO-01, STO-02 and STO-05 with CDK assertion tests on the synthesized template.
- [x] 2.2 Add the bucket policies: write-once deny on delete and unconditional put for application roles, the FinanceModel job-role read grant conditioned on the approved-snapshot tag, and staging write-only by `run_id` prefix. Verify STO-03 and STO-04 with IAM policy-simulation unit tests (cross-env denied, staging read denied, delete denied).
- [x] 2.3 Implement the write-once artifact writer (conditional create, SHA-256 returned). Verify STO-04 unit tests: a second write to the same key fails and the stored checksum is unchanged.
- [x] 2.4 Add the lifecycle rules from configuration (P2 table) and the expired-snapshot catalog sweep. Verify STO-06 with template assertions per environment (beta/gamma expiry present, prod `plans` has no expiry when unset) and a unit test that an expired catalog read returns `NOT_FOUND`.
- [x] 2.5 Add the Excel upload prefix policy and the presigned-POST grant generator (size range, 15-minute expiry). Verify STO-07 with a unit test that an expired grant is refused.
- [x] 2.6 Document the bucket roles, key layout and retention defaults (PQ-1..PQ-3 marked open) in `docs/storage.md`. Verify that the leak scan passes on the document.

## 3. Metadata store

- [x] 3.1 Implement the metadata stack: on-demand tables (`portfolio`, `plan`, `plan_version`, `publication`, `execution`, `snapshot_catalog`, `staged_output`, `idempotency` with TTL, `audit_event`), each with PITR and KMS. Verify MDS-07 template assertions and the MDS-01 policy simulation (FinanceLambdasTool and FinanceModel roles denied).
- [x] 3.2 Implement the transactional repository layer: version create with head move, idempotency record and audit event in one transaction, and mapping of cancellation reasons to `CONFLICT`, replay or `IDEMPOTENCY_KEY_REUSED`. Verify MDS-03 and MDS-05 unit tests against a local DynamoDB-compatible fake: concurrent `expected_revision` produces exactly one winner, and a failed idempotency condition commits nothing.
- [x] 3.3 Implement conditional status transitions with audit events, and the immutable-content guard. Verify MDS-04 unit tests (`invalid` to `validated` refused; content edit returns `IMMUTABLE_RECORD`).
- [x] 3.4 Implement artifact-before-metadata ordering and the orphan sweep (24-hour grace). Verify MDS-02 with a fault-injection unit test (crash after artifact write leaves no visible version, and the sweep removes the orphan after the grace period).
- [x] 3.5 Implement append-only audit events with caller, `correlation_id` and prior and new state. Verify the MDS-06 unit test and a policy simulation showing that no application role can update or delete audit items.

## 4. Plan lifecycle API: portfolios, plans and versions

- [x] 4.1 Implement the API stack: REST API with IAM auth, a resource policy limited to same-environment configured principals, the plan-api Lambda, and the `api/plan-endpoint` SSM output. Verify the API-01 unauthenticated and cross-env denial template and policy tests.
- [x] 4.2 Implement request and response validation through the pinned contract validators, the error envelope with `correlation_id`, and `UNSUPPORTED_CONTRACT_VERSION`. Verify the API-02 contract tests: the contract conformance suite in consumer mode for the pinned version, plus response validation of every route against its contract (or platform) schema.
- [x] 4.3 Implement create portfolio (synthetic only) and create plan. Verify the API-03 unit tests (`synthetic: false` returns `OPERATION_NOT_PERMITTED`).
- [x] 4.4 Implement root and child version creation (rejecting client-supplied IDs and cross-plan parents, JCS SHA-256 checksum, `no_effect` flag). Verify the API-04 unit and contract tests, including no-effect checksum equality.
- [x] 4.5 Implement checksum-bearing reads and trusted artifact references with time-limited download grants. Verify API-09: the downloaded bytes hash to the reference checksum, and no bucket or key appears in any response (contract test scanning responses).
- [x] 4.6 Enforce `idempotency_key` and `expected_revision` on all writes. Verify the API-08 unit tests (missing key rejected, duplicate returns the original, reused key with a different body fails).
- [x] 4.7 Document the API routes, auth and error mapping in `docs/plan-api.md`. Verify that the documented examples validate with the contract validators.
- [x] 4.8 Implement the review-added reads: portfolio, plan head, paginated version list, bounded observation read and staged-output outcome, with per-route grants for the FinanceLambdasTool role classes and the FinanceModel read-only roles. Verify API-11 (stable pagination, head matches website-path read) and API-12 (uncovered range reported, no fabricated observations) unit and contract tests and the API-01 policy simulation (tool `reader` denied write routes; FinanceModel roles denied every write route).

## 5. Validation, publication and execution

- [x] 5.1 Implement deterministic validation (schema, reconciliation tolerance, constraints, snapshot coverage, `validation_ruleset_version`). Verify the API-05 unit tests (1.07 sum is `invalid`; a repeated validation yields an identical result and no new transition).
- [x] 5.2 Implement publish (only validated versions, records the checksum, publication-head `expected_revision`, prior publication untouched). Verify the API-06 unit tests (unvalidated returns `PRECONDITION_FAILED`; concurrent publish yields one `CONFLICT`).
- [x] 5.3 Implement execution recording (`paper`/`simulated` only, `live` returns `OPERATION_NOT_PERMITTED`, `publication_superseded` flag, publication and version untouched). Verify the API-07 unit tests (publication versus execution).
- [x] 5.4 Implement schema-upgrade handling: stored `contract_version`, read-time defaults, no rewrite. Verify API-10 with contract tests over the package's schema-upgrade fixtures (a 1.0.0 record read by a 1.1.0 build keeps the same checksum).

## 6. Market-data ingestion

- [x] 6.1 Implement the versioned session calendar loader, the build-time generator from the pinned `exchange_calendars` library (XNYS), and the synthetic fixture calendar (at least one holiday and one early close). Verify ING-15 (calendar version names XNYS, library and version; early close honoured; unpinned library fails the build). Verify the ING-03 unit tests (holiday is `no_session` with no provider call; out-of-coverage returns `PRECONDITION_FAILED`).
- [x] 6.2 Implement the provider adapter interface (`describe`/`fetch`), the deterministic fixture provider (`synthetic: true`, daily only) and the programmable mock (including empty and partial responses). Verify the ING-05 unit tests (intraday request to the daily-only provider is refused; throttled returns `RATE_LIMITED`, retryable, with no snapshot).
- [x] 6.3 Implement normalization to the finance observation schema and validation into quality flags and rejections. Verify the ING-06 unit tests (missing session, bad OHLC, raw retention of rejected records).
- [x] 6.4 Implement observation-kind classification (`completed_daily` versus `intraday_partial`, early closes, `finality_inferred`). Verify the ING-04 unit tests with a frozen clock at 09:00 ET on a trading day, an early-close day and an intraday mock.
- [x] 6.5 Implement dedupe persistence of curated observations (conditional create per key, `source_revised` revisions). Verify the ING-07 unit tests (re-ingest yields one object; a revised close keeps both values).
- [x] 6.6 Implement snapshot commit (write-once manifest and payload, catalog row with all result fields, `no_new_observations`) and immutability. Verify the ING-08 unit and contract tests (result validates; same content at a later retrieval produces a new ID with the same content checksum; snapshot artifacts cannot be overwritten).
- [x] 6.7 Implement idempotent triggers: required key on demand, deterministic `sched-<env>-<dataset>-<date>` key for the schedule. Verify the ING-09 unit test (duplicate scheduler delivery yields one snapshot and the same ID).
- [x] 6.8 Wire both triggers to one operation: the `POST /v1/ingestions` route and an EventBridge Scheduler schedule (`America/New_York`, weekdays, from configuration, retry and DLQ, DLQ alarm), plus the `api/ingestion-endpoint` and `config/ingest-schedule` SSM outputs. Verify ING-01 (same normalized checksums from both trigger paths in a unit harness) and the ING-02 template assertions (time zone and cron per configured time).
- [x] 6.9 Register dataset identities (`etf-daily`, `index-level`, `universe`) with only the `etf-daily` dataset for the configured S&P 500 tracking ETF enabled (fixture-backed in phase 1). Verify the ING-11 unit test (index request returns `VALIDATION_FAILED`) and ING-13 (fixture run yields `completed_daily` ETF observations; intraday configuration fails the build).
- [x] 6.10 Document ingestion semantics, the initial-instrument decision (S&P 500 tracking-ETF daily series, daily only), the `yfinance` provider decision and its recorded caveats (unofficial, no API key, rate-limited, personal/research terms, no committed data), the XNYS calendar source, OQ-5 and PQ-5 as RESOLVED 2026-10-07, and PQ-6 in `docs/ingestion.md`. Verify the document states no price and no provider capability beyond the recorded caveats (review checklist) and passes the leak scan.
- [x] 6.12 Implement the `yfinance` provider adapter (daily only, OHLCV plus adjusted close, dividends and splits, exact version pin, settle-delay finality) against a mocked library interface and synthetic shape fixtures. Verify ING-14 (normalization of adjusted close, dividend and split fields; unpinned version fails the build; `finality_inferred`).
- [x] 6.13 Implement adapter rate limiting (minimum request interval) and retry with exponential backoff and jitter bounded by the function timeout. Verify ING-17 (throttled-then-success commits one snapshot; exhausted attempts return `RATE_LIMITED`, retryable, with no snapshot).
- [x] 6.14 Implement `empty_response` and `partial_response` quality flags, with `empty_response` added to the approval rule's blocking set. Verify ING-18 (empty response stays `committed` and the FinanceModel read is denied; missing adjusted close yields `partial_response`).
- [x] 6.15 Record provider lineage (`lineage.provider`, `provider_library`, `library_version`, retrieval timestamp) and the calendar version in each manifest and return it from results and snapshot reads. Verify ING-16 contract tests against the contract lineage fields (pinned 0.x; `lineage.provider` is the provider identifier).
- [x] 6.16 Add the build-stage data-hygiene check (fixtures must carry `synthetic: true` and must not match the real-data signature list) and a check that no pipeline test suite enables the real provider. Verify ING-19 with positive and negative fixtures.
- [ ] 6.17 Package the ingestion function with the pinned `yfinance` dependency (container-image Lambda if the zip limit is exceeded) and add the opt-in, rate-limited live shape test (one small request; asserts columns, types and calendar alignment, never values; excluded from CI and prod smoke). Verify ING-19 (excluded from pipeline suites) and a local opt-in run by a human.
- [ ] 6.18 Phase 2 enablement (after the beta phase 1 release): set beta configuration to phase 2 with provider `yfinance`, deploy through the pipeline, then gamma and prod after approval. Verify ING-10 (phase 2 scenario) and a beta scheduled run whose snapshot shows `yfinance` lineage and no blocking flags.
- [x] 6.11 Implement snapshot status transitions (`committed`, `approved`, `expired`) with the versioned approval rule, the audit event and the object tag that conditions the FinanceModel read grant. Verify ING-12 unit tests and the STO-03 policy simulation (unapproved snapshot read denied).

## 7. Staged-output acceptance

- [x] 7.1 Implement the staging reference output (`config/run-staging-ref`) and the manifest-present precondition. Verify the STG-01 policy simulation and the STG-02 unit test (no manifest returns `PRECONDITION_FAILED` `staged_output_incomplete`).
- [x] 7.2 Implement the structural checks: file checksums, unlisted files, manifest schema, registry lookup of `run_id`/`model_version`, snapshot existence, and `DEPENDENCY_UNAVAILABLE` when there is no FinanceModel release. Verify the STG-03 unit tests with a mocked registry reference.
- [x] 7.3 Implement outcome-aware handling (failed, cancelled, timed out, infeasible or unbounded produce no version). Verify the STG-04 unit tests over the contract partial-output, failed and infeasible fixtures.
- [x] 7.4 Implement the commit and validation gate (copy to `plans`, version with origin `model_run`, validation, at most one version per run). Verify STG-05 (partial output is `invalid` and unpublishable) and the STG-06 unit tests (duplicate `run_id` under a new key returns `CONFLICT`; a head move returns `CONFLICT` and stays retryable).
- [x] 7.5 Document the worker-facing staging protocol (manifest last, fields, outcomes) for FinanceModel in `docs/staging.md`. Verify the example manifest validates against the pinned contract `core/v1/staged-output-manifest.json` schema (added by the cross-repo review, contracts D10; pinned 0.x until 1.0.0 is published).

## 8. Excel import and export

- [x] 8.1 Implement template export `xlsx-plan-v1` (`meta` and `allocations` sheets, no macros, formulas or links). Verify the XLS-01 unit test: re-parsing the export yields the source content and the checksum matches.
- [x] 8.2 Implement package pre-checks: `.xlsx` only, VBA/ActiveX/OLE/externalLink rejection, size and decompression-ratio limits, XML parsing with external entities disabled. Verify the XLS-02 and XLS-03 unit tests with crafted fixtures (VBA in `.xlsx`, zip bomb, external link), and that no network call occurs (socket-blocked test).
- [x] 8.3 Implement the data-only parser and canonical mapping into the create-version operation (formula cells rejected unless marked formula-tolerant). Verify the XLS-03 and XLS-04 unit tests (formula cell rejected; unknown instrument named by sheet and row; stale revision returns `CONFLICT`).
- [x] 8.4 Implement source-file lineage (`uploads/excel/<sha256>.xlsx`, trusted ref kind `excel_source`, rejected uploads retained with a rejection record) and idempotent commit. Verify the XLS-05 and XLS-06 unit tests.
- [x] 8.5 Document the template columns and safety rules in `docs/excel.md`. Verify that the documented sample workbook (generated by the export, `synthetic: true`) imports successfully in the unit suite.

## 9. Cost guardrails

- [x] 9.1 Add the account-level budget to the tooling stack: limit from `/finplan/shared/financialplanning/config/cost-ceiling-usd` (default 50), alerts at 50%, 80% and 100% actual and 100% forecast, and an SNS topic whose subscriber is set by a human. Verify the COST-01 template assertions.
- [x] 9.1a Write the default allocation `/finplan/shared/financialplanning/config/budget-allocation` (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5) in the bootstrap when absent, preserve user-set values, and reject sums above the ceiling. Verify the COST-06 unit tests (defaults written; user value preserved; sum 60 rejected) against the contracts allocation schema.
- [x] 9.2 Add the budget action with the generated deny policy and role names from configuration, plus the budget-state flag writer. Verify the COST-02 policy simulation (start job, Bedrock invoke and start pipeline denied; plan reads allowed; bootstrap role unaffected).
- [x] 9.3 Implement the build-stage cost checks: required tags, and no instances, NAT, endpoints, provisioned tables or GPU. Verify the COST-03 and COST-04 unit tests with fixture templates.
- [x] 9.4 Implement the ingestion budget pre-check. Verify the COST-05 unit test (flag set returns `BUDGET_EXCEEDED`, with no provider call and no snapshot).

## 10. Pipeline and bootstrap

- [x] 10.1 Implement the pipeline stack per the contract standard: source, build/test, beta, gamma, approval, prod/smoke, with artifact-only promotion and the `rollback_to_release_id` parameter. Verify the PIPE-01 and PIPE-03 checks with the contract pipeline-structure check (ENV-09) on the synthesized template.
- [x] 10.2 Wire the build-stage gates: unit, conformance, ownership, leak scan, live-permission scan, cost checks and configuration checks. Verify PIPE-02 by feeding a fixture commit containing an account-ID pattern and confirming the build fails.
- [x] 10.3 Publish the release manifest, `current-release-id` and all platform SSM outputs after each deploy. Verify the PIPE-04 unit test of the manifest builder against the contract manifest schema.
- [ ] 10.4 Implement the prod smoke suite on the synthetic smoke portfolio. Verify PIPE-05 locally against a fixture deployment double, then in prod (BLOCKED by 10.6).
- [x] 10.5 Write the bootstrap runbook section for this repo (in-principle approval of 2026-10-07 with the exact stacks and a cost estimate shown before running, existing authenticated CLI session, no refuse-root check but a printed scoped/MFA-role recommendation, account and region match against local untracked configuration, CodeConnection reference written to SSM, scoped roles created, source-stage dry run before deploy stages, budget notification address supplied by a human, allocation defaults). Verify PIPE-07 with mocked STS, connection and pipeline responses (root caller proceeds; dry-run failure stops with the extend-installation message). No AWS mutation runs in tests.
- [ ] 10.6 Run the bootstrap, which deploys two account-level stacks: `finplan-shared-financialplanning-pipeline-store`, then `finplan-shared-financialplanning-tooling` (two stacks because the tooling template exceeds CloudFormation's inline size limit and is staged in the store bucket). Verify the source-stage dry run fetched `main`, the pipeline exists and its first run reaches beta (approved in principle 2026-10-07: runs only after the bootstrap IaC is implemented, with the exact stacks and a cost estimate shown first; if the dry run fails, the user extends the GitHub App installation and reruns it).

## 11. Environment integration checks (after bootstrap)

- [ ] 11.1 Beta integration suite against deployed beta resources (BLOCKED by 10.6):
  - create, override, validate, publish and paper-execute;
  - concurrent overrides;
  - duplicate requests;
  - scheduled and on-demand ingestion via the fixture provider;
  - staged-output fixtures (valid, partial, failed, infeasible);
  - Excel round trip.

  Verify that every integration-beta row in the mapping table passes.
- [ ] 11.2 Website versus agent equivalence in beta and gamma: the website-path role and the tool-role stand-in read the same version. Verify API-01 (identical `plan_version_id`, checksum and bytes).
- [ ] 11.3 Gamma isolation and production-like suite: gamma roles are denied prod buckets, tables and API, and the full lifecycle runs on gamma. Verify the gamma rows plus contract ENV-03.
- [ ] 11.4 Rollback drill in gamma (redeploy previous `release_id`, read records written by the newer release). Verify PIPE-06 and contract ENV-11.
- [ ] 11.5 After prod approval, run smoke and confirm digest equality across environments. Verify PIPE-03 and PIPE-05, and contract ENV-10.

## 12. Contracts 0.2.0 re-pin (local, no AWS)

- [x] 12.1 Re-pin `finplan-contracts` 0.2.0 (`scripts/check_contracts_pin.py --repin`; `uv sync --locked`). Verify the pin check with `--rebuild` (the wheel rebuilt from `contracts/python` is byte-identical to the pinned digest).
- [x] 12.2 Validate the create portfolio, create plan, create root version and record execution requests and responses, the observation read response, the staged-output GET response and the ingestion response (including the holiday answer with no snapshot) with the contract route schemas; serve `publication_revision` as the contract plan field; give every route a response schema. Verify API-02 contract tests and the route-table test that no route lacks a response schema.
- [x] 12.3 Remove `scripts/ownership_known_gaps.json` and every code path that accepted ownership problems; tag the per-environment deploy, execution and stage roles with their environment and bound them by its boundary (contracts D13, ENV-21). Verify the ownership, boundary and shared-resource checks report zero problems on all 14 synthesized templates without the pseudo-parameter workaround.
- [x] 12.4 Apply the contract's CloudFormation-ready boundary patterns. Verify `uvx cfn-lint` over every synthesized template (including the nested stage assemblies) reports no errors and no `E3510`.
- [x] 12.5 Mark the FinanceModel job and job-API role references as registered contract keys and reject a reference marked registered that the contract does not register. Verify the configuration unit tests.

## Requirement-to-test mapping

Test types: unit, contract (schema and conformance with package fixtures), integration-beta, gamma and smoke. All pipeline tests use synthetic fixtures or the provider mock. The real provider is never called in CI; the optional live `yfinance` shape test is opt-in and outside the pipeline.

| Spec | Requirement | Test ID | Type |
|---|---|---|---|
| platform-storage | Per-environment artifact buckets | STO-01 | unit (synth assertions) + integration-beta |
| platform-storage | Encryption at rest and in transit | STO-02 | unit + integration-beta (non-TLS and other-key put denied) |
| platform-storage | No public access and least-privilege grants | STO-03 | unit (policy simulation) + gamma (cross-env denied) |
| platform-storage | Write-once immutable artifacts | STO-04 | unit + integration-beta (**immutable snapshots**) |
| platform-storage | Object versioning is protection only | STO-05 | unit + contract (no version IDs in responses) |
| platform-storage | Per-environment retention and lifecycle | STO-06 | unit (synth assertions) + integration-beta (staging expiry rule present) |
| platform-storage | Excel uploads isolated in raw storage | STO-07 | unit + integration-beta |
| plan-metadata-store | Authoritative metadata records | MDS-01 | unit (policy simulation) + contract |
| plan-metadata-store | Artifact-before-metadata commit ordering | MDS-02 | unit (fault injection) |
| plan-metadata-store | Atomic version creation with head move | MDS-03 | unit + integration-beta (**concurrency**) |
| plan-metadata-store | Conditional status transitions | MDS-04 | unit |
| plan-metadata-store | Idempotency store | MDS-05 | unit + integration-beta (**duplicate requests**) |
| plan-metadata-store | Append-only audit events | MDS-06 | unit + integration-beta |
| plan-metadata-store | Point-in-time recovery and environment isolation | MDS-07 | unit + gamma |
| plan-lifecycle-api | Single plan API for all clients | API-01 | integration-beta + gamma (**website vs agent same `plan_version_id`/checksum**) |
| plan-lifecycle-api | Contract conformance of every operation | API-02 | contract |
| plan-lifecycle-api | Create portfolio and plan | API-03 | unit + integration-beta |
| plan-lifecycle-api | Root and child plan versions | API-04 | unit + contract + integration-beta |
| plan-lifecycle-api | Deterministic validation | API-05 | unit |
| plan-lifecycle-api | Publish an exact validated version | API-06 | unit + integration-beta (**concurrency**) + smoke |
| plan-lifecycle-api | Executions recorded separately | API-07 | unit + integration-beta + smoke (**publication vs execution**) |
| plan-lifecycle-api | Idempotency and concurrency on all writes | API-08 | unit + integration-beta (**duplicate requests**) |
| plan-lifecycle-api | Checksum-bearing reads and trusted references | API-09 | contract + integration-beta + smoke |
| plan-lifecycle-api | Schema upgrade compatibility | API-10 | contract (**schema upgrade**) + gamma |
| plan-lifecycle-api | Plan head and version list reads | API-11 | unit + contract + integration-beta |
| plan-lifecycle-api | Snapshot observation reads | API-12 | unit + contract + integration-beta |
| market-data-ingestion | One ingestion operation, two triggers | ING-01 | unit + integration-beta |
| market-data-ingestion | Daily schedule in America/New_York | ING-02 | unit (config and synth) + integration-beta (schedule present) |
| market-data-ingestion | Exchange session calendar | ING-03 | unit |
| market-data-ingestion | Completed daily observations versus intraday bars | ING-04 | unit (frozen clock) |
| market-data-ingestion | Declared provider capabilities | ING-05 | unit (mock) |
| market-data-ingestion | Normalization and validation | ING-06 | unit |
| market-data-ingestion | Deduplicated persistence | ING-07 | unit + integration-beta |
| market-data-ingestion | Immutable snapshot results | ING-08 | contract + integration-beta (**immutable snapshots**) |
| market-data-ingestion | Idempotent triggers | ING-09 | unit + integration-beta (**duplicate requests**) |
| market-data-ingestion | Phase 1 fixture provider | ING-10 | unit (config gate) + smoke (synthetic flag) |
| market-data-ingestion | Separate datasets for index, ETF and constituents | ING-11 | unit |
| market-data-ingestion | Initial instrument is an S&P 500 tracking-ETF daily series | ING-13 | unit (config gate) + integration-beta |
| market-data-ingestion | Snapshot approval status | ING-12 | unit + integration-beta (FinanceModel read of unapproved snapshot denied) |
| market-data-ingestion | yfinance provider adapter | ING-14 | unit (mocked library, synthetic shape fixtures) |
| market-data-ingestion | XNYS session calendar from a pinned library | ING-15 | unit (generator + build check) |
| market-data-ingestion | Provider lineage with library version | ING-16 | contract + integration-beta (phase 2) |
| market-data-ingestion | Provider rate limiting and backoff | ING-17 | unit (mocked library, fake clock) |
| market-data-ingestion | Empty and partial provider responses are quality flags | ING-18 | unit + integration-beta (mock provider) |
| market-data-ingestion | No retrieved market data in the repository | ING-19 | unit (data-hygiene check) + optional opt-in live shape test |
| staged-output-acceptance | Platform-owned run-output staging area | STG-01 | unit (policy simulation) + integration-beta |
| staged-output-acceptance | Staged output manifest written last | STG-02 | unit |
| staged-output-acceptance | Structural acceptance checks | STG-03 | unit + integration-beta |
| staged-output-acceptance | Outcome-aware acceptance | STG-04 | unit + contract (**partial/failed output**) |
| staged-output-acceptance | Validation gate before plan-version commit | STG-05 | unit + integration-beta (**partial/failed output**) |
| staged-output-acceptance | Idempotent and concurrency-safe acceptance | STG-06 | unit + integration-beta (**duplicate requests**, **concurrency**) |
| staged-output-acceptance | Phase 1 fixture worker output | STG-07 | integration-beta + gamma |
| excel-plan-import | Template export for round trips | XLS-01 | unit |
| excel-plan-import | Accepted formats only | XLS-02 | unit |
| excel-plan-import | No code or formula execution | XLS-03 | unit (socket-blocked) |
| excel-plan-import | Canonical contract mapping | XLS-04 | unit + integration-beta (**Excel override round trip**) |
| excel-plan-import | Source file lineage | XLS-05 | unit + integration-beta |
| excel-plan-import | Idempotent import | XLS-06 | unit (**duplicate requests**) |
| platform-cost-guardrails | Project budget at the cost ceiling | COST-01 | unit (synth assertions) |
| platform-cost-guardrails | Default budget allocation | COST-06 | unit (bootstrap allocation writer) |
| platform-cost-guardrails | Enforcement action at the cap | COST-02 | unit (policy simulation) |
| platform-cost-guardrails | Cost-allocation tagging | COST-03 | unit |
| platform-cost-guardrails | No always-on compute in phase 1 | COST-04 | unit |
| platform-cost-guardrails | Budget pre-check for on-demand ingestion | COST-05 | unit + integration-beta (flag set in beta only) |
| platform-pipeline | Platform pipeline stages | PIPE-01 | unit (pipeline structure check) |
| platform-pipeline | Build-stage gates | PIPE-02 | unit |
| platform-pipeline | Same artifacts promoted | PIPE-03 | smoke (digest equality) |
| platform-pipeline | Published references and manifest | PIPE-04 | unit + integration-beta (SSM resolution) |
| platform-pipeline | Environment tests per stage | PIPE-05 | integration-beta + gamma + smoke |
| platform-pipeline | Rollback without rebuild | PIPE-06 | gamma (rollback drill; **schema upgrade**) |
| platform-pipeline | Pipeline bootstrap prerequisites | PIPE-07 | unit (mocked STS, connection and source-stage dry run; stacks and cost estimate shown before running) + bootstrap dry run |
| platform-pipeline | Account-level bootstrap stacks | PIPE-08 | unit (bootstrap plan names the two stacks; tooling template role tags and boundaries, ownership and boundary checks) |

Required test themes and where they are covered:

| Theme | Test IDs |
|---|---|
| immutable snapshots | STO-04, ING-08 |
| duplicate requests | MDS-05, API-08, ING-09, STG-06, XLS-06 |
| concurrency | MDS-03, API-06, STG-06 |
| partial/failed output | STG-04, STG-05 |
| Excel override round trip | XLS-04 |
| publication vs execution | API-07 |
| schema upgrade | API-10, PIPE-06 |
| website vs agent | API-01 (cross-repo: contract ENV-15) |

## Workflow follow-up

- The contract gaps this change first recorded (staged-output manifest, Excel template and import report, quality flags, artifact kinds, budget role names, the `shared` runtime writer) were resolved in `establish-cross-repo-contracts` D4/D10 and are part of the contract package, which tasks 6.6, 7.2 and 8.3 pin (0.x until 1.0.0 is published).
- The gaps found by verifying the synthesized templates against contracts 0.1.0 (ownership matrix rows, route schemas, `publication_revision`, the holiday ingestion response, the boundary resource patterns, the FinanceModel role keys) were resolved in contracts 0.2.0 (contracts D13) and applied here in section 12. The contract CodeArtifact repository is still not declared (design P10).
- The `yfinance` adapter and XNYS calendar are now in this change (OQ-5 and PQ-5 RESOLVED 2026-10-07); phase 2 enablement is task 6.18. The async ingestion form (PQ-6) is decided from measured beta latency; a fallback adapter (for example Stooq via `pandas-datareader`) would be a separate change with no contract change.
- Archive this change after the phase 1 end-to-end check (contract ENV-15) passes in gamma and the prod smoke passes.
