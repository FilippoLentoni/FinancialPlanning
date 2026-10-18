# Beta modeling evidence backend

`finplan_platform.core.modeling_evidence.ModelingEvidence` is a beta-only EC2
backend interface. `from_ec2()` uses normal EC2 credentials in us-east-2 and reads
the existing reports bucket from SSM; it does not issue Cognito tokens or expose
AWS credentials to reports. Existing paid-compute approval and budget code is
unchanged. The central pipeline owner must deploy the additive metadata stack
before running this interface.

## Physical stores and queries

All table names start `finplan-beta-financialplanning-`. Existing primary keys and
items remain unchanged. Three new on-demand KMS-encrypted PITR tables use `pk`:

| Suffix | ID | Mutable fields | Indexes |
| --- | --- | --- | --- |
| research-input | input_id | none | day-index(report_key,time_key), approach-index(approach_key,time_key) |
| research-run | run_id | status, finished_at, output_ref, execution_ref; bounded invocation lease and forecast reconciliation heads | day-index, approach-index, pending-forecast-index(forecast_partition,forecast_target) |
| portfolio-observation | observation_id | none | day-index, approach-index, portfolio-index(portfolio_id,time_key), observation-time-index(approach_key,observed_key) |

`report_key` is `experiment_id#YYYY-MM-DD` in America/New_York. `approach_key`
is `experiment_id#approach_id`. `time_key` orders a canonical UTC slot plus stable
ID. `query_day(... kind='runs'|'inputs'|'observations', page_size=1..100,
next_token=...)` reads one bounded page and scopes its token to table/experiment/day.
Reports do not scan. Source bundles, exact inference inputs, registry versions,
outputs, fills, histories and observations use immutable canonical RFC8785 JSON
under the existing reports bucket's `modeling-evidence/v1/` prefix. Every read
verifies SHA-256 against DynamoDB. `read_input_bytes(input_id)` returns the original
bytes suitable for exact downloads; JSON pretty printing changes its checksum.
Large allowed artifacts are bounded to 32 MiB and kept out of DynamoDB items.

## Runtime protocol

1. `register_approaches(experiment_id, approaches, policy, recorded_at)` freezes
   distinct variants and the fair comparison policy. `read_registry` reads its
   latest immutable version through one indexed page. Policy changes require a
   separately identified experiment for existing books.
2. `ensure_portfolio(experiment_id, approach_id, initial_capital, universe,
   created_at)` creates an expressly isolated synthetic book plus baseline history.
   Ordinary saved user portfolios are rejected by this automatic interface.
3. `capture_shared_source(experiment_id, slot, payload, available_at)` freezes
   source evidence. `capture_input(experiment_id, approach_id, slot, payload,
   shared_source_ref, captured_at)` freezes the exact prepared representation
   immediately before execution. It rejects future availability fields, nonfinite
   numbers and credential fields. Same-slot retry returns original capture time;
   changed input is an immutable-record conflict, not a replacement.
4. `start_run(... input_id, portfolio_id, started_at, metadata)` reserves a
   deterministic permanent experiment/approach/slot ID. `claim_run(run_id, owner,
   at, lease_seconds=900)` atomically claims an invocation, with bounded lease and
   append-only audited attempts. A recovered invocation must read the original
   captured input; it cannot replace it. Completed slots cannot be claimed again.
5. `finish_run(run_id, outcome, finished_at)` publishes actual output or explicit
   failed/hold/skipped/unavailable/reused status, immutable once completed.
   Forecasts supplied by models must include USD expected/baseline values and
   a declared future target timestamp; allocations alone are not forecasts.
6. The common FinanceModel evaluator supplies `commit_paper_step(run_id,
   expected_revision, state, fills, execution, recorded_at)`. The method validates
   saved state, isolated ownership, original holdings revision, eligible fill time,
   evaluator/provenance/session declarations and same-execution-mark cost/wealth
   conservation. It atomically commits the existing portfolio head, existing
   portfolio-decision row, existing immutable portfolio-history row, permanent run
   execution receipt and audit. Repeated run execution returns its original receipt
   forever, including after idempotency TTL expiry. The common evaluator remains
   responsible for instrument/execution/market price semantics; this method neither
   issues brokerage orders nor invokes a weights-only evaluator with order objects.
Every committed rebalance/no-op also atomically publishes a `paper_execution`
   observation in its actual execution-day partition. It points at the same immutable
   execution receipt and carries run/input/decision IDs, decision slot, execution time,
   recorded time and resulting holdings revision. Its `payload` is the standard receipt
   with normalized fills, transaction costs and the complete evaluator's turnover and
   quote provenance. Reports must deduplicate the event and any same-day run execution
   by run/decision ID plus execution checksum, and allocate costs/fills to execution day,
   even when the issuing recommendation belongs to an earlier day. This is an immutable
   query pointer to the existing ledger receipt, not a second portfolio ledger.
7. `record_observation(...payload,observed_at,kind='valuation')` captures immutable
   valuation/status evidence, including null equity and stale/missing data instead
   of invented zero returns. `query_day` expands verified actual output and execution
   artifacts, and observation payloads for server-side reporting.

## Crash and migration behavior

S3 staging precedes atomic pointer publication. A failed S3/DynamoDB transaction
can leave an unreferenced immutable object; retry verifies identical bytes or stages
another content-addressed artifact, never publishes missing bytes or advances the
book twice. Lease recovery uses the original captured input and records an audited
attempt. The scheduler owns bounded backoff and stale-run failure status.

