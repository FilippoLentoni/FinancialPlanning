# Proposal

## Why

Recommendations currently do not become accepted holdings through MCP, and PPO decisions lack durable issued-decision records. Users need to approve or reject a specific recommendation and later reconstruct its inputs, paper execution, holdings history and observed outcome.

## What Changes

- Persist reinforcement-learning and optimization recommendations with market/model/holdings provenance.
- Add confirmed accept/reject, explicitly labeled paper fills, optimistic revision checks and immutable holdings history.
- Expose history, snapshot discovery, stored explanations, dated comparisons and observed performance through both MCP Gateways and the hosted agent.
- Retain sanitized tool and agent activity durably and feed decision/outcome evidence into bounded research review.
- Preserve beta-only wiring and the $50 project budget; no retraining or silent strategy activation.

## Capabilities

### New Capabilities

- `paper-portfolio-lifecycle`: Platform paper acceptance, immutable holdings revisions, snapshot/activity history and retention.

### Modified Capabilities

None. Existing interfaces remain compatible with optional additive provenance fields.

## Impact

Coordinated contract 1.5.0 across all four repositories. New APIs/tools, bounded DynamoDB indexes, immutable S3 artifacts and beta retention changes complete the paper-investment loop. Real broker execution remains separate.
