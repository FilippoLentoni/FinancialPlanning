The GDELT Project's [released datasets](https://gdeltproject.org/data.html)
provide a second news-derived research source. Its
[terms](https://gdeltproject.org/about.html) permit unrestricted academic,
commercial and government use with attribution. This adapter retains those
terms and their checksum. It reads released machine metadata and does not fetch
source article text. Attribution: The GDELT Project,
[gdeltproject.org](https://www.gdeltproject.org/).

`scripts/backfill_gdelt.py` captures at most 366 prior UTC days per invocation,
with at most four workers. It freezes each actual daily GKG v1 ZIP, a SHA-256
checksum, HTTP source metadata and versioned organization-count/tone features
in a private operator directory. Requests and decompression have size bounds.
Schema changes, corrupt archives and invalid rows fail that day's completeness
check. The immutable capture report lists every failure. Missing archives are
never converted into zero news counts.

Each feature file uses version `gdelt-gkg-v1-organization-tone-v1`. Exact
organization aliases determine matches; document clusters are weighted by the
source NUMARTS count. Document-wide tone is distinct from a financial headline
sentiment classifier. Sparse exact aliases and ambiguous organization extraction
limit coverage. VOO uses Vanguard organization mentions, which do not represent
the ETF's weighted holdings. An observed zero count sets the per-asset missing
flag; source availability is checked independently.

Historical source availability is explicitly modeled as the later of HTTP
Last-Modified and the next UTC day's 12:00. This conservative daily archive proxy
does not prove an individual article's original publication or first-seen time,
and a late-modified historical object must stay unavailable before that date.
Actual local retrieval and platform availability remain today's timestamps.
Retrospective source-vintage evaluation and forward platform-vintage evaluation
must be reported separately. The archived features cannot silently replace the
paid headline feature group or claim to have been available to this platform
before capture.

`scripts/collect_daily_gdelt.py` uses the same archive and feature code for the
previous UTC day's batch. The operator timer runs at 08:45 America/New_York
every calendar day, including weekends and exchange holidays. It writes a private immutable readiness report and
requires the research host to remain running. It does not train, promote a
policy, or verify that a recommendation consumed the features. A model must
explicitly declare this source and feature version and align morning decisions
with preceding completed prices and the subsequent execution opportunity.

The authoritative beta producer also runs through the existing ingestion Lambda
at 08:45 New York. It stores the ZIP and rights evidence under the private raw
`gdelt-metadata/v1/` prefix and checksummed derivatives under the corresponding
curated prefix, protected by the same write-once storage policy. Actual central
availability is stamped by that producer. The authenticated, read-only
`GET /v1/gdelt-metadata` route returns at most thirty-one calendar source days
per request, preserves the full immutable document and checksum, and identifies
every unavailable day. Its default `platform` mode enforces central availability;
explicit `historical_source_proxy` mode enforces the disclosed archive proxy.
Model and reader roles can read this route without gaining storage write access.
The producer appends a metadata audit event containing both checksums and both
availability boundaries. A retry repairs an interrupted audit commit without
downloading or charging for a replacement source.

For bounded central historical capture, `scripts/backfill_central_gdelt.py`
invokes that same producer rather than uploading authoritative client content.
Each invocation retains the project's budget gate. A locally frozen archive
checksum, when supplied, must match the producer's source, including an already
stored vintage. The operator can choose up to eight concurrent invocations;
errors remain listed and are not automatically retried. Bulk capture and its
elapsed/cost estimate must be measured from an initial actual invocation.


A separate GKG2.1 source captures four same-day premarket archives and serves
`GET /v1/gdelt-intraday-metadata` under feature version
`gdelt-gkg-v2-premarket-organization-tone-v1`. Source windows use New York DST,
07:45 through08:45; the conservative source proxy is the later of object LastModified
and batch timestamp plus15minutes. Daily central capture runs08:45 on every calendar
day. Beta prices run08:50; a distinct Friday17:05 capture preserves completed Friday
prices for weekend reports without submitting another recommendation. Gamma/prod times
remain unchanged. The second price phase uses an independent retry key.

Complete windows can have zero entity matches with null tone and `missing:false`.
Partial windows preserve observed counts as lower bounds and `missing:true`; all absent
windows have unknown null counts/tone. Four attempted source URLs with HTTP status,
retrieval timestamp and checksum record the difference. Missing historical files describe
today's archive audit, not proof of historical feed downtime. No failed source is replaced
with a synthetic news observation. Strict source identity, field bounds and immutable hashes
remain required. The released English GKG corpus is a bounded organization-tone signal,
not exhaustive multilingual or article-level financial sentiment.

For GKG2, `cluster_count` equals the number of unique document identifiers. It does
not represent independently clustered financial events or calibrated asset sentiment.


The central operator backfill CLI defaults to two workers and rejects more than two.
The verified regional Lambda concurrency quota is ten. Nested Agent, Tools, Model and
Platform calls require the remaining headroom; eight simultaneous backfill invocations
caused observed Platform API throttles. Local public-HTTP archive capture can use eight
workers because it consumes no Lambda concurrency. Use `--sessions-only` to prioritize
actual XNYS decision-session dates for intraday model training; every-calendar-day live
capture remains required for weekend reports. Uncaptured weekend archives remain explicit
API coverage gaps until a later full historical capture, and cannot be claimed complete.


Malformed physical TSV lines are quarantined individually without discarding valid
records in the same archive. Original ZIPs and per-archive quarantine manifests
retain each rejected line's number, checksum and reason without copying article text
into logs. `row_count` counts all physical raw lines; `rejected_rows` is the invalid
subset. Four captured archives with rejected lines have partial quality and missing
flags. Counts describe accepted records and remain lower bounds; tone is an observed
subset mean and may be biased, rather than a directional lower bound. Effective model
source coverage is `(captured_batches / 4) * (row_count - rejected_rows) / row_count`;
zero usable rows produce null counts, unavailable quality and zero coverage. Live
recommendations fail closed when no usable source input remains. Existing complete
zero-rejection documents retain their original bytes.
