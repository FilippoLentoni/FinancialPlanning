# Design

## Context

The existing synthetic research portfolio has immutable portfolio identity and a mutable plan revision but no actual held-share ledger. Snapshot lookup already has a dataset index. Contracts 1.2 require caller-supplied state.

## Goals / Non-Goals

**Goals:** Saved hypothetical state, explicit operator changes, read-only inference, approved snapshot discovery, additive contracts and reproducible initialization.

**Non-Goals:** Brokerage integration, real holdings, automatic execution, scheduled experiments or retraining.

## Decisions

Store `paper_state` and independent `paper_state_revision` as mutable attributes on the existing portfolio item; keep its immutable document and plan revision unchanged. Extend the conditional head operation to explicitly permit missing revision zero only for initialization. A single transaction includes state, idempotency and full prior/new audit payloads.

GET/PUT `/v1/portfolios/{portfolio_id}/state` use the shared state schema. GET is allowed to tools and inference; PUT is restricted to operator/platform. Website principals are denied writes too. API resource-policy grants derive from this same route table.

GET `/v1/snapshots/latest?dataset_id=...` precedes the dynamic snapshot route and queries the dataset index newest first, skipping unapproved/expired entries. Return the existing snapshot wrapper.

Contracts 1.3 add a recommendation invocation schema for saved mode while preserving the original explicit request schema unchanged. The MCP catalogue selects the invocation schema. This keeps the strict compatibility gate intact and adds portfolio state/quantity provenance to responses. A beta-only script resolves the saved research plan and approved snapshot, reads close observations, creates fractional quantities from the existing plan weights and $10,000 initial capital, and performs expected-revision-zero initialization.

## Risks / Trade-offs

[Paper holdings are hypothetical] → Store mode/source and synthetic labels; no inference mutation or brokerage claim.

[Concurrent initialization] → Compare-and-set missing revision zero plus an idempotency key scoped to portfolio and initialization inputs.

[Missing or stale prices] → Require valid completed-close prices for every allocation instrument and preserve snapshot/date provenance; no fabricated prices.

## Migration Plan

Deploy platform contracts/API before updating consumers. Initialize beta once through the operator API. Consumer old explicit requests remain valid. Rollback consumers independently; stored additive state remains intact.
