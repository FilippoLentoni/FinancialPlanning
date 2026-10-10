# Versioned paper-portfolio lifecycle (beta)

The platform is the authoritative ledger for issued recommendations, human
acceptance/rejection, simulated fills and saved holdings revisions. FinanceModel
captures an issued decision for a saved book; FinanceLambdasTool exposes it through
both MCP Gateways; the hosted agent uses those same tools. Generating a decision
does not update holdings. Only an explicitly confirmed acceptance does.

## Data and API flow

1. The existing 09:00 America/New_York weekday schedule writes approved market
   snapshots. Normal inference reads those stored observations and saved holdings.
2. Model inference creates `POST /v1/portfolios/{portfolio_id}/decisions` with the
   algorithm family, exact numerical recommendation, provenance, market snapshot,
   holdings revision, reference prices, target weights and transaction-cost rate.
3. The user inspects the issued decision and invokes confirmed accept or reject
   through `POST /v1/portfolio-decisions/{decision_id}/resolution`.
4. Acceptance atomically commits the permanent decision status, holdings head,
   immutable history pointer, caller audit and idempotency response. Rejection
   records its resolution without changing holdings.
5. The next recommendation reads the new saved holdings revision. Historical
   decision, holdings and snapshot tools retrieve the exact linked evidence.

Additional read APIs:

| Route | Result |
|---|---|
| `GET /v1/portfolios/{portfolio_id}/decisions` | Issued decisions, newest first |
| `GET /v1/portfolio-decisions/{decision_id}` | Issued evidence and any resolution |
| `GET /v1/portfolios/{portfolio_id}/history` | Holdings revisions, newest first |
| `GET /v1/portfolios/{portfolio_id}/history/{revision}` | One immutable holdings revision |
| `GET /v1/snapshots?dataset_id=…` | Approved snapshot history |
| `POST /v1/activity-events` | Sanitized durable tool/agent receipt |
| `GET /v1/activity-events?portfolio_id=…` | Durable activity for one portfolio |
| `GET /v1/activity-events?session_id=…` | Durable activity for one session |

List APIs accept bounded `page_size` and opaque `next_token`. Activity listing
requires exactly one of portfolio or session. Tokens are scoped to their history
partition. API responses expose trusted identifiers and checksums, never storage
locations or S3 grants.

## Paper execution and concurrency

Acceptance uses fractional shares at the issued snapshot's unadjusted close, with
proportional costs on each bought/sold notional. It solves for post-cost portfolio
wealth before calculating quantities, then verifies that remaining cash is
nonnegative and ending wealth plus costs equals starting wealth. Cash is held in
`cash_balance`; `USD_CASH` is a target allocation, not a share position.

These are historical-reference paper fills, not broker execution or a claim that
an old closing price was tradable when the user accepted. `reference_date` and
`recorded_at` are distinct. History and resolutions use the actual acceptance
timestamp. Decisions for covered historical dates remain available for analysis,
but cannot be accepted unless they use the latest completed session within the
latest approved stored snapshot. A newer approved snapshot or changed holdings
revision requires a new recommendation. This check does not promise that the
provider successfully refreshed the most recent exchange session; callers must
show the actual reference date.

Permanent decision status prevents a second fill even after the short-lived
idempotency cache expires. Conditional holdings updates prevent two competing
recommendations from both advancing the same revision. Reusing an idempotency key
with a different request fails. Repeated acceptance can return the stored original
resolution; it cannot change acceptance provenance or create another revision.

Existing paper books acquire a baseline history record without changing holdings.
Operator edits also retain before/after history and remain distinct from accepted
recommendation fills.

## Persistence, access and retention

Dedicated on-demand DynamoDB tables `portfolio_decision`, `portfolio_history` and
`activity_event` index immutable JSON in the existing encrypted reports bucket:

- `portfolio-decisions/{portfolio_id}/{decision_id}/proposal.json`
- `portfolio-decisions/{portfolio_id}/{decision_id}/resolutions/{resolution_id}.json`
- `portfolio-history/{portfolio_id}/{revision}/{artifact_id}.json`
- `activity-events/{date}/{activity_event_id}.json`

S3 writes are conditional and SHA-256 verified on read. Artifacts are staged before
the DynamoDB transaction; only committed pointers are exposed. A losing concurrent
attempt can leave an unreferenced immutable object, but cannot publish it or reserve
the holdings revision. History and activity tables deny mutation. Decision tables
allow only their proposed-to-accepted/rejected status and resolution-pointer change.

Only the model producer captures issued recommendations. Only the plan-writer
tool role (with delegated user identity), or platform operators, can resolve them.
Reader and model roles cannot accept. Cross-environment callers remain denied.
Activity provenance comes from authenticated transport; nested credentials and
signed download grants are redacted before persistence.

Beta retains linked raw, curated, snapshot, plan, output and report evidence without
automatic current-object expiry. Staging uploads remain short-lived, and noncurrent
S3 versions retain the existing lifecycle. Gamma/prod configuration, lifecycle
routes and tables are unchanged. No training, production strategy activation or
broker execution is triggered by acceptance.
