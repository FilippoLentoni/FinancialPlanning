# Modeling data: deployed beta inventory and next schema change

This is the storage design for the next implementation phase. It does not claim that new tables, model families, or schedulers have been deployed. The permission change preserves every deployed table key and record.

## Existing authoritative stores

The live beta inventory contains twelve FinancialPlanning tables plus the FinanceModel job-control table. FinancialPlanning tables use a string `pk`; audit events add `sk`. Application documents are canonical JSON in `doc`, with selected scalar fields copied to top-level attributes for conditions and indexes. DynamoDB accepts additional non-key attributes; adding model input/output fields does not require replacing every table.

| Existing store | Retain its purpose |
| --- | --- |
| portfolio | Saved synthetic book, holdings, cash and optimistic revision |
| portfolio-decision | Issued numerical recommendation, input snapshot, holdings revision, provenance, permanent resolution |
| portfolio-history | Immutable holdings revisions and their decision links |
| snapshot-catalog | Approved market-data snapshot metadata and dataset-index |
| activity-event | Append-only agent/tool receipts indexed by portfolio and session |
| audit-event | Append-only authenticated change evidence |
| plan, plan-version, publication, execution | Versioned plan and execution lifecycle |
| staged-output | Model output acceptance status |
| idempotency | Short-lived retry deduplication, not permanent experiment evidence |
| FinanceModel job-control | Compute run state, leases, approvals, bounded scheduling and result references |
| Raw/curated S3 and model research/registry S3 | Immutable source deliveries, prepared inputs, exact outputs, artifacts and checksums |

The agent recommendation-run table present in FinanceAgent source is absent from the live beta table list. Source code alone is not proof of deployed storage.

## Additional research records

Keep raw articles, extracted source text, complete prompts/responses and large numerical artifacts in S3. DynamoDB records should hold searchable metadata, checksums and references. DynamoDB's item size limit is 400 KB; see [AWS guidance for large attributes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-use-s3-too.html).

Introduce beta research records without changing existing primary keys:

| Proposed table | Keys and query indexes | Required evidence |
| --- | --- | --- |
| research-input | pk = input_id; experiment-index on experiment_id + available_key | Market snapshot/checksum, news manifest references/checksums, published/retrieved/available timestamps, holdings revision, instrument universe, source coverage and quality, configuration version |
| research-run | pk = run_id; experiment-index on experiment_id + started_key; portfolio-index on portfolio_id + started_key | Shared input_id, explicit model family/algorithm, model/provider version, code commit, prompt hash, exact structured output reference/checksum, decision time, status/errors, latency and observed usage/cost |
| portfolio-observation | pk = portfolio_id; sk = valuation_time#observation_id; experiment-index on experiment_id + valuation_key | Holdings revision, quote snapshot/checksum, equity/cash, net return, drawdown, turnover, fees/slippage and observation count |

Use on-demand billing, point-in-time recovery, environment encryption, conditional inserts, and append-only observation history. Maintain permanent uniqueness for a scheduled slot plus experiment plus strategy; TTL deduplication alone cannot prevent later duplicate fills. A started run can have a mutable status head, but committed input/output evidence must remain immutable. Preserve detailed failure records rather than replacing missing evidence with zeros.

Register table ownership, environment tags, IAM policies, and contract changes before deployment. Do not hand-create unmanaged tables. Existing named tables should not have their keys replaced in place; use an additive table and explicit migration if a future key change is required. [CloudFormation documents table replacement constraints](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-dynamodb-table.html).

The current portfolio-decision contract allows only `reinforcement_learning` and `optimization`. A Codex news strategy must receive an honest separate family identifier through a versioned contract update; do not disguise it as a classical optimizer. Update pinned consumers across all four repositories together. Avoid duplicating the portfolio ledger in the agent run log.

## Comparable portfolio experiments

Each strategy receives a separate synthetic portfolio with the same initial capital, universe, starting timestamp and risk constraints. Store one shared immutable input bundle per comparison slot and link every strategy's run to it. Record the model family and cadence independently.

Use FinanceModel's existing common evaluator, point-in-time views and simulation configuration identifiers. Benchmark against the implemented classical optimizers, RL strategies, cash and buy-and-hold controls. Persist evaluator, dataset and cost-model identities with every result; refuse a comparison when they differ.

News is eligible only after its actual availability time. Record source publication time, retrieval time and platform availability separately. Never backdate scraped historical news or interpret missing coverage as neutral sentiment. Store citations and uncertainty with each Codex recommendation.

Separate news collection frequency from decision and fill frequency. A thirty-minute collector does not make daily historical ETF bars tradable every thirty minutes. Use the same supported decision/fill schedule for comparisons, or report the different-cadence experiment separately. Weekend/off-session collection must not create synthetic market fills. Apply the same fees, spreads, slippage and next-bar execution to every family.

Holdings revisions, executed paper fills and valuation observations serve different purposes. A recommendation alone does not change holdings. Preserve the existing explicit acceptance contract for the user-facing book; any later fully automatic paper experiment must use isolated synthetic books and an explicitly specified simulator policy.

## Order of implementation

1. Complete and verify EC2 access to deployed beta storage and APIs.
2. Add ownership registrations, research record contracts, repository methods and table/index definitions; test bounded queries, idempotency, point-in-time joins and concurrency.
3. Deploy additive beta tables and verify one isolated synthetic input/run/valuation round trip.
4. Run the Codex strategy and traditional controls over the same stored inputs through the common evaluator.
5. Enable the requested one-minute snapshot test and thirty-minute collection/review schedules after the data path is complete. Use systemd timers, a run lock, bounded execution time, per-run logs and permanent slot IDs; keep snapshots independent from portfolio fills.
