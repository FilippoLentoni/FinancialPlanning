# Proposal

## Why

Portfolio recommendations currently require callers to supply holdings on every request. The agent needs one saved, explicitly hypothetical portfolio so a simple portfolio planning question can produce share-level changes using the latest approved market snapshot.

## What Changes

- Add audited, revision-checked paper-state initialization and updates to existing portfolio metadata; readers and inference can read but cannot modify that state.
- Resolve the latest approved snapshot through a bounded API route.
- Publish backward-compatible contracts 1.3 for default saved-portfolio recommendations and share-level decision provenance.
- Provide a beta-only, idempotent operator initializer for the user-approved $10,000 existing paper allocation.

## Capabilities

### New Capabilities

- `saved-paper-portfolio-state`: persistent, audited hypothetical holdings and approved snapshot discovery for recommendation consumers.

### Modified Capabilities

None.

## Impact

FinancialPlanning plan API, portfolio metadata heads, API route policies, shared contracts, and local operator initialization script. No brokerage execution, automatic rebalancing, model training, or recurring jobs.
