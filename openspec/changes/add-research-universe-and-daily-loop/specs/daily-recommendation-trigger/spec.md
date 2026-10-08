# Spec Delta

## Purpose

Defines how the platform submits one FinanceModel daily recommendation after the scheduled 09:00 ET ingestion, and only when a production strategy is configured. The output becomes an unpublished plan version that only the user can publish.

## ADDED Requirements

### Requirement: Triggered only by an approved scheduled universe snapshot
The trigger SHALL run once per environment and session, after the scheduled ingestion commits an `approved` `equity-etf-daily` snapshot. In any other case it MUST record outcome `skipped_snapshot` or `no_session` and make no FinanceModel call.

#### Scenario: Snapshot not approved
- **WHEN** the scheduled universe snapshot stays `committed`
- **THEN** the trigger records `skipped_snapshot`, and the FinanceModel job API receives no request

### Requirement: No production strategy means nothing runs
Before any FinanceModel call, the trigger SHALL read `/finplan/<env>/financemodel/config/production-strategy`. If the key is absent, empty or holds no `strategy_id`, the trigger MUST record `skipped_no_strategy` and submit no job, start no benchmark and create no plan version.

#### Scenario: Strategy not chosen
- **WHEN** ingestion succeeds and the key does not exist
- **THEN** the trigger records `skipped_no_strategy`, the job API receives no request, and the plan's version list is unchanged

### Requirement: Budget pre-check
When a strategy is set, the trigger SHALL check the platform `budget-state` and record `skipped_budget` without calling FinanceModel if the deny action is active. A `BUDGET_EXCEEDED` returned by FinanceModel MUST also be recorded as `skipped_budget`.

#### Scenario: Deny action active
- **WHEN** the 100% budget deny action is active and a strategy is set
- **THEN** the trigger records `skipped_budget` and makes no `submit_job` call

### Requirement: One daily job per session
The trigger SHALL submit one `daily_recommendation` job (purpose `production_candidate`) with the approved `input_snapshot_id`, the research `plan_id` and the idempotency key `daily-<env>-<session_date>`. It waits up to 45 minutes for a terminal state. Duplicate starts MUST yield the same `run_id`.

#### Scenario: Duplicate start
- **WHEN** the trigger starts twice for one session
- **THEN** both observe the same `run_id`, and one SageMaker job runs

### Requirement: Result is an unpublished plan version
For a succeeded run, the trigger SHALL call the existing staged-output acceptance, producing a version with origin `model_run` and the snapshot's `bias_disclosures`, and record `pending_approval` with the `plan_version_id`. The trigger and scheduler roles MUST be denied the publish route.

#### Scenario: Successful daily run
- **WHEN** the run succeeds with `solution_status` `optimal`
- **THEN** a `validated` version with origin `model_run` exists, no new publication exists, and the outcome record names the version

#### Scenario: Trigger role calls publish
- **WHEN** the trigger role calls the publish route
- **THEN** the call is denied
