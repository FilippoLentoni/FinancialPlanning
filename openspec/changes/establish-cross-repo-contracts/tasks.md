# Tasks

Scope: deliverables that FinancialPlanning owns for the cross-repo contract baseline: the contract package, conformance tooling, ownership matrix, release-manifest/SSM convention, isolation guardrails, the pipeline standard and the bootstrap runbook. Platform storage/APIs/ingestion belong to the separate platform implementation change. Steps that need a human or a BLOCKER (design.md, Open Questions) say so explicitly. No task deploys anything during spec work. The user approved the one-time bootstrap in principle on 2026-10-07 (D12); it runs only after the bootstrap IaC is implemented, after the exact stacks and a cost estimate are shown. The bootstrap uses the user's existing authenticated AWS CLI session (OQ-11 resolved) and reuses an existing CodeConnection verified by a source-stage dry run (OQ-2 non-blocking); see design D11.

## 1. Contract package scaffolding and ownership matrix

- [x] 1.1 Create the `contracts/` layout (`core/v1/`, `finance/v1/`, `fixtures/`, `ownership/`, `conformance/`, `python/`, `typescript/`) with a single version file; verify that a package build produces empty Python and npm artifacts carrying the same version
- [x] 1.2 Encode the D1 ownership matrix as machine-readable `contracts/ownership/matrix.yaml` (resource kind/logical name → owner, consumers, reference key), listing external resources as `external`; verify with test OWN-02 that every D1 row is present and each resource has exactly one owner
- [x] 1.3 Implement the ownership check that compares a synthesized CloudFormation template against the matrix; verify OWN-01 (non-owner declaration fails; unmapped resource fails) with fixture templates
- [x] 1.4 Document the matrix and how consumers reference resources in `contracts/README.md`, linking to this change; verify the README contains no account IDs, ARNs, bucket or role names (leak scan from 6.2 passes)
- [x] 1.5 Attribute CDK-generated helper resources in the ownership check: each helper (`AWS::IAM::Policy` on an owned role, `AWS::Lambda::Permission`, `AWS::CDK::Metadata`, custom-resource provider functions/roles, log-retention helpers) is attributed to its parent construct's owner or to an explicit, reviewed `cdk_generated_helpers.allow_list` entry in `matrix.yaml`, never silently ignored; verify OWN-09 with fixture templates (owned-parent helpers and allow-listed providers pass and are listed in the report; a helper of another repo's resource, a helper with no parent and an unlisted provider fail; removing the `AWS::CDK::Metadata` entry fails; an entry without reason or review is rejected)

## 2. Identifier schemas and canonicalization

- [x] 2.1 Add `core/v1/identifiers.json` with prefix + ULID patterns for all nine identifiers and the `cfg_` + 64-hex pattern; verify ID-01 valid/invalid fixtures, including the wrong-prefix case returning `INVALID_IDENTIFIER`
- [x] 2.2 Implement RFC 8785 canonicalization + SHA-256 `configuration_id` helpers in Python and TypeScript; verify ID-02 (key-order invariance, value sensitivity) and that both languages produce identical IDs for the shared fixture set
- [x] 2.3 Add idempotency (`idempotency_key` pattern, request-hash definition) and concurrency (`revision`, `expected_revision`) schema fragments; verify fixtures for duplicate, reused-key and conflict cases validate

## 3. Plan lifecycle and snapshot schemas

- [x] 3.1 Add schemas for portfolio, plan, plan version (lineage fields, origin enum, status enum, checksum), publication and execution (mode enum restricted to `paper`/`simulated`); verify ID-06..ID-09 fixtures, including that a `live` execution mode fails validation
- [x] 3.2 Add the input snapshot metadata schema (manifest checksum, source/retrieval timestamps, coverage, quality flags, dataset identity, domain); verify ID-05 fixtures
- [x] 3.3 Add the trusted artifact reference schema and reject `s3://` or path-like inputs; verify CS-08 fixtures
- [x] 3.4 Write the identifier lifecycle and minting-authority section of `contracts/README.md` (who mints what, immutability, override = child version, publication vs execution); verify by review against the platform-identifiers spec and that docs examples validate with the validators

## 4. Error and outcome conventions

