# Plan lifecycle API

The single plan API of the FinancialPlanning platform (OpenSpec change `add-platform-foundation`,
tasks 4.1 to 4.8 and 5.1 to 5.4; requirements in `specs/plan-lifecycle-api/spec.md`). The
website, Excel import, scheduled workflows and the FinanceLambdasTool MCP adapters all call these
routes; no client writes plan state any other way.

- **Code:** router and Lambda adapter in `platform/finplan_platform/handlers/api.py`; operations in
  `platform/finplan_platform/core/{plans,versions,validation,publication,execution,upgrade,contract_io,snapshot_reads}.py`;
  stack in `infra/stacks/api.py`.
- **Tests:** `tests/unit/test_api_*.py` and `tests/contract/test_api_contract.py` (all offline).
- **Contract package:** `finplan-contracts` pinned at **0.2.1** (a 0.x pre-release, beta only;
  see `contracts-pin.json`). The served contract major is therefore `0` until 1.0.0 is published
  and pinned.

> Public repository: this page holds no account ID, ARN with an account, bucket, role or
> endpoint value. Examples are synthetic (`synthetic: true`) and use the `<account-id>` placeholder.

## Endpoint and authentication

- One REST API per environment. Its invoke URL is published only as the SSM parameter
  `/finplan/<env>/financialplanning/api/plan-endpoint`. Resolve it at deploy or run time in the
  same environment.
- Every method requires **IAM (SigV4)** authorization. API Gateway rejects an unsigned or
  wrongly signed request before the handler runs, with a contract error envelope
  (`UNAUTHORIZED`, or `FORBIDDEN` when the resource policy denies the caller).
- The handler takes the caller principal from the identity API Gateway verified. It never takes
  it from the body. An assumed-role session ARN is normalized to its role ARN, so retries from
  new sessions keep the same idempotency scope.

### Who may call what

The API resource policy and the handler enforce the same route table
(`finplan_platform.handlers.api.ROUTES`). Role classes come from the `consumer_principals`
role-name patterns in `config/<env>.json`.

| Role class | Principals (pattern in config) | Routes |
|---|---|---|
| platform, website, operator | `finplan-<env>-financialplanning-*` (website-path role `...-website*`, operator `...-operator*`) | every route |
| `reader` (FinanceLambdasTool) | `finplan-<env>-financelambdastool-tool-role-reader*` | every `GET` route |
| `submitter` | `...-tool-role-submitter*` | `GET` routes plus `POST /v1/ingestions` |
| `plan-writer` | `...-tool-role-plan-writer*` | `GET` routes plus version create, validate and publish |
| FinanceModel job role | `finplan-<env>-financemodel-job-execution*` (registered key `/finplan/<env>/financemodel/job/job-role-ref`) | `GET /v1/snapshots/*` only |
| FinanceModel job-API role | `finplan-<env>-financemodel-job-api-handler*` (registered key `/finplan/<env>/financemodel/job/job-api-role-ref`) | `GET /v1/snapshots/*` and `GET /v1/staged-outputs/*` |
| anyone else, including every other environment's roles | | nothing (explicit deny) |

No tool role may call the execution, Excel import/export or staged-output accept routes in
phase 1. FinanceModel principals are denied every write route.

### Transport headers

These headers are outside the hashed request body, so they never change an idempotency request hash.

| Header | Meaning |
|---|---|
| `X-Correlation-Id` | Optional. Echoed in every response and error envelope when it matches the contract pattern; otherwise a new ID is minted |
| `X-Finplan-Contract-Version` | Optional contract version the client was built against (the body field `contract_version` is equivalent) |
| `X-Finplan-Caller` | Optional contract `core/v1/caller.json` on-behalf-of block from a trusted hop. It is recorded in audit events and never grants authority |

Responses carry `X-Correlation-Id`, and `X-Idempotent-Replay: true` when a write returns a stored
result.

## Routes

