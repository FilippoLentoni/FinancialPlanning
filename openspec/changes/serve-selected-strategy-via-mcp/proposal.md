# Proposal

## Why

On 2026-10-09 the user clarified that the agent must invoke whichever portfolio strategy is selected after offline experimentation through an MCP Lambda target. The existing research pipeline trains policies but does not provide this strategy-neutral on-demand serving path.

## What Changes

- Publish the contracts 1.2 minor for a strategy-neutral recommendation tool with explicit snapshot, completed decision date and current portfolio state.
- Add a source-run strategy selector to the bounded export configuration; outputs carry the frozen algorithm, configuration and artifact provenance.
- Retain the publication/execution read and optional ledger contracts being prepared for the separate explanation workflow.

## Capabilities

### New Capabilities

- `selected-strategy-contract`: this repository's part of reproducible, on-demand selected-strategy recommendations.

### Modified Capabilities

None in the archived spec inventory. This complements the existing in-flight daily and explanation changes; it does not replace performance replay with an allocation-hold approximation.

## Impact

Contract schemas, fixtures, reproducible wheel and exact consumer pins. The platform remains the contract producer; it does not run policy inference. Validation starts offline. Any new AWS work in this round stays below USD 2 and within the user's USD 50 project budget; no fresh training is authorized by an inference request.
