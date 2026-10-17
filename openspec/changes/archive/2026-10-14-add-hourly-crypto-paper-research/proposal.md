# Proposal

## Why

Portfolio-planning research needs a forward-running synthetic USD 100 crypto portfolio and hourly information, with evidence comparing optimization, PPO and LLM approaches. The deployed daily equity policy and the isolated AgentCore Payments prototype do not provide that capability.

## What Changes

- Add a beta-only 24/7 hourly Coinbase public-market dataset, distinct from daily equity ingestion, with completed UTC candles and immutable provenance.
- Run separate synthetic USD 100 portfolios and cash/buy-and-hold controls; record next-bar simulated fills, costs, drawdown, turnover and returns without selecting a universal winner.
- Expose crypto experiment evidence and recommendations to the deployed LangGraph agent through versioned MCP tools.
- Discover supplemental data through Coinbase x402 Bazaar; keep purchases operator-reviewed through AgentCore Payments, with immutable receipts, availability timestamps, quality and license checks before research use.
- Declare stock-trained PPO and unavailable crypto LLM/PPO artifacts ineligible rather than reuse them silently. Additional training and mainnet data purchases require their existing budget and approval paths.
- Keep the project ceiling USD 100, initial additional experiment cap USD 5, live trading disabled, and real investment capital separate.

## Capabilities

### New Capabilities

- `hourly-crypto-market-data`: FinancialPlanning's part of the hourly synthetic crypto research and evidence workflow.

### Modified Capabilities

None. Daily equity paper-book behavior and existing financial-action denials remain intact.

## Impact

Coordinated change in FinancialPlanning (data/contracts/ingestion pipeline), FinanceModel (policy comparisons and scheduled simulation), FinanceLambdasTool (MCP adapters), FinanceAgent (conversation and skills). The existing four release pipelines package these changes with beta-only rollout. The paper distinguishes implemented capabilities from measured investment outcomes. Coinbase trading, withdrawals and automatic strategy promotion are outside this change.