| Method and path | Operation | Request schema | Response schema | Idempotency key | `expected_revision` |
|---|---|---|---|---|---|
| `POST /v1/portfolios` | create synthetic portfolio | `api/create-portfolio-request` | `api/create-portfolio-response` | required | n/a |
| `GET /v1/portfolios/{portfolio_id}` | portfolio read | path | `portfolio` | n/a | n/a |
| `POST /v1/portfolios/{portfolio_id}/plans` | create plan | `api/create-plan-request` | `api/create-plan-response` | required | portfolio revision |
| `GET /v1/plans/{plan_id}` | plan head | `tools/get-plan-request` | `tools/get-plan-response` | n/a | n/a |
| `GET /v1/plans/{plan_id}/versions` | version list (`page_size`, `next_token`) | `tools/list-plan-versions-request` | `tools/list-plan-versions-response` | n/a | n/a |
| `POST /v1/plans/{plan_id}/versions` | root or child version | `api/create-root-version-request` (no parent) or `tools/create-override-version-request` (child) | `api/create-root-version-response` or `tools/create-override-version-response` (override) | required | plan head revision |
| `GET /v1/plan-versions/{plan_version_id}` | version read (`download=true` adds a grant) | path | `tools/get-plan-version-response` | n/a | n/a |
| `POST /v1/plan-versions/{plan_version_id}/validate` | deterministic validation | `tools/validate-plan-version-request` | `tools/validate-plan-version-response` | required | n/a (status-conditional) |
| `POST /v1/plans/{plan_id}/publications` | publish a validated version | `tools/publish-plan-version-request` | `tools/publish-plan-version-response` | required | publication head revision |
| `GET /v1/publications/{publication_id}` | publication read | path | `publication` | n/a | n/a |
| `POST /v1/publications/{publication_id}/executions` | paper or simulated execution | `api/record-execution-request` | `api/record-execution-response` | required | n/a |
| `GET /v1/executions/{execution_id}` | execution read | path | `execution` | n/a | n/a |
| `GET /v1/snapshots/{input_snapshot_id}` | snapshot metadata (`download=true` adds grants) | path | platform `snapshot-response` (`snapshot` is `input-snapshot`) | n/a | n/a |
| `GET /v1/snapshots/{input_snapshot_id}/observations` | bounded observation read (`instrument_id`, `start_date`, `end_date`, `page_size`, `next_token`) | platform `snapshot-observations-request` | `api/read-snapshot-observations-response` | n/a | n/a |
| `POST /v1/ingestions` | on-demand ingestion (delegated) | `tools/refresh-market-data-request` | `tools/refresh-market-data-response` | required | n/a |
| `POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept` | staged-output acceptance (delegated) | platform `accept-staged-output-request` | platform `accept-staged-output-response` (`api/get-staged-output-response` plus the version's head and validation result) | required | plan head revision |
| `GET /v1/staged-outputs/{run_id}` | acceptance outcome (delegated) | path | `api/get-staged-output-response` | n/a | n/a |
| `POST /v1/plan-versions/{plan_version_id}/exports` | Excel export (delegated) | platform `export-plan-version-request` | platform `export-plan-version-response` | required | n/a |
| `POST /v1/plans/{plan_id}/imports` | Excel upload grant (delegated) | platform `issue-import-grant-request` | platform `issue-import-grant-response` | required | n/a |
| `POST /v1/plans/{plan_id}/imports/{import_id}/commit` | Excel import commit (delegated) | platform `commit-import-request` | platform `commit-import-response` (`import-report` plus the created version) | required | from workbook |

Unnamed schemas are contract schemas from the pinned package; the `api/...` route schemas were
added in contracts 0.2.0 (contracts design D13). *Platform* schemas exist only where the contract
has none (path and query parameters, staged-output acceptance, Excel). They live next to their
operation (`core/contract_io.py`, `core/snapshot_reads.py`, `core/staging.py`,
`excel/service.py`) and are built only from contract definitions (`$ref`), so error codes and
pointers are the contract's own. Every route validates its response before sending it; a
non-conformant response becomes `INTERNAL` and is never sent. Path parameters are merged into the request document, and a
body value that disagrees with the path is a `VALIDATION_FAILED`.

**Delegated routes** are registered in the router and imported lazily from their owning modules
(`handlers/api.py` `DELEGATES`): `core.ingestion.run_ingestion(ctx, request)`,
`core.staging.accept_staged_output` and `get_staged_output`, and the Excel module's
`export_plan_version`, `issue_import_grant` and `commit_import`. Parameters are bound by name
(`ctx`, `svc`/`services`, `repo`, `store`/`artifacts`, `cfg`/`config`, `request`/`body`,
`query`, and path parameters such as `plan_id` or `run_id`). A release without the module fails
only that route, with `DEPENDENCY_UNAVAILABLE`.

Request and response shapes, and the semantics of the delegated routes, are documented by their
owning modules: [ingestion](ingestion.md), [staged-output acceptance](staging.md) and
[Excel import and export](excel.md).

## Errors

Every error is a contract envelope (`core/v1/error.json`): `code`, `message`, `retryable`,
`details`, `correlation_id` and `contract_version`. Envelopes never carry a stack trace, a
storage location or a secret. Messages and details are validated by the contract `no_leaks`
check before they are sent.

| Code | HTTP | Retryable | Typical cause |
|---|---|---|---|
| `VALIDATION_FAILED` | 400 | no | schema violation (`details.pointer` names the field), body/path mismatch, client-supplied platform ID, storage URI in a request |
| `INVALID_IDENTIFIER` | 400 | no | an ID with the wrong prefix or format (`details.field`) |
| `UNSUPPORTED_CONTRACT_VERSION` | 400 | no | declared major not served (`details.served_contract_majors`) |
| `UNAUTHORIZED` | 401 | no | no verified IAM identity |
| `FORBIDDEN` | 403 | no | the role class may not call the route, or the principal belongs to another environment |
| `OPERATION_NOT_PERMITTED` | 403 | no | `synthetic: false` portfolio, execution `mode` other than `paper`/`simulated` |
| `BUDGET_EXCEEDED` | 403 | no | budget enforcement active (ingestion) |
| `NOT_FOUND` | 404 | no | unknown record or route; expired snapshot (details name the expiry) |
| `CONFLICT` | 409 | no | `expected_revision` differs from the current revision (`details.current_revision`) |
| `IMMUTABLE_RECORD` | 409 | no | an attempt to change committed content |
| `IDEMPOTENCY_KEY_REUSED` | 422 | no | same key, different request body |
| `PRECONDITION_FAILED` | 422 | no | publish of a version that is not `validated`; lifecycle precondition |
| `RATE_LIMITED` | 429 | yes | API Gateway or a provider throttled the request |
| `INTERNAL` | 500 | no | producer bug, including a response that failed its own schema (never sent) |
| `DEPENDENCY_UNAVAILABLE` | 503 | per case | metadata store busy, delegated module absent, producer release missing |

## Idempotency and concurrency

- Every state-changing route requires `idempotency_key` (1 to 128 of `[A-Za-z0-9_-]`).
  FinanceLambdasTool forwards its derived `lt_` key.
- The scope is (caller principal, environment, operation, key). The request hash is the
  RFC 8785 SHA-256 of the request document (body plus path parameters).
- A repeat with the same hash returns the stored response. A repeat with a different hash
  fails with `IDEMPOTENCY_KEY_REUSED`.
- Records are kept 8 days, at least the contract's 7.
- Mutable heads carry integer revisions:
  - a portfolio `revision`, which plan creation moves;
  - the plan head `revision`, which version creation moves;
  - the plan `publication_revision`, which publication moves.
- A write sends `expected_revision`. On a mismatch it fails with `CONFLICT` and nothing is applied.
- Every write is one DynamoDB transaction holding the state change, the idempotency record and the
  audit events. Artifacts are written before that transaction (write-once). An artifact orphaned
  by a crash or a lost race is never visible, and the daily sweep deletes it after 24 h.

## Versions

- **Root** (no parent): origin `model_run`, request schema `api/create-root-version-request`.
  Only a root names its lineage, and it names all of it: `input_snapshot_id`,
  `configuration_id`, `model_version` and `run_id`.
- **Child:** names `parent_plan_version_id`, which must belong to the same plan. On this route a
  child is an override (origin `manual_override`, request schema
  `tools/create-override-version-request`, accepted unchanged from the `create_override_version`
  tool). It cannot name `input_snapshot_id`, `configuration_id` or `model_version`: it inherits
  all three from its parent. Excel imports are children too (origin `excel_import`).
- Staged-output acceptance creates versions for FinanceModel runs through the same building
  block (`versions.new_version_mutation`), recording the run's own lineage from its manifest.
- FinanceLambdasTool roles (`plan-writer`) may create override children only. A root from a
  tool role is `FORBIDDEN`, because roots carry FinanceModel lineage that only platform-side
  callers record.
- The platform mints `plan_version_id`. A request that carries one is rejected.
- `checksum` = `sha256:` + SHA-256 of the RFC 8785 canonical `content`. The website and agent
  paths therefore agree byte for byte.
- The content artifact holds exactly those canonical bytes, so its trusted reference checksum
  equals the version checksum.
- `no_effect: true` marks a child whose checksum equals its parent's. The child is still created.
- Every read re-hashes the stored content. A mismatch is `INTERNAL`, never silently served.

## Validation (ruleset `plan-rules-v1`)

Validation is deterministic and has no side inputs beyond the stored version, its portfolio and the
referenced snapshot. The rules are:

- `schema`: the content matches the `plan_content` schema.
- `currency`: the content currency matches the portfolio's base currency.
- `duplicate_instrument`: each instrument appears once.
- `weights_sum`: weights plus cash sum to 1 within the configured tolerance (default 1e-9).
- `no_negative_cash`: the cash weight is not negative.
- `long_only`, `max_weight`, `min_weight`: the configured constraints hold.
- `snapshot_exists`: the referenced snapshot exists and is not expired.
- `instrument_coverage`: every allocated instrument is in the snapshot.

The result is a conditional transition `pending_validation` to `validated` or `invalid`, made with
its audit event. The findings are stored as contract error envelopes in `validation_errors`,
along with `validation_ruleset_version` and `validated_at`. Validating an already final version
returns the stored result and records no new transition. `invalid` never becomes `validated`.

## Publication and execution

- A publication references exactly one `validated` version and records its checksum.
  `supersedes_publication_id` is the previous head, and the previous publication is never modified.
- Concurrent publishes with the same `expected_revision`: one succeeds, the others get `CONFLICT`.
- An execution is a separate record (`paper` or `simulated`) that references one publication. It
  never writes the publication, the version or the plan.
- `publication_superseded` is computed at write time from the publication head.

## Reads, pagination and downloads

- Reads return platform IDs, checksum, status, lineage and **trusted artifact references**
  (`core/v1/artifact-ref.json`). They never return a bucket name, an object key or an object
  version ID.
- `?download=true` adds a **time-limited presigned grant**, valid for 300 s by configuration.
  The presigned URL is the only place a storage location appears, and it is the transport of the
  grant itself. The downloaded bytes hash to the reference checksum.
- Version lists are newest first. `next_token` is opaque and bound to the plan. A version
  committed while a client is paging sorts before the first page, so no version appears twice.
- Observation reads return only observations present in the committed, checksum-verified
  payload. `uncovered_ranges` names the requested dates outside the snapshot coverage, and
  nothing is fabricated for them. Page size is at most 100.
- FinanceModel principals may read `approved` snapshots only (`PRECONDITION_FAILED`
  `snapshot_not_approved`). An expired snapshot is `NOT_FOUND`, with details naming the expiry.

## Schema upgrades (API-10)

- Each record stores the `contract_version` it was written under. Writers stamp the pinned
  version only.
- Readers echo the stored `contract_version`. They fill additive fields that an older minor lacks
  (`no_effect` false, `publication_superseded` false, `quality_flags` empty) at read time only. The
  stored record and its checksum are never rewritten.
- Requests and records under an unserved major fail with `UNSUPPORTED_CONTRACT_VERSION`.
- When a new major is adopted, the previous one is served alongside it
  (`upgrade.ADDITIONAL_SERVED_MAJORS`).

## Examples

Every example below is validated with the pinned contract validators by
`tests/unit/test_plan_api_docs.py`. The comment before each block names the schema.

Create a synthetic portfolio (`POST /v1/portfolios`):

<!-- schema: api/create-portfolio-request -->
```json
{
  "name": "Synthetic retirement portfolio",
  "base_currency": "USD",
  "synthetic": true,
  "idempotency_key": "web-pf-0001"
}
```

<!-- schema: api/create-portfolio-response -->
```json
{
  "portfolio_id": "pf_01KES9T7J0ABJ4V9ZJ6W6SW9K5",
  "name": "Synthetic retirement portfolio",
  "base_currency": "USD",
  "synthetic": true,
  "revision": 1,
  "created_at": "2026-01-12T14:30:00Z",
  "contract_version": "0.2.1"
}
```

Create a plan (`POST /v1/portfolios/pf_01KES9T7J0ABJ4V9ZJ6W6SW9K5/plans`). The portfolio moves to revision 2:

<!-- schema: api/create-plan-request -->
```json
{
  "portfolio_id": "pf_01KES9T7J0ABJ4V9ZJ6W6SW9K5",
  "name": "Core allocation",
  "expected_revision": 1,
  "idempotency_key": "web-pl-0001"
}
```

The response is the contract plan record with `publication_revision` (the publication head's
revision, the `expected_revision` of the first publish) and the portfolio's new revision:

<!-- schema: api/create-plan-response -->
```json
{
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "portfolio_id": "pf_01KES9T7J0ABJ4V9ZJ6W6SW9K5",
  "name": "Core allocation",
  "head": {"current_version_id": null, "revision": 1},
  "current_publication_id": null,
  "created_at": "2026-01-12T14:30:00Z",
  "synthetic": true,
  "publication_revision": 1,
  "portfolio_revision": 2,
  "contract_version": "0.2.1"
}
```

Create a root version (`POST /v1/plans/pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7/versions`):

<!-- schema: api/create-root-version-request -->
```json
{
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "expected_revision": 1,
  "idempotency_key": "web-root-0001",
  "domain": "finance",
  "domain_schema_version": "1.0",
  "content": {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.6}], "cash_weight": 0.4},
    "constraints": {"long_only": true, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5}
  },
  "input_snapshot_id": "snap_01KES9T7J0GRHJS1P0A0T03EHX",
  "configuration_id": "cfg_0000000000000000000000000000000000000000000000000000000000000000",
  "model_version": "mv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "run_id": "run_01KDVDNAZ83BAMMYCEGWF33DPM"
}
```

<!-- schema: api/create-root-version-response -->
```json
{
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "parent_plan_version_id": null,
  "origin": "model_run",
  "status": "pending_validation",
  "checksum": "sha256:a5a7f385a1b6b96dcb116b7fbaee6d41f4224e42f1c19ff9a3f3bda6c3352858",
  "no_effect": false,
  "revision": 2,
  "input_snapshot_id": "snap_01KES9T7J0GRHJS1P0A0T03EHX",
  "content_ref": {
    "artifact_id": "art_01KES9T7J0ABJ4V9ZJ6W6SW9KB",
    "owner": "financialplanning",
    "kind": "plan_content",
    "checksum": "sha256:a5a7f385a1b6b96dcb116b7fbaee6d41f4224e42f1c19ff9a3f3bda6c3352858",
    "content_type": "application/json",
    "size_bytes": 189,
    "domain": "finance",
    "synthetic": true
  },
  "contract_version": "0.2.1",
  "synthetic": true
}
```

A manual override from the `create_override_version` tool, forwarded unchanged with the derived
key (`POST /v1/plans/pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7/versions`):

<!-- schema: tools/create-override-version-request -->
```json
{
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "parent_plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "expected_revision": 2,
  "idempotency_key": "lt_abababababababababababababababababababababababababababababababab",
  "domain": "finance",
  "domain_schema_version": "1.0",
  "content": {
    "base_currency": "USD",
    "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.67}], "cash_weight": 0.4},
    "constraints": {"long_only": true, "max_weight": 0.8},
    "fees": {"transaction_cost_bps": 5}
  },
  "reason": "synthetic manual override"
}
```

<!-- schema: tools/create-override-version-response -->
```json
{
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE",
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "parent_plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "origin": "manual_override",
  "status": "pending_validation",
  "checksum": "sha256:0e15cc2242b8ae44106be5c7754a24ea2e3fdbea235b4c7807a0026432cbeace",
  "no_effect": false,
  "revision": 3,
  "contract_version": "0.2.1",
  "synthetic": true
}
```

Validate it (`POST /v1/plan-versions/pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE/validate`). The weights sum to 1.07:

<!-- schema: tools/validate-plan-version-request -->
```json
{"plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE", "idempotency_key": "lt_efefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef"}
```

<!-- schema: tools/validate-plan-version-response -->
```json
{
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE",
  "status": "invalid",
  "findings": [
    {
      "code": "VALIDATION_FAILED",
      "message": "allocation weights plus cash do not sum to 1 within the configured tolerance",
      "pointer": "/content/allocation",
      "details": {"rule": "weights_sum", "total": 1.07, "tolerance": 1e-09}
    }
  ],
  "validation_ruleset_version": "plan-rules-v1",
  "synthetic": true
}
```

Publish the validated root (`POST /v1/plans/pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7/publications`):

<!-- schema: tools/publish-plan-version-request -->
```json
{
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "expected_revision": 1,
  "idempotency_key": "lt_0101010101010101010101010101010101010101010101010101010101010101"
}
```

<!-- schema: tools/publish-plan-version-response -->
```json
{
  "publication_id": "pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM",
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "plan_version_checksum": "sha256:a5a7f385a1b6b96dcb116b7fbaee6d41f4224e42f1c19ff9a3f3bda6c3352858",
  "plan_version_status": "validated",
  "supersedes_publication_id": null,
  "published_at": "2026-01-12T14:30:00Z",
  "publication_revision": 2,
  "contract_version": "0.2.1",
  "synthetic": true
}
```

Record a paper execution (`POST /v1/publications/pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM/executions`):

<!-- schema: api/record-execution-request -->
```json
{"publication_id": "pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM", "mode": "paper", "idempotency_key": "op-exe-0001"}
```

<!-- schema: api/record-execution-response -->
```json
{
  "execution_id": "exe_01KES9T7J0ABJ4V9ZJ6W6SW9KQ",
  "publication_id": "pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM",
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
  "mode": "paper",
  "status": "recorded",
  "publication_superseded": false,
  "requested_at": "2026-01-12T14:30:00Z",
  "contract_version": "0.2.1",
  "synthetic": true
}
```

Plan head as a tool `reader` sees it (`GET /v1/plans/pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7`):

<!-- schema: tools/get-plan-response -->
```json
{
  "plan": {
    "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
    "portfolio_id": "pf_01KES9T7J0ABJ4V9ZJ6W6SW9K5",
    "name": "Core allocation",
    "head": {"current_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE", "revision": 3},
    "current_publication_id": "pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM",
    "publication_revision": 2,
    "created_at": "2026-01-12T14:30:00Z",
    "contract_version": "0.2.1",
    "synthetic": true
  },
  "current_publication": {
    "publication_id": "pub_01KES9T7J0ABJ4V9ZJ6W6SW9KM",
    "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
    "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
    "plan_version_checksum": "sha256:a5a7f385a1b6b96dcb116b7fbaee6d41f4224e42f1c19ff9a3f3bda6c3352858",
    "plan_version_status": "validated",
    "supersedes_publication_id": null,
    "published_at": "2026-01-12T14:30:00Z",
    "contract_version": "0.2.1",
    "synthetic": true
  },
  "synthetic": true
}
```

One page of the version list (`GET /v1/plans/pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7/versions?page_size=1`):

<!-- schema: tools/list-plan-versions-response -->
```json
{
  "plan_id": "pl_01KES9T7J0ABJ4V9ZJ6W6SW9K7",
  "versions": [
    {
      "plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KE",
      "parent_plan_version_id": "pv_01KES9T7J0ABJ4V9ZJ6W6SW9KA",
      "origin": "manual_override",
      "status": "invalid",
      "checksum": "sha256:0e15cc2242b8ae44106be5c7754a24ea2e3fdbea235b4c7807a0026432cbeace",
      "created_at": "2026-01-12T14:30:00Z"
    }
  ],
  "next_token": "example-opaque-page-token",
  "synthetic": true
}
```

Observation read across the end of the coverage
(`GET /v1/snapshots/snap_01KES9T7J0GRHJS1P0A0T03EHX/observations?instrument_id=SPY&start_date=2026-01-05&end_date=2026-01-13&page_size=2`).
The snapshot covers 2026-01-02 to 2026-01-09, so 2026-01-10 to 2026-01-13 is reported as uncovered:

<!-- schema: api/read-snapshot-observations-response -->
```json
{
  "snapshot": {
    "input_snapshot_id": "snap_01KES9T7J0GRHJS1P0A0T03EHX",
    "domain": "finance",
    "domain_schema_version": "1.0",
    "dataset": {"dataset_id": "finance/etf-daily/SPY", "dataset_version": "fixture-1"},
    "manifest_checksum": "sha256:acce5186b8f6fc59abce9a8cd4772d7eed3402a12bb1311b867366405f9f7389",
    "source_timestamps": {"earliest": "2026-01-02T21:00:00Z", "latest": "2026-01-09T21:00:00Z"},
    "lineage": {"provider": "fixture", "provider_library": "finplan-fixture-provider", "library_version": "0.0.0", "retrieved_at": "2026-01-10T13:00:00Z"},
    "coverage": {"start": "2026-01-02", "end": "2026-01-09"},
    "quality_flags": [],
    "status": "approved",
    "approval_rule_version": "approval-v1",
    "artifacts": [
      {"artifact_id": "art_01KES9T7J0GRHJS1P0A0T03EHZ", "owner": "financialplanning", "kind": "snapshot_payload", "checksum": "sha256:36591fde97d80e8bab8ac81bf3278449aa1b111f79cbb009bf7b7397fe59dd8e", "content_type": "application/json", "size_bytes": 712, "synthetic": true}
    ],
    "created_at": "2026-01-12T14:30:00Z",
    "contract_version": "0.2.1",
    "synthetic": true
  },
  "observation_kinds": ["completed_daily"],
  "instruments": [{"instrument_id": "SPY", "observation_count": 1, "first_date": "2026-01-05", "last_date": "2026-01-05", "close_min": 102.0, "close_max": 102.0, "close_last": 102.0}],
  "partial": true,
  "data_refs": [
    {"artifact_id": "art_01KES9T7J0GRHJS1P0A0T03EHZ", "owner": "financialplanning", "kind": "snapshot_payload", "checksum": "sha256:36591fde97d80e8bab8ac81bf3278449aa1b111f79cbb009bf7b7397fe59dd8e", "content_type": "application/json", "size_bytes": 712, "synthetic": true}
  ],
  "next_token": null,
  "observations": [
    {"instrument_id": "SPY", "session_date": "2026-01-05", "kind": "completed_daily", "session_status": "regular", "open": 101.0, "high": 102.5, "low": 100.5, "close": 102.0, "volume": 1000, "synthetic": true}
  ],
  "requested_range": {"start": "2026-01-05", "end": "2026-01-13"},
  "uncovered_ranges": [{"start": "2026-01-10", "end": "2026-01-13"}],
  "missing_sessions": [],
  "synthetic": true
}
```

Error envelopes:

<!-- schema: error -->
```json
{
  "code": "VALIDATION_FAILED",
  "message": "validation failed at '/input_snapshot_id'",
  "retryable": false,
  "details": {"pointer": "/input_snapshot_id", "field": "input_snapshot_id"},
  "correlation_id": "cor_docexample0001",
  "contract_version": "0.2.1"
}
```

<!-- schema: error -->
```json
{
  "code": "CONFLICT",
  "message": "expected_revision does not match the publication head revision",
  "retryable": false,
  "details": {"record_type": "plan", "expected_revision": 1, "current_revision": 2},
  "correlation_id": "cor_docexample0001",
  "contract_version": "0.2.1"
}
```

<!-- schema: error -->
```json
{
  "code": "UNSUPPORTED_CONTRACT_VERSION",
  "message": "the declared contract major is not served by this platform release",
  "retryable": false,
  "details": {"served_contract_majors": [0], "declared_contract_version": "2.0.0"},
  "correlation_id": "cor_docexample0001",
  "contract_version": "0.2.1"
}
```

<!-- schema: error -->
```json
{
  "code": "OPERATION_NOT_PERMITTED",
  "message": "operation not permitted at '/mode'",
  "retryable": false,
  "details": {"pointer": "/mode", "field": "mode"},
  "correlation_id": "cor_docexample0001",
  "contract_version": "0.2.1"
}
```

## Open points

- **Contracts 1.0.0.** The service pins 0.2.0, a 0.x pre-release, which is beta only. Gamma and
  prod need 1.0.0 once it is published, and the served major then becomes 1.
- **Root versions on the public route.** These carry FinanceModel foreign references (`run_id`,
  `model_version`) that this route does not verify against the FinanceModel registry. The
  registry check belongs to staged-output acceptance. Phase 1 data is synthetic only.
- **FinanceModel role references.** The job and job-API role keys in `config/<env>.json` are the
  registered contract keys (`financemodel/job/job-role-ref`, `job-api-role-ref`, contracts
  0.2.0); the config check rejects a reference marked registered that the contract does not
  register. The role name patterns follow the matrix logical roles until FinanceModel's
  pipeline writes the references.
- **Human website users (PQ-4, OQ-10).** Phase 1 uses the website-path role.
- **Ownership matrix.** Resolved in contracts 0.2.0: the `plan-lifecycle-api` row lists the
  API's child resources, handler role and log group, and the endpoint parameter, and the
  ownership gate passes with no accepted problem.
