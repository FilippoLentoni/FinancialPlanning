# Proposal

## Why

The deployed PPO path returns allocations but cannot explain optimizer trade-offs or reproduce plan-change attribution. Users need an independent traditional-optimization MCP and auditable evidence linking recommendations, observed outcomes and subsequent research.

## What Changes

- Add a separate beta MCP endpoint for minimum-variance, mean-variance and scenario-CVaR recommendations; retain PPO and its selected artifact.
- Persist immutable issued advisory plans with exact market snapshot, holdings, solver/configuration and checksums.
- Compute today's objective-versus-keep counterfactuals and bounded exact Shapley attribution, plus grouped plan-over-plan attribution across different completed decision dates.
- Reconcile the issued plan path against observed paper holdings or supplied executed-account evidence; report missing executions, fees and forecast uncertainty explicitly. Retrieve dated market-event sources as context, without claiming proven causality.
- Expose equivalent versioned skills through the hosted agent and remote MCP tool discovery.
- Persist feedback and sourced literature reviews; enable a beta weekly review with at most one sandbox job and USD 0.50 per week, project/monthly budget checks, idempotency and explicit strategy activation. Supported model/configuration experiments run automatically within limits; unsupported feature/code changes remain recorded proposals.

## Capabilities

### New Capabilities
- `classical-evidence-contracts`: Strict additive contracts for immutable classical plans, attribution, performance and bounded research.

### Modified Capabilities

None: existing changes remain tracked separately; this change supplies their missing producer functionality for the independent classical pathway.

## Impact

Coordinated additive contracts 1.4.0, FinanceModel classical serving and owned research storage/controller, FinanceLambdasTool adapters, and FinanceAgent second Gateway/runtime/skills. Deploy beta through the existing four pipelines. The implementation round has a USD 5 incremental cap within the USD 50 project budget. Gamma/prod, live trading and automatic strategy activation are outside this change.
