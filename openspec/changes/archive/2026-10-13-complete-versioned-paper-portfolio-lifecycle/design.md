# Design

## Context

See proposal.md. Four repositories serve same-environment APIs and two MCP Gateways; saved paper holdings already have revision checks, but no MCP acceptance workflow.

## Goals / Non-Goals

Complete the confirmed paper lifecycle and durable evidence joins. Existing production strategy selection and broker execution are separate.

## Decisions

FinancialPlanning owns authoritative proposal, resolution, holdings history and activity records. Immutable S3 payloads use dedicated prefixes with DynamoDB indexes; status heads and saved holdings advance in a single transaction. Proposal and resolution records remain distinct so accepting a decision does not rewrite its issued evidence. Conditional updates and permanent decision status prevent duplicate acceptance after short-lived idempotency records expire.

FinanceModel captures issued PPO and traditional recommendations against the exact stored snapshot and holdings revision. Explicit supplied portfolios without a saved identity remain hypothetical evidence. Both Gateways expose shared lifecycle tools and algorithms remain separate selectable producers. Hosted acceptance presents the exact decision and revision before mutation. Paper fills use stored reference prices, are labeled simulated, and disclose costs; acceptance timestamps are distinct from historical pricing dates.

Tool adapters persist sanitized request/result receipts. Hosted completed turns persist narrative, skill identity and numerical evidence references. History exposes recent revisions and snapshot discovery supports multi-day analysis. Existing weekly bounded review consumes lifecycle outcomes without auto-activating an algorithm. Beta data retention is extended to preserve evidence.

## Risks / Trade-offs

Stale input or holdings revision → reject mutation and regenerate. Missing news or forecast calibration → explicit unavailable fields, no fabricated causes or expectations. S3 and DynamoDB cannot share a transaction → write immutable artifacts first, expose only committed metadata, retain orphan cleanup safeguards. Full sensitive payloads → redact credentials/grants and enforce same-environment APIs.

## Migration Plan

Publish contract 1.5.0, deploy Platform then Model then Tools then Agent to beta through existing pipelines. Preserve Gamma/prod gates and audit their references. Initialize history from current saved book without changing holdings. Verify recommendation, rejection, acceptance, retry, stale revision, next-request state, historical evidence and hosted/direct parity on an isolated synthetic test portfolio. Keep original user paper book intact unless the user specifically accepts its new proposal.