- [x] 4.1 Add `core/v1/error.json` (envelope) and `core/v1/error-codes.json` (code → default `retryable`); verify CS-05/CS-06 fixtures for every registered code
- [x] 4.2 Add the job/run result schema with separate `completion_status` and `solution_status`; verify CS-07 fixtures (infeasible = succeeded + infeasible; crash = failed + error; no-effect)
- [x] 4.3 Add the capability-description and job submission/status schemas used by tool wrappers; verify valid/invalid fixtures

## 5. Domain-adapter boundary

- [x] 5.1 Split the envelope schemas (`core/v1`) from the finance payloads (`finance/v1`) and add the domain registry with `finance` as its only entry; verify DOM-01/DOM-02 fixtures (finance payload passes, unregistered `supply_chain` fails)
- [x] 5.2 Add finance payload schemas (instruments, allocations, constraints, fees, observation kind `completed_daily`/`intraday_partial`, explanation evidence); verify DOM-03/DOM-04 fixtures
- [x] 5.3 Implement the domain-neutrality check (no finance terms in `core/v1` schemas, driven by a deny-list in the finance adapter); verify it fails on a fixture that adds `ticker` to an envelope

## 6. Validators, fixtures and conformance tooling

- [x] 6.1 Generate deterministic synthetic fixtures for every schema (valid, invalid, duplicate, conflict, partial-output, infeasible, no-effect, schema-upgrade) flagged `synthetic: true`; verify CS-09 (hygiene) and the CS-02 inventory check
- [x] 6.2 Implement the identifier/secret leak scan (account-ID, ARN, bucket-name and key patterns with a placeholder allow-list such as `<account-id>`); verify OWN-03 and ENV-08 with positive and negative fixtures and run it over this repo
- [x] 6.3 Implement the copied-`$id` detector for consumer repos; verify CS-01 with a fixture consumer tree
- [x] 6.4 Implement the live-financial permission policy scan (trading/payment/wallet actions and secret references); verify ENV-05 with fixture policies
- [x] 6.5a Package Python and TypeScript validators and the `finplan-conformance` runner (producer and consumer modes); verify CS-10 by running the suite against fixtures in both languages locally (offline, both-language conformance runner)
- [ ] 6.5b Run the same both-language conformance suite in the FinancialPlanning CodeBuild build stage; verify CS-10 from the build log (waits on the bootstrap, task 10.5, approved in principle 2026-10-07, D12)
- [x] 6.6 Document local, credential-free direct testing with fixtures (for colleagues testing Lambdas before Gateway); verify the documented command runs offline

## 7. Versioning, compatibility gate and publication

- [x] 7.1 Implement the schema compatibility gate (diff against the previous published version; breaking diffs require a major bump; additive changes are minor); verify CS-03 and OWN-07 with fixture schema pairs
- [x] 7.2 Compute SHA-256 digests for Python, npm and tarball outputs and emit them into the build's release metadata; verify by rebuilding the same commit and getting identical digests (CS-04 unit)
- [ ] 7.3 Implement the publish step to the CodeArtifact repository referenced by `/finplan/shared/financialplanning/contract/registry-ref`, refusing to overwrite an existing version; verify CS-04 in beta once bootstrap is done (waits on the bootstrap, task 10.5, approved in principle 2026-10-07, D12)
- [ ] 7.4 Document consumer pinning (exact version + digest, recorded in the manifest) and the 0.x-beta-only rule; verify the documentation's example pin resolves in beta

## 8. Release manifest and SSM convention

- [x] 8.1 Add `core/v1/release-manifest.json` (fields from the spec plus `served_contract_majors`, `approved_by`, `approved_at`, `rolled_back_from`); verify ENV-06 fixtures
- [x] 8.2 Implement manifest write/read helpers that enforce `/finplan/<env|shared>/<repo>/<category>/<name>` and the category enum; verify ENV-07 unit tests (valid paths, rejected categories, rejected cross-repo segment)
- [ ] 8.3 Provide an IAM policy fragment generator that limits `ssm:PutParameter` to the repo's own segment and reads to the same environment; verify with an IAM policy simulation fixture test, then in beta (cross-repo write denied, ENV-07 integration-beta)
- [x] 8.4 Implement the end-to-end compatibility gate that reads all four repos' manifests in one environment and compares pinned `contract_version` with `served_contract_majors`; verify OWN-08 with fixture manifests (compatible and incompatible majors)

