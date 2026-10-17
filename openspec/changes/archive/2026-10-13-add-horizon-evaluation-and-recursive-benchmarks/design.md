# Design

## Context

The four beta repositories currently share finplan-contracts 1.5.0. Performance evidence is additive within classical-analysis, but request schemas and tool conformance allowlists are strict. See proposal.md for motivation.

## Decisions

1. Retain evaluate_portfolio_decision and add horizon_evaluation evidence. Each producer owns the deterministic calculations; the agent orchestrates and cites them. Do not add a second numerical implementation to the agent.
2. Add run_recursive_improvement with dry_run default true, at most three iterations, immutable ca_ analysis lineage and explicit paid-launch confirmation/idempotency. Existing get/list analysis operations retrieve cycle evidence.
3. Keep the existing shared simulator, portfolio constraints and point-in-time inputs for PPO, classical, Qwen swarm and Jev strategies. Research results remain candidates; activation requires the existing human decision.
4. Reuse run-scoped SageMaker jobs and existing cost/approval/lease controls. No always-on GPU service. Jev uses the secret reference and derived descriptors permitted in the recorded user decisions.
5. Publish an additive 1.6.0 wheel with SHA-256 pin to all repositories. Validate both Python and TypeScript schemas and conformance before deployment. Block Gamma before starting owned pipeline executions; stop these executions before restoring gates.

## Risks / Trade-offs

- Historical policies missing horizon metadata: mark retrospective inference explicitly; do not reconstruct a pretend ex-ante commitment.
- Reused test data and recent LLM pretraining: disclose contamination and prevent research from treating inspected history as an untouched promotion test.
- Budget billing lag: retain an incremental deployment estimate and per-job hard limits in addition to the project alarm.
- Partial or unsupported source data: report unavailable evidence and retain the reason.

## Migration Plan

Deploy compatible platform/contracts first, then Model, tools and Agent beta releases. Verify actual hosted and direct-MCP requests using an isolated paper portfolio. Roll back with prior immutable release artifacts if acceptance fails. Keep gamma/prod, production strategy selection and the user's original holdings unchanged.

## Requirement-to-test mapping

| Requirement | Verification |
|---|---|
| Horizon-aware evidence | Producer point-in-time replay and partial-horizon tests; hosted rendering acceptance |
| Bounded recursive lineage | Contract positive/negative fixtures; producer idempotency/stopping tests |
| Independent benchmark gates | GPU missing approval and unavailable readiness tests; no paid live GPU launch without approval |
| Additive beta deployment | Legacy fixtures, Python/TypeScript conformance; release manifests and isolated live MCP/AgentCore audit |
