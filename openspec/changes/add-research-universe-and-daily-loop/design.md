# Design

## Context

See proposal.md (Why). The platform already has:
- the ingestion operation with a `yfinance` adapter and XNYS calendar (phase 2 is gated by configuration, task 6.18 of `add-platform-foundation`);
- the snapshot approval status;
- staged-output acceptance as an explicit platform-side call (design P7);
- IAM-authenticated plan routes.

Contracts 1.0.0 is published. Beta, gamma and prod share one account in us-east-2. Models and the agent run only in the deployed stack (decision 16), so verification is done against deployed environments, not the dev box. The budget is USD 50 in total: `platform_infra` 8 and `cpu_research` 7.

## Goals / Non-Goals

**Goals:**
- Add a real multi-instrument dataset without disturbing `etf-daily` consumers.
- Run a scheduled loop with no SageMaker cost until the user picks a strategy.
- Make "nothing publishes without the user" a platform-enforced rule, not an agent convention.

**Non-Goals:**
- Choosing or validating strategies; the FinanceModel registry does that.
- Running benchmarks; they are on demand in FinanceModel.
- Website UI for approval.
- Bias-corrected universes, such as point-in-time constituent membership.

## Decisions

### U1. One new dataset identity, full-history snapshots
`equity-etf-daily` is a new identity next to `etf-daily`; SPY stays in `etf-daily`. Each scheduled run fetches the full daily history per ticker since 2010-10-01: five `yfinance` requests at the configured 2 s spacing, about 4,000 rows each. It then writes a full-history snapshot payload of about 1–2 MB.
- **Alternative:** incremental append. It was rejected because Yahoo rewrites historical `adj_close` after each dividend or split, so an append-only series silently mixes adjustment bases.
- Full re-fetch plus `source_revised` diffing keeps every snapshot internally consistent at negligible S3 cost.
- The start date 2010-10-01 is the first full month after VOO's inception.

### U2. Cash is modeled, not ingested
`USD_CASH` (kind `cash`) appears only in the snapshot's `universe` block with `return_assumption: zero_nominal`. A T-bill yield series is a later dataset change. Reports state the assumption (FinanceModel change).

### U3. Universe approval is all-or-nothing
Under `approval-v2-universe`, a snapshot with any instrument incomplete stays `committed`. FinanceModel therefore never optimizes over a silently shrunken universe. The rule version is recorded per snapshot. `etf-daily` keeps `approval-v1`.

### U4. Bias disclosures live in data, not prose
`bias_disclosures` is a contract field on the snapshot with fixed texts. Acceptance copies it into plan-version lineage, so every downstream report, tool response and agent answer can carry it mechanically. The texts are configured per dataset and checked at build time.

### L1. Step Functions Standard for the daily loop
The scheduled ingestion's success event starts a Standard state machine:

`ReadSnapshot → CheckApproved → ReadStrategy (SSM) → CheckBudgetState → SubmitJob → Wait/Poll (5 min, ≤ 9 polls) → Accept → WriteOutcome`.

- Standard workflows are pay-per-transition (about 30 transitions per day, well under USD 0.01 per month) and give visible execution history.
- **Alternatives:** chained Lambdas with EventBridge retries (harder to audit), or a FinanceModel push callback (needs a new FinanceModel → platform write grant). Both were rejected.
- The execution name is `daily-<env>-<session_date>`, which together with the FinanceModel idempotency key makes duplicates harmless.
- A test or operator start may add a `run_tag` suffix to both the execution name and the idempotency key.

### L2. Strategy presence is the only switch
The platform reads only whether `/finplan/<env>/financemodel/config/production-strategy` holds a `strategy_id`. Validation against the strategy registry happens in FinanceModel at selection time and again at job start. Absent, empty or unparseable means `skipped_no_strategy`, the zero-cost default required by decision 22.

### L3. Research plan
A post-deploy step idempotently creates one hypothetical paper portfolio and plan per environment (`synthetic: true`, meaning no real holdings) for the universe. It publishes the plan reference at `/finplan/<env>/financialplanning/config/research-plan-ref`. Real-holdings portfolios stay out of scope.

### R1. Review state beside status
`status` (`pending_validation`/`validated`/`invalid`) is a closed 1.0.0 enum, and changing it would be breaking. `review_state` (`pending_approval`, `approved`, `rejected`, `superseded`) is a new optional field. It is a contract-declared lifecycle status field, so changing it is allowed with an append-only event.
- Approval is carried on the existing publish route, so tools keep one publish path. Rejection uses the new `POST /v1/plan-versions/{id}/review`.
- Both require the contract on-behalf-of caller block. The platform trusts it only from the FinanceLambdasTool `plan-writer` role, which receives it from the Gateway, and from the operator and website roles.

### R2. Supersession in the acceptance transaction
Acceptance of a new pending version and the `superseded` transitions of older pending versions commit in one DynamoDB transaction, together with the head move. At most one pending recommendation per plan therefore exists at any time.

### C1. Contracts 1.1.0 scope
All cross-repo schemas needed by the four companion changes ship in one minor, so consumers pin once. The compatibility gate runs without `--allow-zero-major-breaking`.

## Risks / Trade-offs

- [Yahoo throttling or an outage at 09:00 ET] → backoff, then all-or-nothing approval. The loop skips with `skipped_snapshot`, and an on-demand re-ingestion (budget-checked) can be run later that day.
- [Hindsight and survivorship bias inflate backtest results] → mandatory disclosures in data, reports and agent answers. Bias correction is out of scope and is stated.
- [A test run in beta or gamma costs real SageMaker money] → one `buy_and_hold` job per suite run (about USD 0.02–0.12), behind the same budget pre-check. Prod smoke never submits jobs.
- [The approval block is spoofed by a misconfigured role] → only three principals may carry it; policy-simulation tests cover this, and the scheduler and loop roles have an explicit deny.
- [A pending recommendation moves the plan head] → publication, not head, is authoritative. The head move is unchanged 1.0.0 behavior, and the pending list exposes `current_publication_id`.

## Migration Plan

1. Build and publish contracts 1.1.0 (registry), then pin it in the platform.
2. Deploy the platform release with phase 1 config in all environments. The new dataset is fixture-backed, and the loop is deployed but the strategy key is unset everywhere, so it is a no-op.
3. Set beta `phase: 2` and deploy, then verify scheduled universe snapshots in beta (task group 6).
4. Promote phase 2 to gamma, then to prod after the manual approval stage.

Rollback: redeploy the previous release, or set `phase: 1`. Records written under 1.1.0 remain readable, because 1.0.0 readers ignore the new fields.

## Open Questions

- Should the T-bill cash yield replace `zero_nominal`? This would be a later dataset addition with no spec change here.