## 9. Environment isolation guardrails

- [x] 9.1 Provide permission-boundary templates: an env-tag deny for cross-environment access, a research-role deny on platform metadata/publication/execution, and a live-financial deny list; verify ENV-03/ENV-04/ENV-05 with IAM policy simulation unit tests
- [ ] 9.2 Write the gamma isolation test suite (gamma role → prod bucket denied; gamma Gateway targets resolve to gamma only; research role → publication write denied) for consumer repos to run; verify that it runs against beta/gamma once deployed (BLOCKED by bootstrap)
- [x] 9.3 Record the single-account decision (D5, OQ-1 resolved), its isolation mechanisms (naming, env tags, permission boundaries, separate per-env resources) and multi-account as a possible future migration in `contracts/README.md`; verify by review and with ENV-18 (a fixture template with a role lacking the environment permission boundary fails the policy check; a cross-env tagged action is denied in policy simulation)

## 10. Pipeline standard and bootstrap

- [x] 10.1 Write the pipeline standard (stage order, artifact-only promotion, approval recording, smoke on a synthetic portfolio, rollback by `release_id`) and a pipeline-structure check that inspects a synthesized pipeline template; verify ENV-09 with fixture templates (missing approval stage fails)
- [x] 10.2 Write the bootstrap runbook and pre-check script for the user's existing authenticated CLI session (account matches local untracked config, region = configured primary, a root caller proceeds with a printed scoped/MFA-role recommendation, scoped pipeline/deploy/service roles created, CodeConnection AVAILABLE, reference written to `/finplan/shared/<repo>/config/codeconnection-ref`, then a source-stage dry run before deploy stages are enabled); verify ENV-12 (root caller proceeds with recommendation; account mismatch stops; deploy actions use scoped roles) and ENV-13 (dry run success enables stages; dry run failure stops with the extend-installation message) with mocked STS/connection/pipeline responses, and make no AWS mutation in tests
- [ ] 10.3 Write the rollback procedure (redeploy the stored assembly for `previous_release_id`, manifest `rolled_back_from`, coordinated rollback when a contract major is still pinned); verify ENV-11 as a gamma rollback drill once pipelines exist (BLOCKED by bootstrap)
- [x] 10.4 Record the region decision and quota/cost notes (D8), the 2026-10-07 user decisions (D11) and the open-questions register (OQ-1..OQ-13, with resolved items marked RESOLVED 2026-10-07) in `contracts/README.md`, with no prices invented and no account identifiers; verify that the leak scan passes
- [ ] 10.5 Run the FinancialPlanning tooling bootstrap with the user's existing CLI session, including the source-stage dry run; verify the dry run fetched `main` and the scoped roles exist (approved in principle 2026-10-07, D12: runs only after the bootstrap IaC exists, and the exact stacks and a cost estimate are shown first; if the dry run fails, the user extends the GitHub App installation and the dry run is repeated)

## 11. Cross-repo integration checks

- [ ] 11.1 Publish contracts 1.0.0 to beta, then gamma/prod consumers, and confirm that FinanceModel, FinanceLambdasTool and FinanceAgent changes pin it; verify via each repo's build log showing the pinned version and digest (waits on each pipeline's bootstrap, approved in principle 2026-10-07, D12)
- [ ] 11.2 Run the phase 1 end-to-end check in beta then gamma (agent → Gateway → fixture tool → platform plan API; `plan_version_id` and checksum match a direct read); verify ENV-15 and OWN-06 (`DEPENDENCY_UNAVAILABLE` for model-backed tools when the model release is absent)
- [ ] 11.3 After a prod promotion, verify ENV-10 (identical `artifact_digest` across beta/gamma/prod manifests for the same `release_id`) and ENV-15 smoke against the synthetic prod portfolio

## 12. Cross-repo review amendments (design D10)

