# Proposal

## Why

Research data procurement needs a separately bounded payment capability without relaxing the existing paper-planning roles.

## What Changes

- Introduce an operator-deployed beta-only research-payment service boundary, scoped to an owned AgentCore Payment Manager. Preserve all existing environment/shared boundaries and four-pipeline financial-action prohibitions. Keep setup under USD 5 and project total under USD 100.

## Capabilities

### New Capabilities

- `isolated-research-payment-boundary`: Governed setup and isolation of research-data payments.

### Modified Capabilities

None. This separately authorized change implements the previously deferred payment subsystem; it does not relax current paper-runtime or broker-execution restrictions.

## Impact

New independently rendered bootstrap policy/stack and permission-isolation tests; no change to shared contract package or existing pipeline roles.
