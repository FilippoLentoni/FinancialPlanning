# Design

## Context

The four beta services use contracts 1.3.0, approved S3 market snapshots, DynamoDB paper holdings and an AgentCore MCP Gateway. PPO serving works; explanation subflows were mostly verified with fixtures and their full numerical producers are absent. See proposal.md for the requested scope.

## Goals / Non-Goals

Goals: independent classical optimization, reproducible counterfactual explanations, an auditable observed-performance path and a genuinely scheduled bounded sandbox review. Non-goals: trading, fabricated account fills, calibrated forecasts not supplied by a model, causal proof from news, automatic activation, arbitrary generated code execution and Gamma/prod rollout.

## Decisions

1. Add contracts 1.4.0 and an independent classical backend Lambda. Serve min-variance, mean-variance and scenario-CVaR with explicit cash/constraint settings. Persist exact numerical inputs and outputs in the Model-owned research store with immutable IDs, checksums and a bounded index. An issued advisory plan is an audit baseline, not a brokerage execution or an automatic publication of an investment order.
2. Today's explanation compares objective at keep versus optimized holdings, instrument keep counterfactuals and exact Shapley contributions of trade changes to objective gain. The coalition game, baseline, units and any infeasible metric-only hybrids are disclosed. Plan-over-plan Shapley switches up to four declared input groups and checks efficiency; dates differ for consecutive daily decisions, while holding period/rebalance semantics must align explicitly. No RL retraining effects are attributed by classical re-solves.
3. Observed performance is anchored to the exact issued plan checksum. Mark stored paper quantities using approved closes; keep execution, fee and forecast fields unavailable when absent. Report reconciliation, exposure/cost/market gaps and thresholds. Sourced market events are contextual hypotheses, with dates and citations; never proof of causal attribution.
4. Add a second Gateway/policy engine with only classical tools and supporting reads. Reuse beta identity and explicitly scope bootstrap role trust to the two beta gateway names. Preserve Gamma/prod role grants. The hosted runtime uses a composite client routing classical names to the new endpoint and PPO names to the original. Full skill instructions/checksums appear in remote tool descriptions.
5. Record user feedback and bounded public literature/news metadata. The Monday 09:00 New York controller reviews evidence and can submit one supported sandbox benchmark per week within USD 0.50 plus monthly/global/category limits. Event/idempotency and active-job guards prevent repeats. Comparisons produce proposals with held-out evaluation evidence; activation remains explicit. Unsupported feature or algorithm code changes are recorded for implementation rather than executed as arbitrary code.
6. Verify mathematical identities with additive/interacting fixtures and meaningful constraint/counterfactual tests. Live beta acceptance uses approved real-provider data and the existing paper book, checks hosted/direct parity, rejects unauthorized paid jobs and records all deployment/test costs. This round is capped at USD 5 within the project USD 50 budget.

## Risks / Trade-offs

- Optimizer sensitivity to estimated means and correlations → freeze estimators/lookbacks and disclose assumptions; benchmark controls.
- Shapley cost or infeasible hybrid solves → fixed group/asset caps, explicit unavailable evidence and no fabricated efficiency values.
- Incorrect causal reading of attribution → label model counterfactuals and sourced events separately.
- No broker ledger → label paper paths and request dated executions before claiming actual execution attribution.
- Repeated model selection overfits validation → record evaluation windows and seed dispersion; require fresh evaluation before activation.
- New gateway bootstrap permissions or service limits → synthesize and inspect the exact beta-only changes before deployment; no ad hoc unrelated role expansion.

## Migration Plan

Publish the single immutable 1.4.0 contract wheel, update all pins, release beta Platform then Model then Tools then Agent through existing pipelines with Gamma gated. Bootstrap beta gateway trust only if needed. Run direct and hosted recommendation, why, plan comparison, performance, context/research/feedback/dry-run and bounded-controller acceptance. Stop owned executions and restore gates. Roll back service manifests/images if acceptance fails; keep immutable evidence records.
