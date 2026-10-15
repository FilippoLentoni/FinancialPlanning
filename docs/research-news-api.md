Paid ETF research metadata (beta)

POST /v1/research-news accepts operator-authenticated immutable deliveries. Source request and
response schemas: api/research-news-import-request and api/research-news-import-response in
contract 1.8.0. Canonical JSON uses sorted keys, compact separators, UTF-8, no NaN/Infinity.
Both payload_checksum and receipt_checksum hash their respective JSON values with sha256: prefix.
Raw payload/payment metadata are frozen in raw/research-news/v1/deliveries; curated manifest
contains server-stamped availability, provider/query/symbols, checksums, quality and rights.
DynamoDB append-only audit records preserve the authenticated operator and eligibility decision.
A successful receipt does not establish rights or quality. Unknown rights or dates remain stored,
quarantined from algorithm use. Research-only rights do not permit PPO training.

GET /v1/research-news/latest?symbols=SPY,VOO&decision_at=<UTC ISO timestamp>
returns at most 168 matching manifests over seven days, scanning at most 336 total objects.
No future availability is returned. Historical coverage is forward_collection_only.
Tool reader and model job API roles can read; they cannot publish purchases.

The dedicated finplan-beta-financialplanning-research-news-publisher role trusts only the
same-account finplan-devdesktop operator. Identity grants allow only these two routes; an
explicit API resource-policy deny blocks all other routes for this role. It has no wallet,
Secrets Manager, S3 or brokerage permissions. Beta raw/curated/audit evidence has no expiry.
Hourly acquisition is run by the operator paid-trial runner under the existing cumulative $10
cap and actual funded USDC balance. This API never signs payments or resets spending limits.
