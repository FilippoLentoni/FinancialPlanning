# Market-data ingestion

How the platform turns provider data into immutable input snapshots (OpenSpec change
`add-platform-foundation`, spec `market-data-ingestion`, design P5, P6 and P6a, tasks 6.1 to
6.18). Module docstrings hold the details; this page is the map and the decision record.

## Decisions

| Item | Decision | Status |
|---|---|---|
| Initial instrument (P6) | S&P 500 exposure through **one daily series of a tracking ETF** (for example SPY), dataset `finance/etf-daily/<ticker>`. The ticker is configuration (`ingest.dataset.instrument`). The index level (`finance/index-level/<index>`) and the constituent universe (`finance/universe/<index>`) are separate, registered identities and are not enabled | User decision 2026-10-07 |
| Granularity | Daily **completed** observations only, in phases 1 and 2. An intraday granularity in any configuration fails the build (ING-13) | User decision 2026-10-07 |
| Provider (OQ-5) | The `yfinance` Python library, behind the platform's provider-adapter interface | **RESOLVED 2026-10-07** |
| Calendar source (PQ-5) | The `exchange_calendars` library, exchange **XNYS**, exact pinned version, generated into a versioned artifact at build time | **RESOLVED 2026-10-07** |
| Phase 1 | Deterministic synthetic **fixture** provider only (plus the programmable mock in tests). A phase 1 configuration naming another provider fails the build (ING-10) | In force |
| Phase 2 | An environment's configuration sets `phase: 2` and `ingest.provider: yfinance`; beta first, then gamma and prod through the pipeline (task 6.18). Decision 26 (data parity): every environment ends on the same real-data configuration, each ingesting independently into its own storage; the UNI-06 gate (`config/phase2-evidence.json`) orders the promotion | Beta enabled and verified (2026-10-08); gamma enabled with beta evidence; prod follows with gamma evidence |
| Asynchronous ingestion (PQ-6) | On-demand ingestion is synchronous; retries are bounded by the function timeout. Whether real-provider ingestion needs a `202` plus status form is decided from measured beta latency in phase 2 | **Open** |

### Recorded `yfinance` caveats (user decision record, 2026-10-07)

- `yfinance` is **unofficial** and not affiliated with Yahoo.
- It needs **no API key** and no secret; nothing is stored in Secrets Manager or SSM for it.
- It is **rate-limited** and can break when Yahoo changes upstream.
- Yahoo's terms are **personal/research** use.

Consequences in this repository:

- **No retrieved market data is ever committed.** Retrieved data lives only in the platform's
  private per-environment `raw`, `curated` and `snapshots` buckets. Every fixture, including the
  adapter's shape fixture (`tests/fixtures/market_data/yfinance_history_shape.json`), is
  invented and flagged `synthetic: true`.
- Only the platform calls the provider, inside the ingestion code path. FinanceModel reads
  only approved snapshots; FinanceLambdasTool calls the platform ingestion and snapshot API.
