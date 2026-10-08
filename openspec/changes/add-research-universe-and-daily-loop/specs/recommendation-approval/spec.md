# Spec Delta

## Purpose

Defines how model-run recommendations wait for the user's explicit decision. A pending recommendation is published only through an approval-bearing publish call, can be rejected or superseded, and is never auto-published.

## ADDED Requirements

### Requirement: Model-run versions start pending approval
Every plan version that staged-output acceptance creates and validates SHALL carry `review_state` `pending_approval`. Versions created by manual override or Excel import, and model-run versions written under contract 1.0.0, MUST have no `review_state` and keep the 1.0.0 publish rule.

#### Scenario: Accepted daily recommendation
- **WHEN** acceptance validates a staged output
- **THEN** the version reads with `status` `validated` and `review_state` `pending_approval`

#### Scenario: Manual override
- **WHEN** a user creates a manual override child version
- **THEN** it has no `review_state` and can be published under the existing rule

### Requirement: Publication requires explicit user approval
Publishing a version whose `review_state` is `pending_approval` SHALL require an `approval` block with `decision` `approve`, `confirmed_by_user` true and the on-behalf-of caller identity. A request without the block MUST fail with `PRECONDITION_FAILED`. On success the platform MUST set `review_state` to `approved` and create the publication in one transaction.

#### Scenario: Publish without approval block
- **WHEN** a client publishes a `pending_approval` version without an `approval` block
- **THEN** the call fails with `PRECONDITION_FAILED`, details `approval_required`, and no publication exists

#### Scenario: Approved publication
- **WHEN** the tool `plan-writer` role publishes on behalf of a `plan_publisher` user with a valid approval block
- **THEN** a publication referencing the version is created, `review_state` becomes `approved`, and an audit event records the user, the time and the channel

### Requirement: Rejection
The platform SHALL provide a review route that records `decision` `reject`, a reason of 1–500 characters and the on-behalf-of caller for a `pending_approval` version. The route sets `review_state` to `rejected`. A rejected version MUST NOT be publishable.

#### Scenario: Reject then publish
- **WHEN** a user rejects version `pv_R` and later a client tries to publish `pv_R`
- **THEN** the publish fails with `PRECONDITION_FAILED`, details `review_state_rejected`

#### Scenario: Duplicate rejection
- **WHEN** the same rejection is repeated with the same `idempotency_key`
- **THEN** the original result is returned and one audit event exists

### Requirement: Newer recommendation supersedes older pending ones
When a newer daily recommendation for the same plan becomes `pending_approval`, the platform SHALL set every older `pending_approval` model-run version of that plan to `superseded` in the same transaction. Approving or rejecting a superseded version MUST fail with `PRECONDITION_FAILED`.

#### Scenario: Approving yesterday's recommendation
- **WHEN** today's recommendation is accepted and the user then tries to approve yesterday's
- **THEN** the approval fails with details `review_state_superseded` naming today's `plan_version_id`

### Requirement: Pending recommendations are listable
The plan API SHALL list a plan's versions filtered by `review_state`, newest first. Each item includes the parent and the currently published version, so a client can compare the recommendation with the published plan.

#### Scenario: List pending
- **WHEN** a reader lists versions with `review_state=pending_approval`
- **THEN** it receives at most one item per plan with `plan_version_id`, `run_id`, `model_version`, `input_snapshot_id`, `bias_disclosures`, `created_at` and `current_publication_id`

### Requirement: Approval never executes trades
Approving a recommendation SHALL only create a publication. It MUST NOT record an execution, and an execution in mode `live` MUST still fail with `OPERATION_NOT_PERMITTED`.

#### Scenario: Approval side effects
- **WHEN** a recommendation is approved in gamma
- **THEN** the execution table holds no new record for the publication
