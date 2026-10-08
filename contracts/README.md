# finplan contract package

This directory is the **single source** of cross-repository contracts for the four finplan
repositories (FinancialPlanning, FinanceModel, FinanceLambdasTool, FinanceAgent). It holds the
JSON Schemas, synthetic fixtures, validators, the `finplan-conformance` runner, the ownership
matrix, the release-manifest schema and the SSM naming convention. FinancialPlanning publishes
it. Every other repository pins a published version and never copies its schemas.

Requirements and decisions come from the OpenSpec change
[`establish-cross-repo-contracts`](../openspec/changes/establish-cross-repo-contracts/):
the [proposal](../openspec/changes/establish-cross-repo-contracts/proposal.md), the
[design](../openspec/changes/establish-cross-repo-contracts/design.md) (decisions D1 to D12 and
open questions OQ-1 to OQ-13), the [specs](../openspec/changes/establish-cross-repo-contracts/specs/)
and the [tasks](../openspec/changes/establish-cross-repo-contracts/tasks.md). Where this README
and the design differ, the design wins.

> **Public repository.** Nothing in this directory may contain an AWS account ID, an ARN with an
> account, a bucket, role or connection name, an endpoint URL, a secret value or a price.
> Placeholders look like `<account-id>`. `finplan-conformance leak-scan` enforces this
> (OWN-03, ENV-08).

## Contents