- [x] 12.1 Add the 1.0.0 schemas from D10: staged-output manifest, snapshot `status`, `quality_flags` and artifact `kind` open enums, Excel template and import report, job lifecycle/purpose/dry-run/cost-estimate fields, tool catalog plus per-tool request/response pairs, and the caller block; verify CS-02 (inventory includes every new schema with valid and invalid fixtures), CS-11 (open enums and job lifecycle fixtures) and DOM-01 (the new `core/v1` schemas pass the neutrality check)
- [x] 12.2 Add the cost-allocation tag keys and the registered keys from D4 (`budget-enforced-role-names`, `gateway-principal-ref`, `tool-catalog`, and the D11 keys listed in 13.2) to the SSM convention helpers; verify ENV-07 and ENV-17 unit tests and that the `budget-state` runtime writer exception is the only allowed `shared` runtime writer (policy simulation)
- [x] 12.3 Add the matrix rows from D1 for FinanceModel, FinanceLambdasTool and FinanceAgent resources and the account-level shared resources; verify OWN-02 and ENV-16 (a fixture template placing environment data in a `shared` resource fails)
- [x] 12.4 Add the proxied-call key derivation helper (`lt_` + SHA-256) to the Python and TypeScript validators; verify ID-10 (two callers with the same key produce distinct derived keys; the same caller's retry produces the same key in both languages)
- [x] 12.5 Record the phase 2 contract minors (explanation evidence kinds, lineage reproducibility fields, leakage flags, portfolio policy) and OQ-12 in `contracts/README.md`; verify by review against FinanceAgent `add-explanation-workflows` and FinanceModel `add-learning-and-llm-strategies`

## 13. User decisions of 2026-10-07 (design D11)

- [x] 13.1 Update `contracts/ownership/matrix.yaml` per D1: replace the OpenAI secret row with the FinanceAgent explanation-provider configuration row (Bedrock, `config/explanation-provider`, `config/explanation-model-id`), add Bedrock model access as `external`, add the Jev API key secret row (owner FinanceModel, account-level `shared`, pre-existing, reference `/finplan/shared/financemodel/secret-ref/jev-api-key`), and mark the CodeConnection row as a reused existing connection; verify OWN-02 (every D1 row present, exactly one owner, no OpenAI row) and ENV-16 (the Jev secret is accepted as a `shared` credential and no IaC declares it)
- [x] 13.2 Register the D11 SSM keys in the convention helpers (`/finplan/shared/financialplanning/config/budget-allocation`, `/finplan/<env>/financeagent/config/explanation-provider`, `/finplan/<env>/financeagent/config/explanation-model-id`, `/finplan/shared/financemodel/secret-ref/jev-api-key`) and mark the per-repo `config/budget-allocation` and `secret-ref/openai-api-key` keys as retired (helper rejects new writes); verify ENV-07 unit tests and ENV-08 (the Jev secret-ref holds only the name; the leak scan flags any secret value)
- [x] 13.3 Add the budget allocation schema (category → USD; categories `platform_infra`, `cpu_research`, `bedrock_explanations`, `gpu`, `reserve`; sum at most the ceiling) with default fixture 8/7/5/25/5, and `budget_category` on the job cost-estimate block; verify ENV-17 (defaults valid; sum above the ceiling invalid; exhausted category refuses with `BUDGET_EXCEEDED`; Jev cost not counted) and CS-11
- [x] 13.4 Provide the budget-alert and enforcement-action template fragment (alerts at 50/80/100% of actual spend, deny action at 100% on the published enforced role names, including the FinanceAgent runtime role for Bedrock invocations) for the platform tooling stack; verify ENV-19 with synth assertions and policy simulation (start job, start pipeline and Bedrock invoke denied after the action; reads allowed)
- [x] 13.5 Add the GPU approval rule to the conformance checks for job schemas (GPU purpose without a recorded approval cannot leave `awaiting_approval`); verify ENV-20 with job-status fixtures

## 14. Round 2 user decisions of 2026-10-07 (design D12)

- [x] 14.1 Update `contracts/ownership/matrix.yaml`: add the market-data provider source row (`external`, `yfinance`, consumer FinancialPlanning ingestion only, no secret) and record the Bedrock model-access row for `us.anthropic.claude-opus-5`; verify OWN-02 (provider row present, `external`, sole consumer FinancialPlanning ingestion) and OWN-03 (no account identifiers in the new rows)
- [x] 14.2 Record D12 and the OQ-5/OQ-13 resolutions (RESOLVED 2026-10-07) in `contracts/README.md`, with no prices and no account identifiers; verify that the leak scan passes
- [x] 14.3 Add to contracts 1.0.0 the optional snapshot provider-lineage fields (`provider_library`, `library_version`, alongside the retrieval timestamp), the optional `finance/v1` daily observation fields (`adj_close`, `dividend`, `split_ratio`) and the quality flags `empty_response` and `partial_response`; verify CS-11 (open-vocabulary fixtures) and CS-09 (all fixtures remain synthetic)
- [x] 14.4 Add a bootstrap pre-run step that prints the exact stacks to deploy and a cost estimate (from current AWS pricing, no hard-coded prices) and refuses to run before the bootstrap IaC is synthesized; verify ENV-12 (in-principle approval scenario) with a mocked run

## 15. Platform verification fixes (contracts 0.2.0, design D13)

- [x] 15.1 Extend `contracts/ownership/matrix.yaml` so every resource the platform synthesizes has a row or an attributed parent:
  - the API Gateway resources, deployment, stage and gateway responses;
  - the handler roles and log groups, and the SSM reference parameters;
  - the schedule's role, dead-letter queue, queue policy and alarm;
  - the budget roles, topic policy and subscriptions;
  - the pipeline store's bucket policy;
  - new rows for the metadata sweeper, the permission-boundary policies (`shared` pipeline tooling) and `pipeline-environment-roles-<repo>`;
  - `AWS::ApiGateway::Method` added as a parent-attributed helper type.

  The checker is not relaxed. Verify OWN-01, OWN-09 and ENV-16 with fixture templates of the platform's resource kinds, including deploy roles tagged `shared` or with their environment. Verify that `finplan-conformance ownership-check` on the platform's synthesized templates reports zero problems.
- [x] 15.2 Add `core/v1/api/*` and `finance/v1/api/*` request/response schemas for create portfolio, create plan, create root version, record execution, the observation read and the staged-output outcome. Add `plan.publication_revision`, plus the optional snapshot fields `lineage.calendar_version`, `quality_details` and `observation_summary`. Let `tools/refresh-market-data-response` answer a non-session day without a snapshot. Document `CASH` as reserved in the Excel template. Keep `lineage.provider` and correct `provider_id` in the specs. Verify CS-02 (valid and invalid fixtures from the generator), CS-10 (Python and TypeScript conformance), CS-11 and DOM-01.
- [x] 15.3 Register `/finplan/<env>/financemodel/job/job-role-ref`, `/finplan/<env>/financemodel/job/job-api-role-ref`, `/finplan/<env>/financeagent/agent/user-pool-ref` and `/finplan/<env>/financelambdastool/config/direct-test-principal-name` in the SSM convention and the matrix. Tie the existing `authorizer-metadata-ref` and `secret-ref/ci-test-client` keys to the identity provider, which is now final (decisions 15a and 15b). Verify ENV-07 and OWN-02.
- [x] 15.4 Fix the tooling:
  - the boundary renderer resolves pseudo-parameter `Ref`s inside `Fn::Join`;
  - `copied-id` reads YAML timestamps as text and skips documents that are not JSON;
  - the boundaries' name-based deny uses one ARN pattern per service with pseudo parameters (no E3510).

  Verify ENV-18 on a CDK-style boundary ARN, CS-01 on a tree with YAML dates, and that every boundary resource matches the E3510 ARN pattern, stays below IAM's managed-policy size limit and passes `cfn-lint` on a rendered tooling template.
- [x] 15.5 Bump `contracts/VERSION` to 0.2.0. Verify that the compatibility gate against the 0.1.0 wheel lists only additive and annotation changes, apart from the documented 0.x-exempt change in `tools/refresh-market-data-response`. Verify that it passes with `--allow-zero-major-breaking` and that two digest builds are identical (CS-03, CS-04).
- [x] 15.6 Record D13 in the design, specs and `contracts/README.md`. Verify `openspec validate establish-cross-repo-contracts --strict` and the leak scan.

## Requirement-to-test mapping

Test types: unit, contract (schema/conformance with fixtures), integration-beta, gamma, smoke. "Runs in" names the repo whose pipeline executes the test. Tests outside FinancialPlanning use tooling shipped in the contract package.

| Requirement | Test ID | Type | Runs in |
|---|---|---|---|
| Single owning repository per resource | OWN-01 | unit | all repos (build) |
| CDK-generated helper resources are attributed, never ignored | OWN-09 | unit (fixture templates) | all repos (build) |
| Baseline ownership assignments | OWN-02 | unit | FinancialPlanning |
| Consumers reference resources only through published references | OWN-03 | unit (leak scan) + integration-beta (SSM resolution) | all repos |
| No duplicate authoritative plan state | OWN-04 | contract + integration-beta | FinanceLambdasTool, FinanceModel |
| Explicit cross-resource permissions | OWN-05 | integration-beta + gamma (cross-env deny) | FinanceLambdasTool, FinanceAgent |
| Integration order | OWN-06 | integration-beta + gamma | FinanceLambdasTool, FinanceAgent |
| Backward-compatible publish before consumer update | OWN-07 | unit (compat gate) | FinancialPlanning |
| End-to-end validation requires compatible releases in the same environment | OWN-08 | unit (fixture manifests) + gamma | FinancialPlanning, FinanceAgent |
| Canonical identifier set and formats | ID-01 | unit + contract | FinancialPlanning, all consumers |
| Content-addressed configuration identifier | ID-02 | unit | FinancialPlanning |
| Identifier minting authority | ID-03 | contract + integration-beta | FinancialPlanning, FinanceModel |
| Immutable identified records | ID-04 | integration-beta | FinancialPlanning (platform change) |
| Input snapshot identity | ID-05 | contract + integration-beta | FinancialPlanning |
| Plan version lineage and overrides | ID-06 | contract + integration-beta (Excel round trip, no-effect) | FinancialPlanning |
| Plan version validation status | ID-07 | contract + integration-beta (partial output) | FinancialPlanning |
| Publication references an exact validated version | ID-08 | integration-beta + gamma | FinancialPlanning |
| Execution is separate from publication | ID-09 | contract + integration-beta + smoke | FinancialPlanning |
| Idempotency keys on state-changing operations | ID-10 | contract + integration-beta (duplicate requests) | FinancialPlanning, FinanceLambdasTool |
| Optimistic concurrency on mutable heads | ID-11 | integration-beta (concurrent override) | FinancialPlanning |
| Single published contract package | CS-01 | unit (copied `$id`) | all repos |
| Contract package coverage | CS-02 | unit | FinancialPlanning |
| Plan lifecycle API route schemas | CS-12 | contract (valid and invalid fixtures) | FinancialPlanning |
| Registered vocabularies and phase 2 additions | CS-11 | contract (open-enum and non-terminal-state fixtures) | FinancialPlanning, FinanceModel |
| Semantic versioning of contracts | CS-03 | unit (compat gate, schema upgrade) | FinancialPlanning |
| Immutable, pinned contract releases | CS-04 | unit (digest) + integration-beta (republish refused) | FinancialPlanning, consumers |
| Standard error envelope | CS-05 | contract | all producers |
| Registered error codes | CS-06 | unit | FinancialPlanning |
| Completion status separate from solution status | CS-07 | contract | FinanceModel, FinanceLambdasTool |
| Trusted artifact references | CS-08 | contract | FinancialPlanning, FinanceLambdasTool |
| Shared synthetic fixtures | CS-09 | unit (hygiene) | FinancialPlanning |
| Validators and conformance suite | CS-10 | contract (local both-language runner, 6.5a) + build stage (CodeBuild, 6.5b) | all repos |
| Three isolated environments | ENV-01 | integration-beta + gamma | each repo |
| Single-account environment isolation | ENV-18 | unit (policy check, policy simulation) + gamma (cross-env tagged action denied) | each repo |
| Per-environment pipeline roles | ENV-21 | unit (ownership and boundary checks on fixture templates) | each repo (build) |
| Account-level shared resources | ENV-16 | unit (ownership/tag check) + gamma (quota requeue, no shared lease) | FinancialPlanning, FinanceModel, FinanceAgent |
| Project cost ceiling | ENV-17 | unit (synth: budget limit from parameter; allocation defaults and sum check; exhausted category refuses; Jev cost excluded) + integration-beta | FinancialPlanning, FinanceModel, FinanceAgent |
| Budget alerts and enforcement action | ENV-19 | unit (synth assertions + policy simulation) | FinancialPlanning |
| GPU runs require explicit user approval | ENV-20 | contract (job-status fixtures) + integration-beta | FinanceModel |
| Primary deployment region | ENV-02 | unit (synth check) | each repo |
| Gamma and beta never touch production data | ENV-03 | unit (policy simulation) + gamma | each repo |
| Research compute cannot mutate published state | ENV-04 | unit (policy simulation) + gamma | FinanceModel, FinancialPlanning |
| Live financial permissions are separated | ENV-05 | unit (policy scan) | all repos |
| Release manifest per repository and environment | ENV-06 | contract + integration-beta | all repos |
| SSM parameter naming convention | ENV-07 | unit + integration-beta | all repos |
| Secrets referenced by name only | ENV-08 | unit (leak scan) + integration-beta | FinanceAgent, all repos |
| Standard pipeline stages | ENV-09 | unit (pipeline template check) | all repos |
| Immutable artifact promotion | ENV-10 | smoke (digest equality) | all repos |
| Rollback recording and procedure | ENV-11 | gamma (rollback drill) | all repos |
| One-time authenticated pipeline bootstrap | ENV-12 | unit (mocked STS: root caller proceeds with recommendation, account mismatch stops, scoped deploy roles used; stacks and cost estimate shown before running) | FinancialPlanning tooling |
| Verified source connection | ENV-13 | unit (mocked connection and source-stage dry run) + bootstrap dry run | FinancialPlanning tooling, each repo's bootstrap |
| Research jobs are not pipeline deployments | ENV-14 | integration-beta | FinanceModel |
| Phase 1 fixture-backed deployment | ENV-15 | integration-beta + gamma + smoke | all repos |
| Domain-neutral envelope | DOM-01 | unit (neutrality check) + contract | FinancialPlanning |
| Domain adapter registration | DOM-02 | contract | FinancialPlanning, FinanceModel |
| Finance is the initial domain adapter | DOM-03 | contract | FinancialPlanning |
| Domain-neutral explanation evidence | DOM-04 | contract | FinanceAgent |

## Cross-repo coverage of the brief's required test themes

Added by the cross-repo review (2026-10-07). Each theme names the tests that cover it in every repo.

| Theme (brief) | Tests |
|---|---|
| Immutable snapshots | FinancialPlanning STO-04, ING-08; FinanceModel WS-04 (checksum verified before use) |
| Accounting reconciliation | FinancialPlanning API-05, STG-05; FinanceModel SIM-09; FinanceAgent FA-EV-04 |
| Scenario isolation (what-if runs never touch published state or each other) | FinanceModel RST-01, WS-05; FinanceAgent FA-WF3-05, FA-EV-08; contracts ENV-04 |
| Duplicate requests | ID-10; FinancialPlanning MDS-05, API-08, ING-09, STG-06, XLS-06; FinanceModel JOB-04; FinanceLambdasTool integration-beta 10.1 |
| Partial or failed output | CS-07; FinancialPlanning STG-04, STG-05; FinanceModel RST-03, CTL-06 |
| Concurrency | ID-11; FinancialPlanning MDS-03, API-06, STG-06; FinanceModel CTL-02 |
| Manual vs agent agreement (same `plan_version_id` and checksum) | FinancialPlanning API-01; ENV-15 (through Gateway) |
| Excel override round trip | ID-06; FinancialPlanning XLS-04 |
| Publication vs execution | ID-09; FinancialPlanning API-07 |
| Schema upgrades | CS-03, CS-11; FinancialPlanning API-10, PIPE-06 |
| No-effect attribution | ID-06; FinancialPlanning API-04; FinanceAgent FA-EV-06, FA-WF3-04 |
| Deployed MCP invocation | ENV-15; FinanceAgent integration-beta and gamma MCP suites (FA-GW, FA-POL) |
| Reproducible, credential-free teaching artifacts | CS-09 (fixture hygiene, offline direct test); FinanceAgent FA-SK-04 (exported skills pass the leak scan) |

## Workflow follow-up

- The platform implementation change (separate) conforms to these specs and consumes contracts 1.x.
- FinanceModel, FinanceLambdasTool and FinanceAgent changes reference this change and pin the published contract version.
- Archive this change after contracts 1.0.0 is published and the phase 1 end-to-end check passes in gamma.
