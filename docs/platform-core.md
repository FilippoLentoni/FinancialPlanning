# Platform core interfaces

How the platform service is laid out and which shared interfaces every module builds on
(OpenSpec change `add-platform-foundation`, tasks 1.1-1.3, 2.x, 3.x). Module docstrings hold
the details; this page is the map.

## Layout

| Path | Contents |
|---|---|
| `pyproject.toml`, `uv.lock` | uv project, Python 3.12; `finplan-contracts` pinned by version and wheel digest |
| `contracts-pin.json`, `vendor/finplan-contracts/` | the contract pin and the pinned wheel (0.2.2, built from `contracts/python`, beta-only until 1.0.0 is published) |
| `platform/finplan_platform/core/` | operations and shared interfaces |
| `platform/finplan_platform/handlers/` | thin Lambda adapters (API, scheduler, sweep) |
| `platform/finplan_platform/providers/`, `excel/` | market-data adapters, workbook import/export |
| `infra/app.py`, `infra/stacks/` | CDK app (one Stage per environment) |
| `config/{beta,gamma,prod,shared}.json` | environment configuration (settings and SSM names only) |
| `tests/{unit,contract,integration,smoke}/`, `tests/fakes/` | test suites and offline doubles |
| `scripts/check_contracts_pin.py` | contract pin gate (`--rebuild` in CI, `--repin` to re-pin deliberately) |

Commands (offline):

```sh
uv run pytest                      # all offline suites
npx aws-cdk@2 synth                # all environments; cdk.json runs `uv run python infra/app.py`
uv run python scripts/check_contracts_pin.py --rebuild
```

## Shared interfaces

| Interface | Module | Use |
|---|---|---|
| `OperationContext(caller, env, correlation_id, clock, ids, trigger)` | `core/context.py` | first argument of every operation; `ctx.new_id("plan_version_id")`, `ctx.now_ts()` |
| `Caller(principal, role_class, channel, on_behalf_of)` | `core/context.py` | principal comes from the authenticated transport, never the body |
| `Clock`, `SystemClock`, `FrozenClock` | `core/clock.py` | all time reads; tests freeze and advance |
| `IdFactory` | `core/ids.py` | monotonic `<prefix>_<ULID>`; mints only platform IDs |
| `PlatformError(code, message, **details)` | `core/errors.py` | registered contract codes only; `.to_envelope(correlation_id)`; `from_validation(result)` |
| `EnvConfig`, `load_config(env)` | `core/config.py` | validated configuration; `schedule_expression("09:00")` |
| `ArtifactStore` | `core/artifacts.py` | `put_once` (If-None-Match, SHA-256), `get` (verified), `put_tags`, `download_grant`, `excel_upload_grant`; `key_*` layout helpers |
| `MetadataRepository` | `core/repository.py` | `commit(ops, audit=, idempotency=)` (one TransactWriteItems), `run_idempotent(...)`, `transition(...)`, `get`/`require`/`query_index`/`audit_events` |
| Ops `PutNew`, `HeadMove`, `Transition`, `Check`, `PutConditional` | `core/repository.py` | the only write primitives; cancellation reasons map to `IMMUTABLE_RECORD`, `CONFLICT`, `PRECONDITION_FAILED`, `NOT_FOUND`, replay or `IDEMPOTENCY_KEY_REUSED` |
| `version_create_mutation(...)` | `core/repository.py` | design P3 transaction: version + head move + idempotency + audit |
| `audit_event(ctx, record_id=, record_type=, operation=, prior=, new=, **details)` | `core/audit.py` | one per state change, committed in the same transaction |
| `orphan_sweep`, `expired_snapshot_sweep`, `ensure_snapshot_readable` | `core/sweeps.py` | daily sweeps; snapshot read guard |
| `FakeDynamoDB` | `tests/fakes/dynamodb.py` | in-memory DynamoDB with TransactWriteItems, condition failures, fault injection and interleaving hooks |
| `simulate`, `Principal`, `TemplateResolver` | `infra/policy_sim.py` | offline IAM simulation with resource policies over synthesized templates |
| `PlatformStack`, `platform_role`, `tag_role`, `lambda_code` | `infra/stacks/common.py` | tags, boundaries, names, code asset for every stack |

### An idempotent, transactional operation

```python
def create_child(ctx, repo, store, *, plan_id, expected_revision, body):
    def execute():
        doc = {...}                                   # mint ctx.new_id("plan_version_id")
        store.put_once("plans", key_plan_content(plan_id, doc["plan_version_id"]), payload)  # artifact first
        return version_create_mutation(repo, ctx, plan_id=plan_id, expected_revision=expected_revision,
                                       version_doc=doc, contract_version=current_version(), response={...})
    return repo.run_idempotent(ctx, operation="create_plan_version",
                               idempotency_key=body.get("idempotency_key"), request_body=body, execute=execute)
```

## CDK hooks for other stack modules

`infra/app.py` builds one `Stage` per environment with `StorageStack` and `MetadataStack`, then
calls `add_to_stage(stage, ctx)` on `infra.stacks.api` and `infra.stacks.ingestion` and
`add_to_app(app, shared, stages)` on `infra.stacks.tooling` and `infra.stacks.pipeline`, when
those modules exist. `ctx.storage.grant_artifacts(...)`, `ctx.storage.bucket_env()`,
`ctx.metadata.grant_metadata(...)` and `ctx.metadata.tables` give access to the shared
resources. Platform runtime roles must be created with `platform_role(...)` so their names match
`finplan-<env>-financialplanning-*`, the only principals the bucket and table policies admit.

## Services bundle

The plan lifecycle operations and every delegated route take `(ctx, svc, ...)`, where `svc` is
`core.services.Services(cfg, repo, artifacts, extras)`. It also exposes `.config` and `.store`,
so it satisfies the ingestion and snapshot modules' `deps` protocol. `extras` carries optional
clients (`ssm`, and in tests `ingestion_provider`, `budget_gate`, `model_registry`).
