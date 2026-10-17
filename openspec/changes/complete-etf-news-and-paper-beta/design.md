# Design
## Context
The portfolio state GET already admits the model API, while portfolio metadata GET admits only tool readers. FinanceModel needs settings to constrain the new separate paper profile. The default live-permission checker rejects brokerage credentials categorically.
## Goals / Non-Goals
Enable metadata reads and a strictly bounded read-only brokerage deployment; no order execution, new principal state writes or general financial-secret exception.
## Decisions
Reuse the existing portfolio GET schema and add only the model API authorization class. Register the Agent-owned brokerage resources in the canonical ownership matrix. Release contract 1.9.0 because ownership and checks ship in the wheel. Match the credential exception to the dedicated beta reader role, secret and read action; do not rely on a broad keyword exemption.
## Risks / Trade-offs
Incorrect profile identity could affect another paper book → retain explicit profile identity and before/after state evidence in the coordinated Model change. Broader IAM could escape a naive secret-name exception → negative tests for role, action, secret and environment mutations.
