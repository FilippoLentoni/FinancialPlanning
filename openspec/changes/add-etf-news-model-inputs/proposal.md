# Proposal

## Why
ETF portfolio research currently uses prices without an auditable connection to paid news. We need immutable deliveries and point-in-time features before evaluating whether news improves recommendations.

## What Changes
- Store immutable paid-news deliveries and expose point-in-time eligibility and licensed features.
- Preserve the existing cumulative USD 10 purchase cap and funded-wallet stop; do not reset the ledger.
- Quarantine unknown rights, unverifiable dates, stale inputs and invalid deliveries.
- Report matched research comparisons without promising improved performance or promoting policies.

## Capabilities
### New Capabilities
- `research-news-storage`: Store immutable paid-news deliveries and expose point-in-time eligibility and licensed features.
### Modified Capabilities
None.

## Impact
Coordinated additive contract 1.8 release across FinancialPlanning, FinanceModel, FinanceLambdasTool and FinanceAgent. Daily equity and hourly crypto remain compatible. No live brokerage orders or additional cloud training are authorized by this change.
