# Design

## Context

See proposal.md (Why). The following already exist in `add-platform-foundation` and are reused unchanged:
- the scheduled ingestion, the `yfinance` adapter and the XNYS calendar;
- snapshot approval;
- the plan lifecycle and publish routes;
- staged-output acceptance as an explicit platform-side call (P7).

Contracts 1.0.0 is published. All environments share one account in us-east-2. Models run only in the deployed stack (decision 16), so verification uses deployed environments.

## Goals / Non-Goals

**Goals:** add the universe dataset, and add a zero-cost-by-default trigger that leaves every recommendation unpublished.

**Non-Goals:**
- A new approval or review state (approval is the existing publish).
- Experiments or benchmarks (existing, on demand).
- Strategy validation (FinanceModel).
- Bias correction.

## Decisions

### U1. Full-history snapshots per run
Each run fetches the full history since 2010-10-01 for each of the five tickers: five requests at the 2 s spacing, about 4,000 rows each, a payload of about 1–2 MB. Incremental append was rejected because Yahoo rewrites historical `adj_close` after each dividend or split, which would mix adjustment bases. 2010-10-01 is the first full month after VOO's inception.

### U2. Cash is modeled
`USD_CASH` appears only in the `universe` block with `return_assumption: zero_nominal`. A T-bill series would be a later dataset.

### U3. Disclosures are data
`bias_disclosures` is a contract field on the snapshot with fixed configured texts, checked at build time. FinanceModel copies it into staged manifests and reports, so flagging is mechanical.

### T1. Step Functions Standard trigger
The scheduled ingestion's success event starts:

`CheckSnapshot → ReadStrategy → CheckBudget → SubmitJob → Poll (5 min, ≤ 9) → Accept → WriteOutcome`

- It costs about 30 transitions per day (well under USD 0.01 per month) and has auditable history.
- Chained Lambdas and a FinanceModel push callback were rejected as harder to audit or as needing new write grants.
- The execution name and the idempotency key are both `daily-<env>-<session_date>`. A test start may add a `run_tag` suffix to both.

### T2. Strategy presence is the only switch
The platform checks only that the key holds a `strategy_id`. FinanceModel validates it against the registry at selection and at submit time.

### T3. Research plan
A post-deploy step idempotently creates one hypothetical paper portfolio and plan per environment (`synthetic: true`, meaning no real holdings), referenced at `/finplan/<env>/financialplanning/config/research-plan-ref`.

## Risks / Trade-offs

- [A Yahoo outage at 09:00 ET] → backoff, then no approval. The trigger records `skipped_snapshot`, and an on-demand re-ingestion is possible.
- [Hindsight-selected tickers inflate results] → mandatory disclosures carried into every derived artifact.
- [Deployed tests spend money] → at most one `buy_and_hold` daily job per beta or gamma suite (about USD 0.12). Prod smoke never submits.
- [The head moves to an unpublished version] → unchanged 1.0.0 semantics: the publication is authoritative.

## Migration Plan

1. Publish contracts 1.1.0 and pin it.
2. Deploy everywhere with phase 1 config and no strategy key, so the trigger is a no-op.
3. Enable phase 2 in beta, verify, then gamma, then prod after approval.

Rollback: redeploy the previous release or set `phase: 1`. Deleting the strategy key stops jobs immediately.
