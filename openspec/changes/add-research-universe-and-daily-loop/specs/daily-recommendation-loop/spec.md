# Spec Delta

## Purpose

Defines the platform's daily recommendation loop. After the scheduled 09:00 ET ingestion, the platform submits one FinanceModel `daily_recommendation` job, and only when a production strategy is configured. It then accepts that job's staged output as a pending-approval plan version.

## ADDED Requirements

### Requirement: Loop starts only after a successful scheduled ingestion
The loop SHALL start once per environment and session, after the scheduled ingestion commits an `approved` `equity-etf-daily` snapshot. If the snapshot is not approved, or the day is not a session, the loop MUST end with outcome `skipped_snapshot` or `no_session` and submit no job.

#### Scenario: Universe snapshot not approved
- **WHEN** the scheduled universe snapshot stays `committed` because NFLX is missing
- **THEN** the loop records outcome `skipped_snapshot` with the `input_snapshot_id`, and no FinanceModel call is made

#### Scenario: Holiday
- **WHEN** the schedule fires on an XNYS holiday
- **THEN** the loop records `no_session` and makes no FinanceModel call

### Requirement: No production strategy means no run
Before any FinanceModel call, the loop SHALL read `/finplan/<env>/financemodel/config/production-strategy`. If the parameter is absent, empty or holds no `strategy_id`, the loop MUST end with outcome `skipped_no_strategy`. It then submits no job, runs no benchmark and creates no plan version.

#### Scenario: Strategy not yet chosen
- **WHEN** the scheduled ingestion succeeds and the production-strategy parameter does not exist in beta
- **THEN** the loop records `skipped_no_strategy`, the FinanceModel job API receives no request, and the plan's version list is unchanged

#### Scenario: Strategy set
- **WHEN** the parameter holds `{"strategy_id": "min_variance", ...}`
- **THEN** the loop proceeds to the budget pre-check

### Requirement: Budget pre-check before submission
The loop SHALL refuse to submit when the platform `budget-state` shows the AWS Budgets deny action active, recording outcome `skipped_budget`. A `BUDGET_EXCEEDED` returned by FinanceModel MUST also end the loop as `skipped_budget` and not as a failure.

#### Scenario: Deny action active
- **WHEN** `budget-state` reports the 100% deny action active and a strategy is set
- **THEN** the loop records `skipped_budget` and makes no `submit_job` call

#### Scenario: Category exhausted at FinanceModel
- **WHEN** `submit_job` returns `BUDGET_EXCEEDED` for `cpu_research`
- **THEN** the loop records `skipped_budget` with the returned estimate and creates no plan version

### Requirement: One daily_recommendation job per session
The loop SHALL submit exactly one `daily_recommendation` job per environment and session with purpose `production_candidate`. The job carries the approved `input_snapshot_id`, the configured research `plan_id` and the idempotency key `daily-<env>-<session_date>`. Retries and duplicate deliveries MUST return the original `run_id`.

#### Scenario: Duplicate scheduler delivery
- **WHEN** the loop is started twice for the same session
- **THEN** both executions observe the same `run_id`, and FinanceModel starts one SageMaker job

### Requirement: Run tracking with a bounded wait
The loop SHALL poll `get_job_status` until the run is terminal or 45 minutes have passed since submission. On timeout it MUST record `run_timeout` and leave acceptance to a later operator call.

#### Scenario: Run exceeds the wait
- **WHEN** the run is still `running` 45 minutes after submission
- **THEN** the loop ends with `run_timeout` naming the `run_id`, and no plan version is created

### Requirement: Staged output accepted as a pending recommendation
For a run that is `succeeded` with a committable solution status, the loop SHALL call staged-output acceptance with the plan head's `expected_revision`. A `validated` result MUST carry `review_state` `pending_approval`. The loop MUST NOT call publish or review.

#### Scenario: Successful daily run
- **WHEN** the run succeeds with `solution_status` `optimal`
- **THEN** a plan version with origin `model_run`, the run's lineage, status `validated`, `review_state` `pending_approval` and the snapshot's `bias_disclosures` is created, and no publication is created

#### Scenario: Infeasible run
- **WHEN** the run succeeds with `solution_status` `infeasible`
- **THEN** the loop records outcome `no_version` and no plan version exists for the run

### Requirement: Loop outcome record
Every loop execution SHALL write one audited outcome record. The record holds the session date, the outcome (`no_session`, `skipped_snapshot`, `skipped_no_strategy`, `skipped_budget`, `run_failed`, `run_timeout`, `no_version`, `pending_approval`), the snapshot, run and plan-version identifiers when present, and the strategy read. It MUST be readable through the plan API.

#### Scenario: Outcome readable
- **WHEN** a client reads the loop outcomes for the last 7 sessions
- **THEN** it receives one record per session with the fields above and no storage locations

### Requirement: Automated principals cannot publish
The loop and scheduler roles SHALL be denied the publish and review routes and the FinanceModel approve operation.

#### Scenario: Loop role calls publish
- **WHEN** a policy simulation or deployed call has the loop role call the publish route
- **THEN** it is denied, and no publication exists
