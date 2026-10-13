# Design

## Context

See proposal.md. Current local edits were started before OpenSpec tracking and are uncommitted. The current MCP adapter reaches FinanceModel through a short job API; the user clarified a direct, five-minute Lambda target on 2026-10-09.

## Goals / Non-Goals

**Goals:** one selected-strategy recommendation interface with bounded request latency, exact model semantics and provenance.

**Non-Goals:** training during a recommendation, automatic promotion, broker execution, and changes to full explanation evidence semantics.

## Decisions

Keep finance holdings and recommendation payloads under finance/v1, not core. Use an additive contracts 1.2 minor with deterministic fixtures and reproducible wheel publication. An optional policy_strategy_id selects an evaluated algorithm at export, never at the unprivileged recommendation call.

The selected research policy remains beta advisory/paper while the existing promotion gate is unmet. This is separate from the production-strategy key. The source run and explicit strategy selector freeze the chosen parameters; no fallback to a different algorithm is silent. Unsupported algorithms fail explicitly.

## Risks / Trade-offs

- [Cold starts or data reads dominate inference] → use bounded inputs, nested deadlines and a deployed latency check; no always-on endpoint.
- [Different preprocessing changes the policy] → compare exported deterministic actions with the training library and constrain the frozen universe and feature order.
- [Read requests still incur cost] → no training privileges, fixed timeouts, bounded request size and beta validation under the round's USD 2 cap within the USD 50 project budget.

## Migration Plan

Release producer contracts, model service, adapters, then agent into beta through their existing pipelines. Preserve gamma/prod gates. Roll back to the recorded releases or clear the advisory pointer; source artifacts remain immutable. Existing uncompleted explanation tasks stay uncompleted until their specified deployed tests pass.