Forecast publication atomically adds a sparse due-work index head. The original
forecast remains inside its immutable output artifact. `query_pending_forecasts`
queries due unmatched forecasts by experiment and target timestamp regardless of
how old their decisions are; there is no seven-day expiry of pending work.
`query_observations_after` returns bounded pages ordered by actual observation time,
which is separate from hourly slot identity. `mark_forecast_realized` verifies the
original forecast checksum/book, the fresh post-horizon authoritative valuation and
cash-flow-adjusted errors, then conditionally links the immutable forecast-match
observation and removes the head from the pending partition. Retrying a crash after
the match was saved but before the head moved completes that same link. Later quotes
cannot rewrite the original forecast or permanent match. `read_run` expands verified
`forecast_realization` and exposes its `forecast_realization_ref` for reports.

No historic model inputs can be recovered retroactively unless they were originally
captured. There is no destructive backfill: existing stores remain authoritative,
new experiments start forward-only, and controlled replay must use separately
labeled experiment IDs. Gamma/prod metadata resources and access policies are
unchanged. The contract 1.12.0 enum adds `language_model`, `agentic`, `control` to
existing `reinforcement_learning`, `optimization`; callers must identify a real
family rather than label an LLM an optimizer.

Contract 1.13.0 adds `native_input_ref` for the existing platform BTC/ETH hourly
source. These isolated experiment decisions carry `input_snapshot_id: null`
because no daily snapshot was consumed. The reference identifies the already
captured shared research input and its exact checksum, plus the original native
platform dataset checksum, as-of and availability times, ordered universe and
3600-second frequency. Proposal, execution receipt and history retain the same
reference. Reads verify original bytes; neither a replacement dataset nor an
invented snapshot is accepted. Provider provenance, chronological complete candles,
source freshness, actual availability and book universe are checked before commit.
Synthetic market inputs require an explicitly controlled replay source.

This is an additive forward-only record form; no prior artifacts, keys or tables
are migrated. Existing user-facing decision requests and explicit confirmations
continue using real approved daily snapshots. The native form is available only
through the owned isolated experiment path. The four repositories must pin the
same 1.13.0 wheel before rollout. Historical 1.12.0 artifacts remain readable.

Contract patch 1.13.1 conservatively restricts the compatibility proof to old
closed object schemas without patternProperties. It preserves the exact 1.13.0
native schemas and source references, and does not replace prior wheel bytes.

## Bounded execution reconciliation

`query_run_headers(experiment_id, approach_id, before_slot, page_size, next_token)`
uses the existing approach index, newest first and strictly before the selected
slot. It returns metadata and lease heads without fetching inputs, outputs or
model tensors from S3. Cursors are scoped to the experiment, approach and cutoff.
Because a GSI can lag, the caller uses `read_run_header(run_id)` for one strong
metadata read before relying on a candidate, lease or cleanup checkpoint.

`mark_execution_superseded(run_id, replacement_run_id, recorded_at)` binds an
older unexecuted policy to a newer completed policy in the same isolated book.
Its conditional status transition atomically publishes a separate immutable
`execution_disposition_ref`, an ancillary `execution_disposition` observation and
an audit event. It does not create an execution receipt, fill, fee, portfolio
revision or ledger history entry. The original inference output and forecast stay
immutable. Execution and supersession race on the same run status; only one can
commit. Reports display successful inference with superseded execution separately.

Cleanup processes older policies oldest first and stops on any error.
`record_execution_frontier(frontier_run_id, pending_run_id, recorded_at,
previous_frontier_run_id=...)` then checkpoints a verified metadata interval in a
small immutable receipt. It rejects another unresolved policy, an unreaped running
invocation, a mismatched/corrupt prior checkpoint or more than 10,000 metadata rows.
No checkpoint advances on those failures. A prior pending policy remains an
explicit pointer even through a long sequence of unavailable/failed runs; callers
must follow it rather than expire it with a report window. A first checkpoint
checks the bounded complete history, and later checkpoints check the interval
since the previous verified frontier and its pending pointer. A v2 proof also
strongly reads every deterministic expected hourly run ID from the registered
horizon through the new frontier, independent of index omissions. Missing or
running scheduled heads prevent advancement. The strong interval is bounded to
168 slots per recovery step; a larger backlog remains an explicit blocker until
bounded recovery accounts it. Older metadata-only v1 proofs cannot authorize a
v2 barrier. This gives steady hourly work independent of the experiment's age.
`read_execution_frontier(run_id)`
verifies only that small receipt, not the original model arrays. Frontier heads
are internal attributes of the existing research-run table; no table, index,
public contract or frozen wheel changes are required.

Autonomous execution requires a verified registered hourly horizon. Successful
registry creation primes its per-invocation policy cache from a strong metadata
read and verified original artifact, so immediate GSI lag cannot change eligibility.
A fresh backend with an unavailable registry index fails with a retryable blocker.
`mark_execution_ineligible` retains a pre-horizon manual decision or a decision
created after its original slot closed as `excluded_nontrading`; the paper commit
independently rejects it. Exclusion changes neither original inference artifacts
nor book/history/fills. Any forecast head leaves the pending comparison partition,
while its original forecast and prior realization evidence remain inspectable.

New valuation metadata can carry `run_id` and `input_id` copied from the actual
payload after strong ownership/approach/slot/book binding checks. Terminal retries
can verify these metadata links without reloading large inference artifacts.
Legacy observations keep their original metadata and artifact bytes; readers
verify their full original payload when those optional metadata links are absent.