1. [Layout](#layout)
2. [Quick start](#quick-start)
3. [Ownership matrix and how consumers reference resources](#ownership-matrix-and-how-consumers-reference-resources) (task 1.4)
4. [Identifier lifecycle and minting authority](#identifier-lifecycle-and-minting-authority) (task 3.4)
5. [Environments: single-account decision and isolation](#environments-single-account-decision-and-isolation) (task 9.3)
6. [Region decision, quotas and cost notes](#region-decision-quotas-and-cost-notes) (task 10.4, D8)
7. [User decisions of 2026-10-07 (D11)](#user-decisions-of-2026-10-07-d11) (task 10.4)
8. [Round 2 user decisions (D12)](#round-2-user-decisions-d12) (task 14.2)
9. [Planned phase 2 contract minors and OQ-12](#planned-phase-2-contract-minors-and-oq-12) (task 12.5)
10. [Contracts 0.2.0: platform verification fixes (D13)](#contracts-020-platform-verification-fixes-d13) (tasks 15.x)
11. [Contracts 0.2.1: dead deny entries and pipeline log groups (D14)](#contracts-021-dead-deny-entries-and-pipeline-log-groups-d14) (tasks 16.x)
12. [Contracts 0.2.2: the budget action can reset its own action (D15)](#contracts-022-the-budget-action-can-reset-its-own-action-d15) (tasks 17.x)
13. [Contracts 1.0.0: first stable release, FinanceModel rows and the contract registry (D16)](#contracts-100-first-stable-release-financemodel-rows-and-the-contract-registry-d16) (tasks 18.x)
14. [Open-questions register](#open-questions-register) (task 10.4)
15. Procedures in [`docs/`](docs/):
    - [Local, credential-free testing with fixtures](docs/local-testing.md) (task 6.6)
    - [Consumer pinning and the 0.x beta-only rule](docs/consumer-pinning.md) (task 7.4)
    - [Pipeline standard](docs/pipeline-standard.md) (task 10.1)
    - [Bootstrap runbook](docs/bootstrap-runbook.md) (tasks 10.2, 14.4)
    - [Rollback procedure](docs/rollback.md) (task 10.3)

## Layout

| Path | Contents |
|---|---|
| `VERSION` | The single version number. The Python wheel, the npm package and the schema tarball all carry it |
| `core/v1/*.json` | Domain-neutral envelope schemas (JSON Schema 2020-12), including `core/v1/tools/*` request/response pairs and `core/v1/api/*` plan lifecycle API route schemas (0.2.0) |
| `finance/v1/*.json` | Finance adapter payload schemas, including `finance/v1/tools/*` and `finance/v1/api/*` (0.2.0) |
| `domains/registry.json` | Domain registry. `finance` is the only entry; the file also holds the neutrality deny-list |
| `fixtures/<schema-name>/{valid,invalid}/*.json` | Synthetic fixtures for every schema (see [`fixtures/README.md`](fixtures/README.md)) |
| `fixtures/vectors/*.json` | Cross-language test vectors (`configuration_id`, proxied idempotency keys) |
| `conformance/cases.yaml` | Language-neutral conformance cases (expected error codes and check context) |
| `ownership/matrix.yaml` | Machine-readable ownership matrix (design D1) |
| `templates/` | IAM permission-boundary, SSM-access, live-financial deny and budget templates |
| `python/` | `finplan-contracts` distribution: validators and the `finplan-conformance` CLI |
| `typescript/` | `@finplan/contracts` npm package |
| `docs/` | Procedures (local testing, pinning, pipeline standard, bootstrap, rollback) |

Schema `$id`s have the form `https://contracts.finplan.invalid/<core|domain>/v<major>/<name>.json`
(design D3). The `.invalid` host means the `$id`s are identifiers only and are never fetched.
Every `$id` contains the major version.

### `finplan-conformance` subcommands

| Subcommand | Purpose | Spec tests |
|---|---|---|
| `validate` | Validate JSON documents against a schema; `--envelope` prints the error envelope | CS-05, ID-01 |
| `conformance` | Conformance suite, `--mode producer` or `--mode consumer` | CS-02, CS-09, CS-10 |
| `ownership-check` | Compare synthesized CloudFormation templates with the matrix | OWN-01, ENV-16 |
| `neutrality` | No finance terms in `core/v1` schemas | DOM-01 |
| `leak-scan` | Account IDs, ARNs, bucket names, endpoints, secrets | OWN-03, ENV-08 |
| `copied-id` | Contract `$id`s copied into a consumer repository | CS-01 |
| `live-perm-scan` | Live trading, payment or wallet permissions | ENV-05 |
| `compat` | Schema compatibility gate against the previous release | CS-03, OWN-07 |
| `digest` | Reproducible build outputs and their SHA-256 digests | CS-04 |
| `manifest-gate` | End-to-end compatibility of one environment's release manifests | OWN-08 |
| `pipeline-check` | Synthesized pipeline template against the pipeline standard | ENV-09 |
| `bootstrap-precheck` | Bootstrap pre-run plan (stacks and cost estimate) and read-only pre-checks | ENV-12, ENV-13 |
| `budget` | Allocation check and pre-flight against a budget category | ENV-17 |
| `ssm-path` | Build or check SSM parameter names, list registered keys | ENV-07 |

## Quick start

All commands run offline, with no AWS credentials (details in
[docs/local-testing.md](docs/local-testing.md)):

```sh
cd contracts/python
uv run finplan-conformance conformance --mode producer   # all schemas against all fixtures
uv run pytest -q                                         # unit tests, fully offline
uv run finplan-conformance leak-scan ..                  # public-repo hygiene
```

## Ownership matrix and how consumers reference resources

`ownership/matrix.yaml` is the machine-readable form of design D1. It is authoritative for
tooling, and `finplan-conformance ownership-check` reads it.

### Rules

- **One owner per resource.** Every deployable resource has exactly one owning repository. Only
  the owner's pipeline creates, updates or deletes it. Another repository whose synthesized IaC
  declares the resource fails its build (OWN-01). A resource with no row fails too, until an
  owner is recorded.
- **CDK-generated helpers are attributed, never ignored** (OWN-09, task 1.5). A resource CDK
  generates without its own `logical-role` (the `AWS::IAM::Policy` on a role, an
  `AWS::Lambda::Permission`, `AWS::CDK::Metadata`, custom-resource providers, log-retention
  helpers) either belongs to the owner of its parent construct (its type is in
  `cdk_generated_helpers.parent_attributed_types` and everything it references is owned by the
  checked repository) or matches an entry of the reviewed `cdk_generated_helpers.allow_list`.
  Each entry records types, a logical-ID or `aws:cdk:path` pattern, a reason and the review.
  Anything else fails, and the check's report lists every attribution. Adding an entry needs
  review.
- **A row lists every type its constructs synthesize** (0.2.0, D13). For example, the plan API
  row lists the API Gateway resources, deployment, stage and gateway responses and the handler's
  role and log group, and every row whose reference is published in SSM lists
  `AWS::SSM::Parameter`. Untaggable API methods (`AWS::ApiGateway::Method`) are attributed to
  their parent API like the other CDK helpers.
- **Per-environment pipeline roles** (0.2.0, D13). The deploy, CloudFormation execution and stage
  roles of one environment are declared in the account-level pipeline stack. They are tagged
  with their environment and carry that environment's permission boundary (row
  `pipeline-environment-roles-<repo>`, `scope: environment`). The same logical roles tagged
  `shared` belong to the account-level pipeline row. The `environment` tag selects the row; the
  ENV-16 and ENV-18 rules then apply to the selected row.
- **`external` resources** are owned by no repository: account limits, third-party services, and
  human-managed or pre-existing resources. Examples are the GitHub CodeConnection, Bedrock model
  access, the `yfinance` market-data source and SageMaker quotas. No repository declares them in
  IaC. They are referenced only through configuration.
- **Pre-existing account-level secrets** are referenced, never created. The Jev API key secret is
  the example: FinanceModel owns the *reference*, and IaC never declares, reads or writes the
  value.
- **Only FinancialPlanning holds authoritative plan state** (portfolio, plan, plan version,
  publication, execution). Other repositories keep at most non-authoritative caches, keyed by
  immutable IDs.
- **Explicit grants.** The owner grants each cross-repository access through a scoped resource
  policy or role trust. That grant names the consumer's principal for the same environment.
  Wildcard cross-environment grants are not allowed.

### Matrix (summary of D1)

`<env>` is `beta`, `gamma` or `prod`. `shared` is the reserved segment for account-level values.

| Resource | Owner | Consumers | Reference |
|---|---|---|---|
| Raw/curated input buckets, immutable snapshot artifact bucket | FinancialPlanning | FinanceModel (approved snapshots only) | Trusted artifact refs from the platform API, plus a read grant to the per-env FinanceModel job role. Bucket names appear only in `/finplan/<env>/financialplanning/config/*` |
| Plan/output/report artifact buckets | FinancialPlanning | website, FinanceLambdasTool, FinanceAgent (indirectly) | Trusted artifact refs or presigned downloads from the platform API. No raw keys |
| Run-output staging area | FinancialPlanning | FinanceModel production workers (write-only) | `/finplan/<env>/financialplanning/config/run-staging-ref` plus an IAM grant |
| Platform KMS keys, lifecycle and retention | FinancialPlanning | none (key policy grants) | Key policy grants, per env |
| Metadata tables (portfolio, plan, version, publication, execution, snapshot catalog, staged-output outcomes, idempotency, audit) | FinancialPlanning | none directly | Plan API only |
| Metadata sweeper (scheduled housekeeping of the metadata tables) | FinancialPlanning | none | Internal |
| Plan lifecycle API (Excel import, publish, paper execution) | FinancialPlanning | FinanceLambdasTool, website, scheduled workflows | `/finplan/<env>/financialplanning/api/plan-endpoint` plus IAM (SigV4) |
| Ingestion service | FinancialPlanning | FinanceLambdasTool `refresh_market_data`, scheduler | `/finplan/<env>/financialplanning/api/ingestion-endpoint` |
| Daily EventBridge Scheduler (America/New_York) | FinancialPlanning | none | `/finplan/<env>/financialplanning/config/ingest-schedule` (OQ-6) |
| Contract package | FinancialPlanning | all repos | Pinned version plus SHA-256 digest |
| Project budget (USD 50 ceiling, alerts 50/80/100%), deny action at 100%, budget-state writer, default allocation | FinancialPlanning (tooling stack, `shared`) | all repos | `/finplan/shared/financialplanning/config/{cost-ceiling-usd,budget-allocation,budget-state}`; `/finplan/<env>/<repo>/config/budget-enforced-role-names` (per environment, written by the pipeline) and `/finplan/shared/<repo>/config/budget-enforced-role-names` (account-level tooling roles, written by the repo's bootstrap; 1.0.0) |
| Cost-allocation tag keys | FinancialPlanning (contract package) | all repos | `project`, `owner-repo`, `environment`, `logical-role`, `run-id` |
| Permission-boundary managed policies (`finplan-<env>-permission-boundary`, `finplan-<env>-research-permission-boundary`, `finplan-shared-permission-boundary`) | FinancialPlanning (tooling stack, `shared`, pipeline tooling) | every pipeline-created role | Policy names from the contract package; ARNs built at deploy time from pseudo parameters |
| Research workspace storage, evaluation datasets | FinanceModel | none outside FinanceModel | n/a |
| SageMaker job definitions, containers, ECR repos, SageMaker Pipelines, the per-environment job-execution role (`job-execution-role`, 1.0.0) | FinanceModel | FinanceLambdasTool (via the job interface), platform grants | `/finplan/<env>/financemodel/job/*`; the job-execution role at `/finplan/<env>/financemodel/job/job-role-ref` (snapshot read and staging write grants) |
| Job submission/status/result interface (mints `run_id`; REST API with its deployment and stage, 1.0.0) and job control plane | FinanceModel | FinanceLambdasTool, platform | `/finplan/<env>/financemodel/api/job-endpoint`; the job API handler role at `/finplan/<env>/financemodel/job/job-api-role-ref` (may read `GET /v1/staged-outputs/*`) |
| Model artifacts and registry (mints `model_version`; registry bucket policy, 1.0.0) | FinanceModel | platform (lineage), FinanceLambdasTool | `/finplan/<env>/financemodel/model/registry-ref` |
| Self-hosted Qwen3.6-27B serving lifecycle, concurrency lease, staged weight copy | FinanceModel | FinanceModel agent-swarm strategy | Internal. The other project's existing stack is **external and not adopted** |
| Promotion-criteria store, explanation-evidence artifacts | FinanceModel | FinanceAgent (via tool results) | Trusted artifact refs from `get_job_result` |
| Account-level SageMaker instance quotas | **external** | FinanceModel | Per-environment lease plus quota-aware requeue; no shared lease |
| MCP adapter Lambdas, aliases, role classes `reader`/`submitter`/`plan-writer` | FinanceLambdasTool | FinanceAgent Gateway, the direct-test principal | `/finplan/<env>/financelambdastool/lambda/<tool>-arn`, `.../lambda/role-<class>-arn`; the single direct-test principal (the project owner, decision 15b) at `/finplan/<env>/financelambdastool/config/direct-test-principal-name` |
| Tool catalog | FinanceLambdasTool | FinanceAgent Gateway registration | `/finplan/<env>/financelambdastool/contract/tool-catalog` (manifest output `tool-catalog`) |
| AgentCore Runtime, Gateway, targets, policy engine, interceptor, Memory, skills, FinanceAgent ECR repo | FinanceAgent | Claude Code/Codex via MCP, website/CLI | `/finplan/<env>/financeagent/agent/*` |
| Per-env Gateway service role | FinanceAgent | FinanceLambdasTool (invoke grant) | `/finplan/<env>/financeagent/agent/gateway-principal-ref` |
| OIDC identity provider: one Amazon Cognito user pool per environment (**final**, decision 15a; FA-OQ-1 resolved; the website reuses it, OQ-10) | FinanceAgent | Gateway, Runtime, website, CI | `/finplan/<env>/financeagent/agent/user-pool-ref`, `/finplan/<env>/financeagent/agent/authorizer-metadata-ref`; CI client secret name at `/finplan/<env>/financeagent/secret-ref/ci-test-client` |
| Explanation-provider configuration (default `bedrock`, model ID; no API-key secret) | FinanceAgent | FinanceAgent runtime only | `/finplan/<env>/financeagent/config/explanation-provider`, `/finplan/<env>/financeagent/config/explanation-model-id` |
| Bedrock model access (Claude Opus 5 via `us.anthropic.claude-opus-5`) | **external** | FinanceAgent runtime role | Model ID from the FinanceAgent config parameter |
| Market-data provider source (`yfinance`) | **external** | FinancialPlanning ingestion **only** | Pinned library version; provider selection in `/finplan/<env>/financialplanning/config/*`; no secret |
| TypeSafe Jev API key secret (pre-existing, account-level) | FinanceModel (reference) | FinanceModel Jev strategy job roles only | Secret name `finplan/shared/financemodel/jev-api-key`, published at `/finplan/shared/financemodel/secret-ref/jev-api-key` |
| CodePipeline, CodeBuild projects and their log groups (FinancialPlanning since 0.2.1, FinanceModel since 1.0.0), pipeline artifact bucket and its policy, account-level pipeline/build/deploy roles | each repo for itself (`shared`) | none | n/a |
| Per-environment deploy, CloudFormation execution and stage roles (tagged with their environment, environment boundary) | each repo for itself (`environment`) | none | n/a |
| GitHub CodeConnection (existing, AVAILABLE, reused) | **external** | each pipeline | `/finplan/shared/<repo>/config/codeconnection-ref` |
| CodeArtifact domain `finplan` and repository `contracts` for the contract package (declared in the FinancialPlanning tooling stack, 1.0.0) | FinancialPlanning | all build stages (read: `finplan-shared-<repo>-*` build roles) | `/finplan/shared/financialplanning/contract/registry-ref` |
| Website (plan UI) | **OQ-10**, provisionally FinancialPlanning | users | Plan API only |

The OpenAI secret reference and the per-repo budget-allocation keys were retired on 2026-10-07
(D11) and have no row. `finplan-conformance ssm-path keys` lists every registered and retired key.

### How consumers reference resources

A consumer never writes another repository's resource identifier into a file. It resolves the
identifier at deploy or run time, in the same environment and region, by one of two routes:

1. **SSM parameters.** Names follow `/finplan/<environment>/<repo>/<category>/<name>`:
   - `<environment>` is `beta`, `gamma` or `prod`. It is `shared` for account-level values, which
     only the bootstrap or the contract-publish step writes. The one exception is the platform's
     `budget-state` writer.
   - `<repo>` is the owning repository in lowercase.
   - `<category>` is one of `release`, `contract`, `api`, `lambda`, `job`, `model`, `agent`,
     `config` or `secret-ref`.
   - `<name>` is lowercase kebab-case.

   Only the owning repository writes under its `<repo>` segment. A pipeline role writes only in
   `/finplan/*/<own-repo>/*`, and reads its own environment plus `shared`.
2. **The release manifest.** `/finplan/<env>/<repo>/release/manifest` validates against
   `core/v1/release-manifest.json`. Its `outputs` map logical keys to SSM parameter names, so a
   consumer reads the manifest and then the named parameters.
   `/finplan/<env>/<repo>/release/current-release-id` names the current release.

Examples:

```sh
# Name of the gamma refresh_market_data Lambda reference that FinanceAgent reads
uv run finplan-conformance ssm-path build --env gamma --repo financelambdastool --category lambda --name refresh-market-data-arn
# -> /finplan/gamma/financelambdastool/lambda/refresh-market-data-arn

# Check that a write by the FinanceModel pipeline into the platform segment is rejected
uv run finplan-conformance ssm-path check --writer-repo financemodel --writer-kind pipeline --writer-env gamma \
  /finplan/gamma/financialplanning/api/plan-endpoint
```

- **Stored artifacts** are referenced only through *trusted artifact references*
  (`core/v1/artifact-ref.json`: `artifact_id`, `owner`, `kind`, `checksum`, `content_type`). The
  owning service resolves them. Requests that carry `s3://` URIs or paths are rejected with
  `VALIDATION_FAILED` (CS-08). Responses never expose bucket names or object keys.
- **Secrets** are referenced by name only. An SSM `secret-ref` parameter holds the secret *name*,
  and the runtime role fetches the value. No repository file or manifest contains a secret value.
- **Never use ARNs, account IDs, bucket or role names, or endpoint URLs in repository files.**
  The leak scan fails the build on them. IAM statements in repository files use generic patterns
  built at synth time from configuration.

## Identifier lifecycle and minting authority

Schemas: `core/v1/identifiers.json` (formats), `plan-version.json`, `publication.json`,
`execution.json`, `input-snapshot.json`, `idempotency.json`, `concurrency.json`. Spec:
[platform-identifiers](../openspec/changes/establish-cross-repo-contracts/specs/platform-identifiers/spec.md).

### Formats

| Identifier | Format | Minted by |
|---|---|---|
| `portfolio_id` | `pf_` + ULID | FinancialPlanning |
| `plan_id` | `pl_` + ULID | FinancialPlanning |
| `plan_version_id` | `pv_` + ULID | FinancialPlanning |
| `input_snapshot_id` | `snap_` + ULID | FinancialPlanning |
| `publication_id` | `pub_` + ULID | FinancialPlanning |
| `execution_id` | `exe_` + ULID | FinancialPlanning |
| `run_id` | `run_` + ULID | FinanceModel (job submission interface) |
| `model_version` | `mv_` + ULID | FinanceModel (model registry) |
| `configuration_id` | `cfg_` + lowercase hex SHA-256 of the RFC 8785 (JCS) canonical configuration | anyone (content-addressed) |
| `release_id` | `rel_` + ULID | the repository's build stage (shared by all environments of that build) |

A ULID is 26 Crockford base32 characters (uppercase, no `I`, `L`, `O` or `U`). The prefix lets
validators catch an ID in the wrong field. In that case validation fails with
`INVALID_IDENTIFIER` and names the field (ID-01).

### Minting authority

- FinancialPlanning mints the six platform identifiers. A client that sends its own
  `plan_version_id` on a create request is rejected with `VALIDATION_FAILED`. A tool wrapper that
  creates an override calls the platform API and returns the ID that the platform minted.
- FinanceModel mints `run_id` and `model_version` (delegated minting). The platform records them
  as foreign references and verifies them against FinanceModel's published registry reference
  before it commits staged output.
- Any party may compute `configuration_id`. Identical configurations always produce the same ID,
  whatever their key order or whitespace.
- No other repository mints any of these identifiers.

### Immutability

Once committed, the content of plan versions, snapshots, model versions, configurations, run
results, publications and executions never changes. Only contract-defined status fields change,
and each status change is an append-only event. Editing a committed version fails with
`IMMUTABLE_RECORD`; create a child version instead. Object-store versioning is never used as a
business identity. Artifacts are written before the metadata commit, under a key derived from the
new ID. An orphaned artifact is never visible and is garbage-collected.

### Lifecycle

```
portfolio ─┬─ plan ─┬─ plan_version (root; origin model_run | manual_override | excel_import)
           │        │     └─ child plan_version (override; parent_plan_version_id) ...
           │        ├─ publication ──► exactly one validated plan_version (+checksum)
           │        │     └─ execution (paper|simulated, phase 1) ──► publication
           │        └─ head pointer {current_version_id, revision}
input_snapshot ◄── plan_version ──► configuration_id, model_version, run_id
```

- **An override is a child version.** Every plan version records `plan_id`,
  `parent_plan_version_id` (null only for a root), `input_snapshot_id`, `configuration_id`,
  `model_version`, `run_id` (null for manual and Excel versions), `origin` and a content
  `checksum`. An override, whether manual or an Excel import, always creates a new child. A
  no-effect override still creates a child, whose checksum equals the parent's.
- **Validation status.** A version is `pending_validation`, `validated` or `invalid`. Only
  deterministic platform validation (schema, accounting reconciliation, constraints) moves it to
  `validated`. Partial worker output never becomes `validated`.
- **Publication versus execution.** A publication references exactly one `validated` version and
  records its checksum. Publishing anything else fails with `PRECONDITION_FAILED`. A later
  publication supersedes an earlier one without modifying it. An execution is a separate record:
  it references one `publication_id`, and its mode in phase 1 is `paper` or `simulated`. Executing
  never changes the publication or the version. Mode `live` is rejected with
  `OPERATION_NOT_PERMITTED`.
- **Snapshots** reference immutable artifacts plus a SHA-256 manifest, source and retrieval
  timestamps, coverage, quality flags, dataset identity, domain and `status`
  (`committed`, `approved` or `expired`). FinanceModel reads only `approved` snapshots.
- **Idempotency.** Every state-changing operation takes a required `idempotency_key`
  (1 to 128 characters from `[A-Za-z0-9_-]`). Its scope is (principal, environment, operation),
  and it is retained for at least 7 days. A retry with the same body returns the original result.
  The same key with a different body fails with `IDEMPOTENCY_KEY_REUSED` (not retryable). A tool
  adapter that proxies a call forwards `lt_` + hex SHA-256 of `caller|env|tool|key`, so different
  end callers never collide (`fixtures/vectors/proxied_keys.json`).
- **Optimistic concurrency.** Mutable heads carry an integer `revision`, and writes send
  `expected_revision`. On a mismatch the write fails with `CONFLICT` and nothing is applied.
  Creating a version and moving the head happen in one conditional transaction.

### Examples (synthetic, validated by the package validators)

A manual-override child version (its parent is the root version `pv_01KDVDNAZ83BAMMYCEGWF33DPM`, created by a model run):

```json
{
  "plan_version_id": "pv_01KDVDNBYGX5V5HY2JSK5XWKHC",
  "plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM",
  "parent_plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "input_snapshot_id": "snap_01KDVDNAZ83BAMMYCEGWF33DPM",
  "configuration_id": "cfg_9eda1821d7a3f8b5c965f4375058e544a3aba022644d6a94d9e446a63100a5f0",
  "model_version": "mv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "run_id": null,
  "origin": "manual_override",
  "status": "pending_validation",
  "checksum": "sha256:c3518bb8bfc4ec431c12373f42319945255bdbd1e6cf60d7ef94aca8a28655b9",
  "domain": "finance",
  "domain_schema_version": "1.0",
  "content": {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.6}], "cash_weight": 0.4},
    "constraints": {"long_only": true, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5}
  },
  "content_ref": {
    "artifact_id": "art_01KDVDNZFG70MWE1HWERS847WY",
    "owner": "financialplanning",
    "kind": "plan_content",
    "checksum": "sha256:0573287b11f3da0b8ee26b4027d7a4f6ddb1c08e614eb93c70b4d51b8a7c740c",
    "content_type": "application/json"
  },
  "created_at": "2026-01-12T14:30:00Z",
  "synthetic": true
}
```

A publication of the validated root version, followed by a paper execution of that publication:

```json
{
  "publication_id": "pub_01KDVDNAZ83BAMMYCEGWF33DPM",
  "plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM",
  "plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "plan_version_checksum": "sha256:60f424bfa087ab458b0e32518ba51734e1009d8833294b4b73aaa9d706ed9008",
  "plan_version_status": "validated",
  "supersedes_publication_id": null,
  "published_at": "2026-01-12T14:30:00Z",
  "synthetic": true
}
```

```json
{
  "execution_id": "exe_01KDVDNAZ83BAMMYCEGWF33DPM",
  "publication_id": "pub_01KDVDNAZ83BAMMYCEGWF33DPM",
  "mode": "paper",
  "status": "recorded",
  "requested_at": "2026-01-14T14:30:00Z",
  "synthetic": true
}
```

The same documents live under `fixtures/plan-version/valid/manual-override-child.json`,
`fixtures/publication/valid/publication.json` and `fixtures/execution/valid/paper.json`. Run
`uv run finplan-conformance validate --schema execution ../fixtures/execution/invalid/live-mode.json --envelope`
to see the `OPERATION_NOT_PERMITTED` envelope that a `live` mode produces.

## Environments: single-account decision and isolation

**Decision (D5; OQ-1 RESOLVED 2026-10-07):** beta, gamma and prod all live in **one AWS account**
in the primary region `us-east-2`. Spec:
[environment-promotion](../openspec/changes/establish-cross-repo-contracts/specs/environment-promotion/spec.md),
"Single-account environment isolation".

Isolation mechanisms:

1. **Naming.** Every resource name and SSM path carries the environment segment.
2. **Tags.** Every resource carries `environment` = `beta|gamma|prod|shared`, plus `project`,
   `owner-repo` and `logical-role` (and `run-id` on SageMaker jobs).
3. **Permission boundaries.** Every role a pipeline creates has an environment permission
   boundary. The boundary explicitly denies actions on resources tagged with another environment
   or named under another environment's prefix (`templates/permission-boundary-<env>.json`). A
   synthesized role without the boundary fails the build-stage policy check (ENV-18). Since
   0.2.0 the name-based deny lists one ARN pattern per service (S3, DynamoDB, Lambda, IAM roles
   and policies, SSM, Secrets Manager, SQS, SNS, CloudWatch Logs, EventBridge rules and
   schedules, Step Functions, CloudFormation, SageMaker) built from the `${AWS::Partition}`,
   `${AWS::Region}` and `${AWS::AccountId}` pseudo parameters, so IAM and cfn-lint (E3510) accept
   it. Every boundary stays below IAM's managed-policy size limit (6,144 characters), which a
   unit test checks. Other services are covered by the tag-based deny.
4. **Separate per-environment resources:** buckets, tables, API endpoints, Lambdas, Gateway and
   Runtime instances, and job queues.
5. **Research roles** may read approved snapshots and write the staging prefix only. They are
   explicitly denied on platform metadata, publication and execution
   (`templates/research-permission-boundary-<env>.json`; ENV-04).
6. **Live-financial separation.** Every pipeline-created role carries a deny list covering
   trading and brokerage secrets, payments and wallet actions
   (`templates/live-financial-deny.json`). `live-perm-scan` enforces it statically (ENV-05).

**Account-level (`shared`) exceptions.** These exist once per account: pipeline tooling, the
project budget and its action, the contract registry, digest-addressed ECR repositories, and
pre-existing third-party secrets referenced by name (the Jev API key). Each has one owner, is
tagged `environment` = `shared` and holds no environment data (ENV-16). Account-level quotas are
external limits, not shared leases. Prod smoke tests use a dedicated synthetic portfolio flagged
`synthetic: true`.

**Multi-account is a possible future migration, not a recommendation.** Because every name
already carries the environment, moving to one account per environment (AWS Organizations) would
change only environment configuration (the target account per environment). Contracts, SSM names
and schemas would not change.

Trade-off (design, Risks): one account has a weaker blast-radius boundary. Naming, environment
tags, boundaries with environment-tag denies, separate resources and the gamma isolation test
suite mitigate it.

## Region decision, quotas and cost notes

**Region: `us-east-2` (D8).** Evidence: all existing project resources are there and the
AgentCore control plane responds there. The vLLM DLC image used by the prior Qwen runs is
available there, the GPU quotas below exist there, and the GitHub CodeConnections are there. The
choice was not made from the user's timezone. Region values come from environment configuration,
never from literals in application code (ENV-02).

**Quota notes (observed read-only on 2026-10-07; partial):**

| Instance type | Training | Processing | Endpoint |
|---|---|---|---|
| `ml.g6.12xlarge` (4x L4) | 1 | **0** | 1 |
| `ml.g5.xlarge` | 1 | 0 | 2 |
| `ml.m5.xlarge` | 15 | 1 | n/a |

- A GPU Processing-Job benchmark needs a quota increase (OQ-8). The alternatives are the
  Training-Job batch pattern (quota 1) or a short-lived endpoint (quota 1).
- With a training quota of 1, at most one Qwen job runs at a time, so FinanceModel uses a
  concurrency lease and a queue. A quota increase is requested only when a planned GPU run fits
  the `gpu` allocation.

**Cost notes (no prices recorded).** Prices are not written here. They must come from current
AWS pricing at decision time; the bootstrap pre-run, for example, queries the pricing API. Cost
drivers:

- GPU instance-hours for SageMaker jobs or endpoints (billed as managed SageMaker, not raw EC2)
- S3 storage of about 52 GiB of staged weights
- AgentCore usage
- CodeBuild minutes
- Bedrock invocations for explanations (inside `bedrock_explanations`)

TypeSafe Jev calls are billed by TypeSafe from prepaid credits, outside AWS and outside the
USD 50 ceiling. Observed reference: the last Qwen batch run lasted 4754 s on one
`ml.g6.12xlarge`.

## User decisions of 2026-10-07 (D11)

These override earlier assumptions in the design.

- **Single AWS account (OQ-1 resolved).** See [Environments](#environments-single-account-decision-and-isolation).
- **Bootstrap credentials (OQ-11 resolved, non-blocking).** The one-time bootstrap uses the user's
  existing authenticated AWS CLI session. No pre-existing scoped human role is required, and the
  "refuse root principal" rule is dropped. The bootstrap still creates the scoped pipeline, deploy
  and service roles, and only those deploy afterwards. Moving the human operator to a scoped/MFA
  role later is a recommendation, not a blocker.
- **GitHub (OQ-2 non-blocking).** The repos are `FilippoLentoni/{FinancialPlanning,FinanceModel,FinanceLambdasTool,FinanceAgent}`
  (public, `main`). Each pipeline reuses an existing AVAILABLE CodeConnection in us-east-2,
  referenced through `/finplan/shared/<repo>/config/codeconnection-ref` and never by ARN. A
  source-stage dry run proves access. Only if it fails does the user extend the GitHub App
  installation.
- **Budget (OQ-7 resolved).** USD 50 is the total AWS budget for everything; it is not split per
  repository. The default category allocation lives in
  `/finplan/shared/financialplanning/config/budget-allocation` and is configurable:

  | Category | USD | Covers |
  |---|---|---|
  | `platform_infra` | 8 | Platform and serverless infrastructure and storage, all repos |
  | `cpu_research` | 7 | FinanceModel CPU jobs |
  | `bedrock_explanations` | 5 | FinanceAgent Bedrock calls |
  | `gpu` | 25 | FinanceModel Qwen3.6-27B and RL GPU jobs |
  | `reserve` | 5 | Reserve |

  The categories must sum to at most the ceiling (`budget check-allocation`). Enforcement has
  three parts: the paying repository runs a pre-flight check against its category (exhausted
  means `BUDGET_EXCEEDED`), an AWS Budgets budget sends alerts at 50%, 80% and 100%, and a deny
  action applies at 100% to the published enforced role names. Every GPU run also needs explicit
  user approval of a cost estimate. Jev usage is billed by TypeSafe, tracked separately and not
  counted.
- **Explanation LLM (OQ-3 resolved).** Amazon Bedrock is the default explanation provider in
  FinanceAgent (IAM auth, no API key, billed inside `bedrock_explanations`). The model ID is SSM
  configuration (now resolved by OQ-13, see D12). No OpenAI secret exists; OpenAI may become an
  optional future adapter. Qwen3.6-27B is a separate benchmark strategy, not the explanation
  provider.
- **Market data (OQ-5, partially resolved by D11 and completed by D12).** The initial instrument
  is S&P 500 exposure through a tracking-ETF daily series (SPY), kept distinct from the index
  level and the constituent universe. Phases 1 and 2 use daily completed observations only; there
  is no intraday data. Phase 1 uses fixture and mock providers.
- **TypeSafe Jev (OQ-4 resolved).** An external HTTPS decision API (TypeSafe AI "System One") with
  bearer-token auth. The key is the pre-existing secret `finplan/shared/financemodel/jev-api-key`,
  and its name is published at `/finplan/shared/financemodel/secret-ref/jev-api-key` (owner
  FinanceModel, account-level shared). FinanceModel records the exact `model` value of each
  response. The strategy runs in phase 2 behind explicit user approval, and its spend is outside
  the AWS budget.
- **Unchanged:** no live trading, no deployment during spec work, and public repos contain no
  account IDs, ARNs, bucket names or secret values.

## Round 2 user decisions (D12)

Recorded 2026-10-07. These override D11 where they differ.

- **Market-data provider: `yfinance` via the platform only (OQ-5 RESOLVED 2026-10-07; platform PQ-5).**
  - **Provider.** The phase 2 daily provider is the Python library `yfinance` (Yahoo Finance),
    used behind the platform's provider-adapter interface for SPY daily completed OHLCV, adjusted
    close, dividends and splits.
  - **Caveats.** `yfinance` is unofficial and not affiliated with Yahoo. It needs no API key. It
    is rate-limited and can break when Yahoo changes upstream. Yahoo's terms cover personal and
    research use.
  - **Consequences.**
    - Retrieved market data is never committed to any public repository; fixtures stay synthetic.
    - The library version is pinned.
    - Snapshot lineage records the library and the retrieval timestamp.
    - Empty or partial responses become quality flags.
    - Calls retry with backoff.
  - **Fallback.** A fallback (for example Stooq via `pandas-datareader`) can be added later
    without a contract change.
  - **Exchange calendar.** `exchange_calendars` (or `pandas_market_calendars`), calendar
    **XNYS**, pinned version.
  - **Where it runs.** Inside the platform ingestion Lambda or container. CI uses the mock
    provider only. An optional live-provider test is rate-limited and asserts shape, not values.
  - **Only the platform calls the provider.** It is an `external` matrix row whose sole consumer
    is FinancialPlanning ingestion. FinanceModel reads only approved platform snapshots, and
    FinanceLambdasTool calls the platform ingestion and snapshot API.
  - **Contract consequences (in 1.0.0).**
    - The snapshot provider-lineage block gains optional `provider_library` and `library_version`
      next to the retrieval timestamp.
    - The `finance/v1` daily observation gains optional `adj_close`, `dividend` and `split_ratio`.
    - The open quality-flag vocabulary registers `empty_response` and `partial_response`.
- **Explanation model: Claude Opus 5 as configuration (OQ-13 RESOLVED 2026-10-07).**
  - **Model.** Claude Opus 5 through the US cross-region inference profile
    `us.anthropic.claude-opus-5` (foundation model `anthropic.claude-opus-5`). A read-only
    inference-profile listing verified it ACTIVE in us-east-2. The `us.` profile, not `global.`,
    keeps data routing in the US.
  - **Configuration.** It is the value of `/finplan/<env>/financeagent/config/explanation-model-id`.
    It is configuration, not code, and beta and gamma may set a cheaper model through the same key.
  - **Cost controls.** Opus is a high-cost tier against the USD 5 `bedrock_explanations`
    allocation, so FinanceAgent enforces per-invocation max-token caps, a per-session budget check,
    prompt caching where supported, and the fixture provider in CI.
  - **IAM.** The policy allows the inference profile and its underlying regional foundation-model
    ARNs, written generically in repository files.
  - **Model access.** Enabling Opus 5 model access is a bootstrap step.
- **Pipeline bootstrap approved in principle (2026-10-07).** It runs only after the bootstrap IaC
  is implemented (tasks 10.5 and 14.x, plus the platform change's bootstrap tasks). At run time
  the operator first shows the exact stacks and a cost estimate, then runs under this approval.
  Nothing is deployed during spec work. See the [bootstrap runbook](docs/bootstrap-runbook.md).

## Planned phase 2 contract minors and OQ-12

Contracts 1.0.0 contains everything phase 1 needs, plus the D10 and D12 additions. The following
items are **not** in 1.0.0. Each arrives later as a **minor** release (new schemas or optional
fields only; spec contract-schemas, "Registered vocabularies and phase 2 additions"):

| Planned minor | Contents | Consumers |
|---|---|---|
| Explanation-evidence kinds | One `finance/v1/explanation-evidence/*` schema per kind, matching FinanceAgent `add-explanation-workflows` design E2: `performance_reconciliation`, `gap_decomposition`, `forecast_position`, `data_quality_report`, `difference_inventory`, `controlled_resolve_set`, `grouped_shapley`, `sensitivity_sweep`. Each carries its source identifiers, evaluator code version, seeds, declared tolerances and check results. Explanation envelope fields ship in the same minor | FinanceAgent `add-explanation-workflows`; FinanceModel produces the evidence |
| Lineage reproducibility fields | Optional plan-version lineage fields `solver`, `solver_version`, `tolerance`, `seed`, `code_version` | FinancialPlanning, FinanceModel |
| Finance result flags | Per-period `leakage_risk` and `out_of_configuration` | FinanceModel `add-learning-and-llm-strategies`, FinanceAgent |
| Portfolio policy | A schema for risk preferences and constraints. Agents may read it but never rewrite it | FinanceAgent, FinancialPlanning |

Evidence kinds are payload shapes, not job types. The FinanceModel experiment types that produce
them (FinanceAgent design E1: `performance_decomposition`, `controlled_resolve`, `grouped_shapley`,
`sensitivity_sweep`) belong to the follow-up FinanceModel change under OQ-12.

**Cross-repo review (task 12.5, 2026-10-07).** The table above was compared with FinanceAgent
`add-explanation-workflows` (design E2 evidence kinds, GAP-E2; lineage fields `solver`,
`solver_version`, `tolerance`, `seed`, `code_version`, GAP-E3; portfolio-policy schema, EX-OQ-6) and
FinanceModel `add-learning-and-llm-strategies` (CG-8: per-period `leakage_risk` and
`out_of_configuration`). All four rows agree with both changes, and each change records them as
phase 2 contract minors (design D10). None of these schemas is in 1.0.0.

**OQ-12 (open; BLOCKER for deployed phase 2 explanations only).** Who builds the FinanceModel
explanation-evidence experiment kinds that FinanceAgent `add-explanation-workflows` needs? The
answer is a follow-up FinanceModel change, made after contracts adds the evidence schemas above.
In the meantime FinanceAgent tests use fixture evidence.

## Contracts 0.2.0: platform verification fixes (D13)

Version 0.2.0 (design D13, tasks 15.1 to 15.6) fixes the gaps that the platform's verification
against 0.1.0 found. 0.2.0 is still a 0.x pre-release, so it is beta-only. Neither 0.1.0 nor 0.2.0
was published to the registry.

- **Ownership matrix.** The rows now cover every resource the platform synthesizes, with no
  accepted gaps:
  - the plan API's API Gateway resources, deployment, stage and gateway responses;
  - the handler roles, log groups and SSM reference parameters;
  - the schedule's role, dead-letter queue, queue policy and alarm;
  - the budget roles, topic policy and subscriptions;
  - the pipeline store's bucket policy.

  It also gains the rows `platform-metadata-sweeper`, `permission-boundaries` (shared pipeline
  tooling) and `pipeline-environment-roles-<repo>`. `AWS::ApiGateway::Method` is attributed to
  its parent. The ownership check never ignores a resource.
- **New schemas** for the plan lifecycle API routes that had no contract schema (`core/v1/api/*`,
  `finance/v1/api/*`), each with valid and invalid fixtures:
  - `api/create-portfolio-request` and `api/create-portfolio-response` (finance);
  - `api/create-plan-request` and `api/create-plan-response`;
  - `api/create-root-version-request` and `api/create-root-version-response`;
  - `api/record-execution-request` and `api/record-execution-response`;
  - `api/read-snapshot-observations-response` (finance);
  - `api/get-staged-output-response`.
- **Optional fields:**
  - `plan.publication_revision`;
  - the snapshot's `lineage.calendar_version`, `quality_details` and `observation_summary`;
  - in `tools/refresh-market-data-response`: `input_snapshot_id`, `new_snapshot`,
    `content_checksum`, `quality_flags`, `session_status` and `trigger`.
- **Excel template.** The template documents `CASH` as the reserved instrument ID of the
  cash-weight row.
- **Lineage field name.** The provider lineage field is `lineage.provider`. There is no
  `provider_id` field.
- **New SSM keys:**
  - `/finplan/<env>/financeagent/agent/user-pool-ref`;
  - `/finplan/<env>/financelambdastool/config/direct-test-principal-name`;
  - `/finplan/<env>/financemodel/job/job-role-ref`;
  - `/finplan/<env>/financemodel/job/job-api-role-ref`.

  `authorizer-metadata-ref` and `secret-ref/ci-test-client` were already registered.
- **Tooling fixes:**
  - the boundary check renders pseudo-parameter `Ref`s inside `Fn::Join`;
  - `copied-id` no longer crashes on YAML dates;
  - the boundaries use valid per-service ARNs (E3510).
- **One change that 0.x allows but a 1.x release would not.**
  `tools/refresh-market-data-response` no longer always requires `snapshot`. A non-session day
  with no earlier snapshot answers without one, with `no_session`, `new_snapshot: false` and a
  null `input_snapshot_id`. Every other change is additive. The compatibility gate against 0.1.0
  passes with `--allow-zero-major-breaking`, the semver initial-development rule for 0.x. It
  reports this schema's two entries as its only breaking changes. Such a change after 1.0.0 would
  need a new major.

## Contracts 0.2.1: dead deny entries and pipeline log groups (D14)

A patch release before the first bootstrap. No schema changes; the compatibility gate against
0.2.0 passes as a patch.

- **Live-financial deny list.** The `bedrock-agentcore-control:*Payment*`, `*Wallet*` and
  `*Funds*` entries are removed. The AWS Service Authorization Reference documents one IAM service
  prefix for Amazon Bedrock AgentCore, `bedrock-agentcore`, which covers its control-plane and
  data-plane APIs. `bedrock-agentcore-control` is an SDK client name, not an IAM prefix, so those
  entries matched no action. The existing `bedrock-agentcore:*Payment*`, `*Wallet*` and `*Funds*`
  entries keep the same coverage. Every permission boundary and `templates/live-financial-deny.json`
  are regenerated.
- **Budget enforcement deny list.** `bedrock:Converse*` is removed: no IAM action has that name.
  The Converse and ConverseStream APIs are authorized by `bedrock:InvokeModel` and
  `bedrock:InvokeModelWithResponseStream`, which `bedrock:InvokeModel*` already denies.
- **Ownership matrix.** The `pipeline-financialplanning` row lists `AWS::Logs::LogGroup`: the
  explicit 30-day log groups of the FinancialPlanning CodeBuild projects, tagged with the
  `pipeline-build-project` logical role. A named log group references no template resource, so it
  cannot be a parent-attributed helper. The checker is not relaxed.

## Contracts 0.2.2: the budget action can reset its own action (D15)

A patch release after a production incident. No schema changes; the compatibility gate against
0.2.1 passes as a patch.

- **Incident (2026-10-07).** The budget action attached `finplan-budget-enforcement-deny` to 11
  pipeline roles, then its reset failed with `RESET_FAILURE`: the shared permission boundary,
  which also bounds the action's own execution role, denied that role `iam:DetachRolePolicy` of
  the deny policy.
- **Fix.** In `finplan-shared-permission-boundary` only, the role detach deny exempts principals
  whose `aws:PrincipalArn` matches
  `arn:${AWS::Partition}:iam::${AWS::AccountId}:role/finplan-shared-*-budget-action-role`. The
  role's own policy allows attach and detach of exactly the deny policy. Every other principal
  under a boundary is still denied the detach, user and group detaches are never exempt, and
  `budgets:ExecuteBudgetAction` stays denied, so lifting the cap is still a human decision.
- **Guard.** `ProtectBudgetActionRole` denies shared-boundary principals `iam:CreateRole`,
  `UpdateAssumeRolePolicy`, `PutRolePolicy`, `AttachRolePolicy`, `PutRolePermissionsBoundary`
  and `PassRole` on that name pattern, so no automation can obtain a role that matches the
  exemption.

## Contracts 1.0.0: first stable release, FinanceModel rows and the contract registry (D16)

The first stable release. 1.0.0 may be pinned in **beta, gamma and prod** (the 0.x beta-only
rule no longer applies to consumers that re-pin). Schemas are unchanged: the compatibility gate
against 0.2.2 passes as a major bump with no schema change (`finplan-conformance compat --old
<0.2.2 data> --new contracts`), so 0.2.2 records stay valid under the same `v1` `$id`s. Producers
that hold 0.2.2 records keep serving major 0 next to 1 (the platform does, `served_contract_majors`
`[0, 1]`).

- **Ownership matrix: the FinanceModel rows its verification asked for.** Each pair maps to a
  FinanceModel row, so FinanceModel can deploy its job API and job role with zero ownership
  problems. Nothing else changed for FinanceModel.

  | Row | Added |
  |---|---|
  | `job-interface` | `AWS::ApiGateway::Deployment`, `AWS::ApiGateway::Stage` (the job REST API's deployment and stage) |
  | `sagemaker-job-definitions` | logical role `job-execution-role` and `AWS::IAM::Role` (the per-environment job-execution role named by `job-role-ref`) |
  | `model-registry` | `AWS::S3::BucketPolicy` (the registry bucket policy) |
  | `pipeline-financemodel` | `AWS::Logs::LogGroup` (explicit CodeBuild project log groups, `pipeline-build-project`, as 0.2.1 did for FinancialPlanning) |

- **Contract registry.** The `contract-registry` row also lists `AWS::SSM::Parameter`: the
  FinancialPlanning tooling stack declares the CodeArtifact domain `finplan`, the repository
  `contracts` (no upstream; retained on stack deletion) and the reference parameter
  `/finplan/shared/financialplanning/contract/registry-ref`. Its value is JSON text with
  `domain`, `repository`, `region` and `formats` (`pypi`, `npm`); it never holds an account ID or
  an endpoint (`finplan_contracts.registry.parse_registry_ref`). The domain owner is the reader's
  own account (single account, D5).
  - Packages: `finplan-contracts` (pypi) and `@finplan/contracts` (npm).
  - Publishing: only the FinancialPlanning build stage, after every gate passed. A version is
    published once. When the version already exists, the step compares SHA-256 digests (the wheel
    byte for byte; the npm tarball byte for byte or, failing that, by its file contents) and either
    does nothing or fails the build. It never overwrites, deletes or disposes a version.
  - Reading: `finplan_contracts.registry.read_policy()` is the identity policy a consumer attaches
    to its build roles (`codeartifact:GetAuthorizationToken` on the domain,
    `GetRepositoryEndpoint` and `ReadFromRepository` on the repository, `sts:GetServiceBearerToken`
    for CodeArtifact only, and a read of the reference). The domain and repository resource
    policies admit the build roles of the other repositories by name pattern
    (`finplan-shared-<repo>-*`, this account only), for reads only.
- **SSM convention: account-level tooling role names.** New registered key
  `/finplan/shared/<repo>/config/budget-enforced-role-names` (value: role names, written only by
  that repository's bootstrap). It lists a repository's account-level tooling roles, for example
  its pipeline and build roles, so that the budget action can deny them. The FinancialPlanning
  bootstrap reads, for every repository, the `shared` list plus the beta, gamma and prod lists
  (`finplan_contracts.ssm.budget_enforced_role_name_keys`). The budget template fragment takes the
  four `shared` lists too.
- **Bootstrap cost estimate.** `AWS::CodeArtifact::Domain` is listed as not billed (its
  repositories bill storage and requests; the repository has a pricing rule).

Consumers: re-pin `finplan-contracts==1.0.0` by version and SHA-256 (see
[docs/consumer-pinning.md](docs/consumer-pinning.md)). Until the registry is provisioned (the
FinancialPlanning tooling bootstrap re-run), the wheel built reproducibly by the platform is
vendored byte for byte; afterwards the same bytes resolve from CodeArtifact.

## Contracts 1.1.0: research universe and daily recommendation (backward-compatible minor)

Change `add-research-universe-and-daily-loop` (user decisions 16-22 of 2026-10-08). Every addition is
optional, an open-enum value, a new `$defs` entry or a new schema; the compatibility gate against
1.0.0 (`finplan-conformance compat --old contracts/python/tests/data/releases/finplan-contracts-schemas-1.0.0.tar.gz`)
passes as a **minor** without the 0.x exemption, and every 1.0.0 fixture is byte-identical (CON-02).

- Dataset `finance/equity-etf-daily/<subject>` and `instrument.kind` (`etf`, `equity`, `cash`; open
  enum) with `return_assumption` (`zero_nominal`) for the modeled cash instrument.
- Snapshot payload `universe` block (instruments with kinds, `history_start`, `return_basis`
  `adj_close`) and `bias_disclosures` (`hindsight_selection`, `survivorship`; open enum) on input
  snapshots, snapshot payloads and staged-output manifests.
- Job kind `daily_recommendation` (`common.json#/$defs/job_type`, open enum) and the optional
  `plan_id` of a job submission.
- The FinanceModel-owned key `/finplan/<env>/financemodel/config/production-strategy` holding
  `core/v1/production-strategy.json`: single writer FinanceModel's runtime `strategy-selection`
  principal, reader the FinancialPlanning daily trigger (`ssm.REGISTERED_KEYS`, matrix row
  `production-strategy-config`, not declared in IaC). Absent or empty means no strategy.
- The FinanceLambdasTool tool `production_strategy` (`core/v1/tools/production-strategy-{request,response}.json`;
  `get`, `set`, `clear`).
- Matrix row `daily-recommendation-trigger` (FinancialPlanning): state machine, step function, start rule.

Consumers move to 1.1.0 only after FinancialPlanning serves it in that environment (consumer promotion gate).

## Open-questions register

This register is copied from the design. Specs hold regardless of the answers. Items marked
BLOCKER stop the named step until resolved.

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

Still open: OQ-6, OQ-8, OQ-9, OQ-10 and OQ-12.
