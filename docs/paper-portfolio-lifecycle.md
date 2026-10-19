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

Activity listing reserves `ae1_` plus base64url-encoded JSON containing only
`activity_event_id` as an exact immutable-event lookup cursor. The MCP adapter uses
this cursor to retrieve an original event while serving a large receipt in bounded
chunks. The same portfolio or session filter is mandatory and must match the stored
event. The API verifies the original artifact checksum and returns one complete
event with `next_token: null`. Ordinary list pagination remains unchanged; the
cursor cannot select storage paths or grant access to another history.

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

### Agent tool-call evidence (activity events) in DynamoDB

Every activity event (the agent's `record_agent_activity` receipts, including each
tool's exact arguments and result) is stored in full in DynamoDB as well as in S3.
In beta the table is `finplan-beta-financialplanning-activity-event` (us-east-2,
encrypted with the platform KMS key). It holds two kinds of item:

| Item | `pk` | Attributes |
|---|---|---|
| Index | `act_<ULID>` (time-sortable) | `event_kind`, `recorded_at`, `day_key` (UTC `YYYY-MM-DD`), `session_id`, `correlation_id`, `portfolio_id`, `artifact_key`, `checksum`, `doc` |
| Content | `content#activity-events/<day>/<act_id>.json#<NNNN>` | `record_type=artifact_content`, `part`, `parts`, `size_bytes`, `checksum`, `encoding=gzip(RFC8785 JSON)`, `data` (binary) |

The full sanitized event document (`payload`, `session_id`, `correlation_id`,
`caller`, `recorded_at`, ...) is the gzip of its canonical JSON, split into
350,000-byte `data` slices (`core/content_mirror.py`, shared with
`ModelingEvidence(mirror_content=True)`). The slices are written before the index
item commits, with `attribute_not_exists(pk)`. A retry with identical bytes is a
no-op and different bytes are `IMMUTABLE_RECORD`. The table's resource policy denies
updates and deletes. `checksum` is the SHA-256 of the uncompressed bytes, and the
same bytes are the S3 object `activity-events/<day>/<act_id>.json` in `reports`.
`GET /v1/activity-events` reads the DynamoDB copy first, verifies the checksum, and
falls back to S3 for events recorded before mirroring.

Size limits: an event document may be at most 4 MiB of canonical JSON. API Gateway
REST accepts up to 10 MB, but Lambda's synchronous invocation limit is 6 MB for the
whole proxy event, in which the body is an escaped JSON string next to the headers. A
4 MiB document fits unless about half of its bytes need escaping. A larger request is
refused before the handler runs, so callers should keep receipts at or below 4 MiB.
That is at most about 13 content items, each under DynamoDB's 400 KB item limit. List
pages stop adding events once they hold 4 MiB, and always return at least one event,
so that a response stays under Lambda's 6 MB response limit. The next page resumes
after the last event returned.

Lookups (GSIs project ALL; content items carry no index attributes, so they never
appear in an index):

- **By session:** `session-index` (`session_id`, `activity_event_id`), newest first
  with `--no-scan-index-forward`.
- **By correlation id:** `correlation-index` (`correlation_id`, `activity_event_id`).
  This index was added in a later stack update than the table. CloudFormation adds
  one GSI per table update, so add any future index in its own push.
- **By time:** `activity_event_id` is a ULID, so ids sort by recording time within
  every index. A day or time window outside a session is a filtered scan on
  `day_key` or on a `pk` ULID range. That is acceptable for beta volumes; there is
  no time GSI.

```bash
T=finplan-beta-financialplanning-activity-event; R=us-east-2
# All tool calls of one agent session, newest first
aws dynamodb query --region $R --table-name $T --index-name session-index \
  --key-condition-expression "session_id = :s" --expression-attribute-values '{":s":{"S":"<session id>"}}' \
  --no-scan-index-forward --projection-expression "pk,event_kind,recorded_at,correlation_id,artifact_key,checksum"
# All events of one correlation id (one authenticated API request chain)
aws dynamodb query --region $R --table-name $T --index-name correlation-index \
  --key-condition-expression "correlation_id = :c" --expression-attribute-values '{":c":{"S":"cor_..."}}'
# Everything recorded on one UTC day (index items only)
aws dynamodb scan --region $R --table-name $T --filter-expression "day_key = :d" \
  --expression-attribute-values '{":d":{"S":"2026-10-19"}}' --projection-expression "pk,session_id,event_kind,recorded_at"
# Full document of one event: concatenate parts 0000..parts-1, then gunzip
KEY=activity-events/2026-10-19/act_XXXXXXXXXXXXXXXXXXXXXXXXXX.json
N=$(aws dynamodb get-item --region $R --table-name $T --key "{\"pk\":{\"S\":\"content#$KEY#0000\"}}" --query Item.parts.N --output text)
for i in $(seq 0 $((N-1))); do
  aws dynamodb get-item --region $R --table-name $T --key "{\"pk\":{\"S\":\"content#$KEY#$(printf %04d $i)\"}}" \
    --query Item.data.B --output text | base64 -d
done | gunzip | jq .
```

In Python, `content_mirror.read_content_bytes(client, table, key,
expected_checksum=<index checksum>, allowed_prefixes=("activity-events/",))` returns
the verified bytes.

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