- CI and every pipeline suite use the fixture provider and the mock only. The optional live
  shape test is opt-in, local and rate-limited (see [Testing](#testing)).

## One operation, two triggers (ING-01)

`finplan_platform.core.ingestion.run_ingestion(ctx, request, *, deps=None, svc=None)` is the
single implementation:

| Trigger | Entry | Request | Idempotency key |
|---|---|---|---|
| On demand (`POST /v1/ingestions`; agent tools via FinanceLambdasTool, website, operators) | plan-API router, in process (`svc=`) | contract `tools/refresh-market-data-request` (`dataset_id`, `start_date`, `end_date`, `granularity`, `idempotency_key`) | required from the caller |
| Daily schedule (EventBridge Scheduler) | ingestion function `finplan_platform.handlers.ingest.handler` -> `handlers.scheduler.handle_scheduled` | static input `{"source": "finplan.scheduler", "trigger": "scheduled", "dataset_id", "scheduled_time": "<aws.scheduler.scheduled-time>", "execution_id"}` | `sched-<env>-<dataset with "/" as "_">-<scheduled session date>` |

Idempotency scope is (caller principal, environment, `ingest_market_data`, key); a duplicate
scheduler delivery or a client retry returns the original result and the same
`input_snapshot_id` (ING-09). A failure before the commit (throttling, budget, validation)
records nothing, so a retry with the same key runs again.

Schedule: `cron(0 9 ? * MON-FRI *)` (or `cron(30 9 ...)`) with
`ScheduleExpressionTimezone` `America/New_York`, generated at synth time from
`ingest.schedule_time` (`09:00` default, `09:30` allowed, anything else fails the build; OQ-6).
Daylight saving never shifts the local time. Scheduler delivery failures and function failures
after two asynchronous retries go to the dead-letter queue `ingest-schedule-dlq`; a depth above
zero raises the `ingest-dlq-depth` alarm. Published references:
`/finplan/<env>/financialplanning/config/ingest-schedule` and
`/finplan/<env>/financialplanning/api/ingestion-endpoint`.

## Pipeline inside one invocation

1. Idempotency check.
2. Budget pre-check: `/finplan/shared/financialplanning/config/budget-state` enforced ->
   `BUDGET_EXCEEDED`, no provider call (COST-05).
3. Calendar resolution: a holiday or weekend makes **no provider call** and creates no snapshot;
   the result carries `no_session` and the latest committed snapshot of the dataset, or a null
   `input_snapshot_id` and no `snapshot` when none exists (the contract
   `tools/refresh-market-data-response` allows that answer since contracts 0.2.0; the router
   validates every result against it). Dates outside the calendar coverage fail with
   `PRECONDITION_FAILED` naming the coverage (ING-03). A scheduled run before the open ingests
   the most recent completed session.
4. Provider capability check: a granularity the provider does not declare fails with
   `PRECONDITION_FAILED` `provider_capability_unsupported`; intraday support is never inferred
   from daily support (ING-05). A range with no completed session yet fails with
   `PRECONDITION_FAILED` `session_not_completed`.
5. Fetch; the raw response is written to `raw` (`provider/<provider>/<dataset>/<retrieved_at>/<ulid>.json`)
   before parsing. Exhausted throttling -> `RATE_LIMITED`, other repeated failures ->
   `DEPENDENCY_UNAVAILABLE`, both retryable, no snapshot.
6. Normalize to the contract `finance/v1/observation.json` (instrument, session date, kind,
   session status, unadjusted OHLC, volume, and separately `adj_close`, `dividend`,
   `split_ratio`; adjustment basis recorded in the manifest).
7. Validate: schema, monotonic dates, non-negative prices and volumes, OHLC consistency,
   calendar alignment. Rejected records are retained in `raw` (`...-rejected.json`).
8. Dedupe-persist curated observations, one object per
   (dataset, instrument, session date, kind, source timestamp), conditional create. Identical
   content is never duplicated; a changed `completed_daily` value is kept as a second object and
   flagged `source_revised` (ING-07).
9. Write-once snapshot payload (`<id>/payload/observations.json`, contract
   `finance/v1/snapshot-payload.json`) and manifest (`<id>/manifest.json`), both tagged
   `snapshot-status=committed`.
10. One transaction: catalog row (`status` `committed`, trigger and caller principal),
    idempotency record, audit event.

Then the approval rule runs (below) and the response shows the current status.

### Observation kinds (ING-04)

- `completed_daily` only when the session has closed per the calendar (early closes included)
  **and** the provider marks the bar final.
- A provider that cannot assert finality (`yfinance`) gets `completed_daily` only after the
  session close plus `ingest.settle_delay_minutes`, flagged `finality_inferred`; a younger bar is
  excluded (listed under `quality_details.excluded`), never relabelled.
- `intraday_partial` comes only from an adapter that declares `intraday` (the test mock); the
  snapshot then carries `contains_intraday_partial`.

### Quality flags

Open vocabulary of the contract `input-snapshot` schema; details are in the record's
`quality_details`.

| Flag | Meaning | Blocks approval |
|---|---|---|
| `no_session` | holiday or weekend; no provider call (result only) | n/a |
| `missing_sessions` | expected sessions without any provider record (listed) | no |
| `partial_response` | sessions or fields missing from a non-empty response (session and field named) | no |
| `empty_response` | no rows at all after the retries for a range with expected sessions | **yes** |
| `stale_source` | rows exist but the most recent expected session is missing | **yes** |
| `rejected_records` | records rejected by validation (count and reasons) | above `ingest.approval.rejected_records_max_ratio` |
| `source_revised` | a stored completed value changed (instrument and date named) | no |
| `finality_inferred` | finality from the settle delay | no |
| `contains_intraday_partial` | intraday partial bars present | no |
| `no_new_observations` | every observation was already stored (same content, later retrieval) | no |

For an `empty_response` snapshot the contract requires a `coverage` range; it is the requested
range, and `missing_sessions` lists every session. Consumers must honour the flag.

## Snapshot results and status (ING-08, ING-12, ING-16)

The result is the contract `refresh-market-data-response` (`snapshot`, `requested_range`,
`coverage_complete`) plus `input_snapshot_id`, `new_snapshot`, `content_checksum` (payload
checksum: identical content at a later retrieval gives a new ID with the same content checksum
and `no_new_observations`) and `trigger`. The `snapshot` is the contract `input-snapshot`
record: manifest checksum, source timestamps, coverage, quality flags, dataset identity,
status, trusted artifact references (never bucket names, keys or object version IDs) and
lineage:

| Lineage field | Example |
|---|---|
| `lineage.provider` (the provider identifier) | `fixture`, `yfinance` |
| `provider_library`, `library_version` | `yfinance`, the pinned version |
| `retrieved_at` | retrieval timestamp |
| `calendar_version` (contract field since 0.2.0) | `xnys-exchange_calendars-<version>-<start>-<end>` |

Status: `committed` -> `approved` -> `expired` (and `committed` -> `expired`). Approval rule
`approval-v1` (`ingest.approval.rule_version`): approve when the snapshot has completed daily
observations and no blocking flag. Approval is an audited conditional transition recording the
rule version, mirrored afterwards as the object tag `snapshot-status=approved`, which is the
only condition under which the `snapshots` bucket policy lets the FinanceModel job role read
the objects. A lost tag write is repaired by the next settle (ingestion replays call it).

Reads for the API routes live in `core/snapshots.py`: `get_snapshot` and the bounded, paginated
`read_observations` (filters by instrument and date range, states `uncovered_ranges` and
`missing_sessions`, never fabricates an observation). FinanceModel role classes read approved
snapshots only.

## Session calendars (ING-03, ING-15)

| Calendar | File (`platform/finplan_platform/data/calendars/`) | Used with |
|---|---|---|
| XNYS from `exchange_calendars` (Apache-2.0), pinned exactly | `xnys-exchange_calendars-<version>-<start>-<end>.json` | `yfinance` and the mock |
| Synthetic fixture calendar (rule-based holidays and early closes, `synthetic: true`) | `fixture-synthetic-v1-<start>-<end>.json` | the fixture provider and tests only |

Each file lists every covered weekday as `regular`, `early_close` (with the local close) or
`holiday`; weekends are derived. The ingestion function never imports the library.

```sh
uv run python scripts/generate_calendar.py --source xnys            # regenerate (extend coverage with --start/--end)
uv run python scripts/generate_calendar.py --source xnys --check    # build gate: shipped file == regeneration
uv run python scripts/generate_calendar.py --source fixture --check
uv run python scripts/check_ingest_pins.py                          # build gate: exact pins in pyproject.toml and uv.lock
```

`yfinance` and `exchange-calendars` are pinned with `==` in the `providers` extra of
`pyproject.toml` and locked in `uv.lock`; an unpinned or drifted library, or a calendar that does
not name the pinned version, fails the build. Upgrades are ordinary changes through beta and
gamma. The calendar coverage must be extended (regenerated) before its end date; requests past
the coverage fail with `PRECONDITION_FAILED` rather than guessing.

## Packaging (task 6.17)

The ingestion function is a zip Lambda built from the build-stage bundle with the pinned
`providers` extra (`scripts/lambda_bundle.py`; 178 MiB unzipped for arm64; see
`docs/pipeline.md` "Source-only Lambda bundle"). `scripts/ingestion_package_size.py` measures the unzipped dependency
closure against the zip limit and reports `mode` `zip` or `image`; for `image` the build sets
`FINPLAN_INGESTION_IMAGE_DIR` and the stack switches to the container image defined in
`infra/docker/ingestion/Dockerfile`. The plan-API function serves the on-demand route in
process, so its bundle carries the same extra (it is the same bundle).

## Testing

The deployed universe check distinguishes a new approved snapshot from retained historical data.
On weekends or holidays, `no_session` may include the latest committed snapshot; the test verifies
that no snapshot was created and the separate latest-approved baseline is unchanged. A live
provider response with blocking quality flags must remain committed and must not replace that
approved baseline. Both cases report `UNIVERSE-FRESHNESS` with the approved coverage end. The
baseline must still contain the complete five-instrument historical universe and no sessions after
the completed cutoff. A passing infrastructure check does not assert that rejected or unavailable
latest-session market data became available.

| Gate | Command |
|---|---|
| Unit and contract suites (fixture provider, mock, mocked `yfinance` interface; sockets blocked where relevant) | `uv run pytest tests/unit/ingestion tests/contract/test_ingest_contract.py` |
| Data hygiene (ING-19): fixtures synthetic and not matching `scripts/data_hygiene_signatures.json`; no suite enables the live test; `yfinance` only in phase 2 configurations | `uv run python scripts/check_data_hygiene.py` |
| Optional live shape test: one small request, asserts columns, types and XNYS alignment, never values; excluded from CI and prod smoke | operator only: set `FINPLAN_LIVE_PROVIDER_TEST` in a local shell, then `uv run pytest tests/integration/test_live_yfinance_shape.py` |

## Open items

- **PQ-6** asynchronous ingestion form (above).
- **Task 6.18** phase 2 enablement and its beta scheduled-run verification need a deployed beta
  (bootstrap, task 10.6).
- **Contract follow-ups resolved in contracts 0.2.0:** `lineage.calendar_version`,
  `quality_details` and `observation_summary` are optional `input-snapshot` fields;
  `refresh-market-data-response` expresses a `no_session` result with no earlier snapshot; the
  ownership matrix rows `ingestion-service` and `daily-scheduler` list the IAM role, log group,
  dead-letter queue and its policy, alarm and SSM reference parameters, so the ownership gate
  passes with no accepted problem.
