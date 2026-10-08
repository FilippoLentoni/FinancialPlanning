# Daily recommendation trigger

Change `add-research-universe-and-daily-loop` (capability `daily-recommendation-trigger`, decisions 18-22).

After the 09:00 America/New_York scheduled ingestion commits the research-universe snapshot
(`finance/equity-etf-daily/research-universe`), the ingestion function's asynchronous success
destination puts an event on the default EventBridge bus. A rule matching a **scheduled universe**
ingestion starts the Step Functions Standard state machine
`finplan-<env>-financialplanning-daily-trigger`:

```
CheckSnapshot -> ReadStrategy -> CheckBudget -> SubmitJob -> (Wait 5 min -> Poll) x <= 9 -> Accept -> WriteOutcome
```

Each state calls the step Lambda `finplan-<env>-financialplanning-daily-trigger-step`
(`finplan_platform.handlers.daily_trigger.handler`; logic in `finplan_platform.core.daily_trigger`).

## Switch

The only switch is FinanceModel's production-strategy key
`/finplan/<env>/financemodel/config/production-strategy` (contracts 1.1.0, single writer: FinanceModel's
strategy-selection operation; reader: this trigger). Absent, empty, or without `strategy_id` means the
daily job is a **no-op**: no job, no benchmark, no plan version, no SageMaker cost. Deleting the key stops
the daily jobs immediately.

## Outcomes

Every execution writes one outcome record (write-once JSON in the environment's `reports` bucket under
`daily-trigger/outcomes/<session_date>/`), readable through `GET /v1/daily-trigger/outcomes/{session_date}`
(platform and operator principals). The names match `finplan_platform.core.daily_trigger.OUTCOMES`
(a unit test checks this list):

| Outcome | Meaning | FinanceModel calls |
|---|---|---|
| `no_session` | the scheduled ingestion found no session (weekend or holiday) | none |
| `skipped_snapshot` | the universe snapshot is not `approved` (for example `partial_response` naming a ticker), or the event is not a scheduled universe snapshot | none |
| `skipped_no_strategy` | no production strategy is set | none |
| `skipped_budget` | the 100% budget deny action is active (pre-check), or FinanceModel answered `BUDGET_EXCEEDED` | none, or one refused `submit_job` |
| `pending_approval` | the run succeeded and the staged output was accepted as an **unpublished** plan version (origin `model_run`, carrying the snapshot's `bias_disclosures`); the record names `plan_version_id` | one `submit_job`, at most 9 `get_job_status` |
| `no_version` | the run finished without a committable solution (acceptance outcome `no_version`) | as above |
| `run_failed` | the run failed or was cancelled, or its staged output was rejected | as above |
| `timed_out` | no terminal job state within 9 polls x 5 minutes (45 minutes) | as above |

## Idempotency

The execution name and the FinanceModel idempotency key are both `daily-<env>-<session_date>`; a test
start may add `-<run_tag>` (lowercase, at most 32 characters) to both. A duplicate start observes the same
`run_id`, so one SageMaker job runs per session. Acceptance uses the key `accept-<run_id>`.

## Publication

The trigger never publishes. Publication stays the user's explicit approval through the existing publish
route (agent or API). The trigger role and the scheduler role are denied the publish route twice: an
explicit IAM deny on `POST /v1/plans/*/publications`, and the API router's `automation` role class, which
is not granted that route (DLY-06).

## Research plan

`scripts/research_plan.py` (post-deploy, idempotent) creates one hypothetical paper portfolio and plan per
environment (`synthetic: true`, no real holdings) and writes its `plan_id` to
`/finplan/<env>/financialplanning/config/research-plan-ref`. Re-running it changes nothing.

## Cost per run

| Case | Cost |
|---|---|
| No strategy (default) | about 6 state transitions, one Lambda call per state: well under USD 0.01 per month |
| Strategy set | at most one `ml.m5.xlarge` CPU job of at most 30 minutes: about USD 0.12 per trading day (about USD 2.50 per month), charged to `cpu_research` by FinanceModel; about 30 state transitions per day |

Deployed tests submit at most one `buy_and_hold` job per beta or gamma suite (DLY-08); the prod smoke
never submits.
