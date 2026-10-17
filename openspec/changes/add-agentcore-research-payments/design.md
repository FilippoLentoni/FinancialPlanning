# Design

## Context

The user selected AgentCore Payments after a documented capability review. No managers or provider credentials currently exist. All four original pipeline roles retain financial-action denials. AWS documents Coinbase Quick Create, separate session management/execution roles and Base Sepolia testnet operation.

## Goals / Non-Goals

Goals: resumable independent bootstrap, separately bounded roles, deterministic testnet payment adapter, free discovery and honest consent readiness. Non-goals: retail Coinbase accounts, trading, mainnet spending, automatic feature training, production activation or relaxing the four original pipeline boundaries.

## Decisions

Use an operator-owned bootstrap outside the four paper-planning release pipelines, with a FinancialPlanning-owned dedicated boundary and FinanceAgent-owned service, management and execution roles. Keep payment control plane outside model tools. Use a fixed manager name, Coinbase QUICK_CREATE, private state files and no credential output. Payment execution requires a bounded existing session, exact challenge validation and idempotent private receipts. User wallet identity/consent/funding are external prerequisites. Initial native paid path is testnet only; public Bazaar discovery remains free and may list mainnet services, which cannot be paid by this release. Main hosted chat remains a paper-planning interface until a later reviewed connection of the isolated payment subsystem.

The user's instruction to use the selected integration authorizes implementation now; the prior request established proposal followed by apply as the session workflow. This authorization overrides the generic propose skill planning-only pause. USD 100 remains the total ceiling and USD 5 is the incremental setup envelope; no mainnet funds are authorized.

## Risks / Trade-offs

Coinbase subscription and user authorization cannot be supplied by code. Preserve a pending task until those prerequisites and authenticated testnet verification complete. Do not broaden IAM grants to bypass an unavailable service or approval. The first operator workflow uses private durable state on this development host; it is not a distributed production payment ledger. Concurrent execution must fail closed. Signing success is not proof of merchant settlement or useful data delivery.


### Observed AWS create-time identity authorization

A prefix-only identity tagging grant passed IAM simulation but live manager creation
was denied during automatic workload-identity creation. The separately trusted
service role and boundary now allow creation-time identity tagging within the same
account/region only when project, repository and beta request tags match. Its trust
still admits only AgentCore for the owned manager. Manager provisioning then reached
READY; all original boundary documents were left unchanged.
