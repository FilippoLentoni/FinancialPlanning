# Persisted beta modeling reports

`platform/finplan_platform/reporting` is the production report builder. It does not
import `codex-jobs/build-report-demo.py`, invoke any market/news provider, regenerate
features, or substitute shared data for an inference input. The approved demo informs
the explorer/table interactions only. All report figures originate in the bounded
`ModelingEvidence.query_day` DynamoDB read path and checksum-verified immutable S3
artifacts. Reporting cannot make an unavailable model or stale quote succeed.

## Run and inspect

From FinancialPlanning, using the repository environment:

```bash
PYTHONPATH=platform .venv/bin/python scripts/modeling_report.py --experiment EXPERIMENT_ID
PYTHONPATH=platform .venv/bin/python scripts/modeling_report.py --experiment EXPERIMENT_ID --include-experiment EXPERIMENT_ID-cohort8
PYTHONPATH=platform .venv/bin/python scripts/modeling_report.py --experiment EXPERIMENT_ID --day YYYY-MM-DD --finalized
PYTHONPATH=platform .venv/bin/python scripts/modeling_report.py --experiment EXPERIMENT_ID --day YYYY-MM-DD --weekly
```

The EC2 backend is restricted to beta/us-east-2. It resolves the approved existing
reports bucket through SSM and uses the ordinary EC2 credentials server-side. No
credentials, signed AWS URLs, browser AWS clients or external JavaScript CDN are
embedded. A day cannot be marked finalized before its 17:00 America/New_York reporting
cutoff. Daily EOD and seven-day schedule ownership stays with the experiment runner.
`--no-s3-archive` is a diagnostic option; the normal path archives immutable reports
in S3 and local files. The report window is at most seven calendar days; each DynamoDB
query is partitioned by experiment/reporting day, paginated, and bounded to 20,000
records per kind/day. An exceeded bound fails visibly instead of silently truncating.

Default outputs:

- `/home/ec2-user/projects/reports/beta-modeling/latest.html`, `latest.csv`, `latest.json`.
- `/home/ec2-user/projects/reports/beta-modeling/YYYY-MM-DD.html`, matching CSV/JSON.
- `/home/ec2-user/projects/reports/beta-modeling/archive/YYYY-MM-DD/REVISION/` contains
  immutable `report.html`, `report.csv`, `report.json`, `manifest.json` revisions.
- `/home/ec2-user/projects/reports/beta-modeling/weekly/` has current and archived
  seven-calendar-day reports named `START_END.html`, matching CSV/JSON.
- A controlled replay uses a separate `replay/` subtree. It cannot replace any forward
  `latest.html`, even if its test data uses a date that appears in a forward report.

Open/download `latest.html` with the IDE file explorer, or copy it to your laptop
using your existing SSH/SCP connection and open the file in any modern browser. It is
self-contained, including original downloadable input bytes, data, CSS and charts.
JSON/CSV are companion exports. Keep these private: exact input bundles may contain
permitted market/news context. Immutable S3 keys follow
`modeling/v1/EXPERIMENT/MODE/DATE_OR_WINDOW/REVISION/FILE`; the manifest includes source
record IDs and SHA-256/size for every output. Local aliases update atomically; immutable
archive revisions are never overwritten when later data arrives.

## Storage joins and exact input acceptance

The source adapter is `finplan_platform.core.modeling_evidence.ModelingEvidence`:

| Read | Evidence |
| --- | --- |
| `read_registry(experiment)` | Versioned approach identities, book IDs, common comparison policy |
| `query_day(..., kind='runs')` | Permanent approach/slot run head plus verified immutable outcome/execution |
| `query_day(..., kind='observations')` | Immutable valuation envelope plus verified exact observation payload |
| `read_input(input_id)` + `read_input_bytes(input_id)` | Captured inference-boundary metadata, decoded content and ORIGINAL bytes |
| `read_artifact_bytes(graph_sub_boundary_ref)` | Actual graph provider/tool numerical input layers and activity receipts |

Every input must match the selected approach, permanent slot, `input_id`, and the
`run_id` stored in its payload when present. The shared-source kind is refused as an
inference input. SHA-256 is checked against the original bytes; pretty-printed JSON
is only a reading aid and is never used for the input download. HTML contains the
original bytes as base64 so archived downloads survive later source/config changes.
Graph orchestration requests and remote PPO/provider/tool captures are shown as
separate layers; the served policy is not described as consuming a narration prompt.

The prominent explorer pins a run independently of report filters. It displays
actual own-book holdings/cash/revision, supplied constraints/model versions, ordered
numeric values, history, optimizer matrices/scenarios, policy vectors/normalization,
source news content and exact prepared context. It distinguishes metadata-only news,
news not consumed, and invocation not attempted. Large lists/matrices have explicit
preview limits and complete downloads. No missing text is manufactured. A missing,
misbound, regenerated or future-available input remains a visible evidence failure
and blocks ranking for that approach.

The input matrix derives actual consumption from each selected day's captured input,
with native capabilities labeled separately. Rendering escapes untrusted model/news
text; links accept only HTTP(S), and CSP disables network connections. CSV text that
could be interpreted as a spreadsheet formula is prefixed with an apostrophe.
Secret credential fields and signed grants are refused rather than rendered.

## Aggregation and comparison definitions

Distinct run IDs are deduplicated before joins. Conflicting duplicate evidence and
multiple run IDs for one permanent approach/slot fail; input/news fan-out never
multiplies decision, fill or P&L counts. Book identities must be unique per approach.
The comparison policy should persist `first_scheduled_slot` (or
`scheduled_start_at`) and `cadence_seconds: 3600`; this accounts for even an entire
hour with no recorded runs. Without an initialization manifest the report discloses
that coverage cannot be established before the earliest known slot.

Only valid fresh valuations enter realized performance. Stale/carried-forward
observations remain visible, with dashed chart segments, and block equivalent
comparison. Net P&L is current NAV minus opening NAV minus external cash flows;
execution fees are already in NAV and are not subtracted twice. A cash-flow window
requires the common evaluator's persisted time-weighted return. Drawdown is the
maximum decline from the running NAV peak. Turnover and execution costs use the
persisted evaluator output. Unknown execution/model cost stays unavailable, never
an invented zero. Model costs remain separate from simulated transaction costs and
carry an observed/estimated/unavailable basis.

A consolidated report may include up to eight experiments using repeatable
`--include-experiment` flags. Each experiment is independently read and validated
before combining. Display approach IDs are namespaced by experiment, while original
`source_approach_id`, run/input/book IDs, checksums and downloadable payloads remain
unchanged. Five-asset and eight-asset cohorts appear in the same report with distinct
books and cohort labels; they never share a performance rank. Non-valuation
`forecast_match`, execution-disposition and execution-attempt evidence remains
inspectable but does not become an extra equity point or a missing valuation slot.
Durable realization references allow a revised historical decision-day report to
show the original forecast's later matched outcome without rerunning its inputs.

Ranks require identical declared cohort, universe, starting capital, comparison
policy and complete observation timestamps. Missing/unavailable approaches are
unranked; a sole valid book cannot be called a cross-approach winner. The ranking
metric is net realized return, then lower drawdown, then approach ID for deterministic
ties. The result is the observed comparable period's winner, not a universal optimum.
The leaderboard always states its full reporting window even when decision filters
are narrowed. It reports observed dates/valuation counts, failures and coverage.

Forecasts must be actual frozen value forecasts with `forecast_id`, `expected_value`,
`target_at`, portfolio/run identity, baseline, units and model/projection type. An
allocation alone is unavailable forecast coverage. The first valid same-book
observation at/after target (and at/before report cutoff) gives realized value. Signed
error is realized minus expected; absolute error, MAE and RMSE only use matched model
forecasts. A deterministic projection is labeled and excluded from model forecast
accuracy. Future horizons and unpriced matured horizons remain pending. Reporting
is read-only; the durable experiment reconciler owns persisted realization records.

## Verification

```bash
PYTHONPATH=platform .venv/bin/pytest tests/unit/reporting/test_modeling_reports.py
node --check platform/finplan_platform/reporting/report.js
```

Known-outcome controlled replay tests cover exact historical inputs, checksums,
wrong/shared run substitution, no-lookahead, falsely attributed news, duplicate
joins/slots, missing whole hours, unequal windows, stale prices, immutable horizons,
pending-to-realized alignment, cash flows/fees, graph sub-boundary receipts, secret
refusal, HTML escaping and immutable daily/weekly report revisions. Test fixtures
are explicitly controlled replay and never forward research performance.

Browser verification can inspect two real decisions per registered approach and
checksum every original input download:

```bash
PLAYWRIGHT_MODULE=/PATH/TO/playwright-core CHROMIUM_EXECUTABLE=/PATH/TO/chromium \
node scripts/verify_modeling_report.cjs /home/ec2-user/projects/reports/beta-modeling/latest.html /PATH/TO/verification.json
```

The harness records the report cutoff, exact run/input/checksum, actual news layer,
numeric keys, invocation state, pinned selector test and JavaScript errors. Exit 2
means two inspectable decisions per approach are not yet available; it does not
pretend that a missing approach passed. Browser tests against controlled replay
validate UI mechanics only; live scheduled acceptance needs live report input.

## Actual execution-day ledger accounting

The production backend declares `execution_events_supported=True` and atomically
writes one immutable `paper_execution` observation with every ledger commit. Its
DynamoDB day partition is the actual execution day, while the recommendation keeps
its original decision-day partition. The event references the SAME verified ledger
receipt as the run, and permanently binds its run/input/decision/book IDs.

Daily/weekly leaderboard fills, transaction costs and turnover come from these
execution-day events. A Friday recommendation filled Monday therefore contributes
one recommendation Friday and its fills/costs Monday. Querying a window containing
both days never counts the run receipt plus event twice. Recommendation detail still
shows its eventual lifecycle fills/time/cost, clearly distinct from a day's ledger
totals. A missing expected event or corrupt receipt remains a coverage blocker.
Zero execution events means zero committed executions, rather than substituting a
failed model's return. Legacy test readers without this capability explicitly label
issue-day fallback accounting; production uses the actual-day event path.

Late receipts/forecast matches contribute their real observed/recorded timestamp to
the report's data cutoff. A revised historical report therefore cannot display a
Monday fill while claiming that its evidence cutoff is still Friday.

## Interim research and verified session counts

The report CLI includes the primary experiment's `-interim` thirty-minute research
by default (`--include-interim`; `--no-include-interim` is a diagnostic override).
An old interim workflow need not have registered a trading strategy: the read model
queries its actual bounded daily runs, verifies the existing isolated evidence book
with `get_portfolio`, and takes only a descriptive approach reference from the
primary persisted registry. It does not write a registry, create a portfolio, invoke
a model, or construct replacement model inputs. Every original interim run/input
and recommendation remains available in the table, explorer and exports.

Interim rows state `non_trading` and show `reviewed_portfolio_id` from that run's
EXACT input holdings. Their evidence book is distinct from the reviewed primary
book. They are excluded from the primary leaderboard, fills, turnover and forecast
accuracy; an interim prediction is labeled not applied rather than realized against
a book that did not execute its policy. Half-hour recommendations cannot give Codex
extra primary comparison trades.

`observed_dates` counts dates with valid observations, including cash observations
on closed days. `observed_sessions` counts distinct market session dates only when
persisted provider/market-session evidence proves them. Missing proof yields null,
not an inferred weekday or holiday calendar. Explicit stored provider closure with
regular-session timestamps establishes zero observed market sessions, including a
Saturday cash-only book. Coverage of session proof is reported separately.

Cash strategies do not receive quote inputs just to establish market state for a
report. The builder may verify an original shared source and extract ONLY compact
session provenance from its stored `current_quotes`; this is labeled valuation/
session context, remains separate from the exact inference input, and never adds
raw shared prices or news to an approach's consumed payload. Root provider checksums
and the source input ID remain available for reconciliation.

For cohorts with a declared market calendar, ranking uses verified open-session
valuation windows. Expected provider-proven closed-market marks remain visible but
do not create unequal extra cash observations or block an otherwise identical
eligible session window. Missing/stale values during open or unproven periods still
block comparison. A closed-only cash period has a truthful cash return but cannot
be declared a market winner. When the first eligible mark is after a fill, its
immutable execution receipt's `before_value` supplies the opening baseline so the
opening trade fee is included in return; that event ID is displayed in provenance.

A nominal query covering seven dates is not automatically a mature week. Finalized
weekly comparison additionally requires a persisted experiment start on/before the
first reporting date, an actual cutoff at/after the final reporting date, and no
missing expected slots. Newly initialized or unproven windows remain incomplete.

Large exact input artifacts use lossless deterministic gzip transport above 32 KiB.
The JSON export's `artifact_chunks` holds original-byte compressed content keyed by
SHA-256; each input's `raw_chunks` references it. The inline `payload` is an explicitly
bounded preview with `preview_truncations`, never a replacement inference artifact.
The self-contained HTML decodes only the selected original input, verifies its
original checksum, displays its full structured content with labeled table previews,
and downloads the original bytes. Changing selections during decompression cannot
replace a newer selection. Current browsers supporting `DecompressionStream` work
from `file://`; Python export consumers can use `captured_input_bytes(report,input)`.
Small inputs retain the compatible `raw_base64` original-byte representation.

A provider-verified exact closing quote may be a `fresh_close` valuation after the
session ends. Its persisted `market_session.status=completed`, actual session date
and provider/source proof count as an observed session and retain the true close in
comparison windows. `market_open` remains false; the reporting path does not create
fills. Old `carried_closed` quotes remain stale and never become closing marks.
Forecast alignment also requires persisted `value_as_of` at/after the target:
a 17:05 retrieval of an actual 16:00 close cannot realize a 16:30 forecast. Older
observations without this field retain their original `observed_at` alignment rule.

A successful recommendation may later be superseded before any eligible execution.
Its separate verified immutable `execution_disposition_ref`/`execution_disposition`
retains the replacement decision and reason. This is `superseded_not_executed`, not
an accepted ledger receipt: there is no simulated fill, fill timestamp, portfolio
revision/history or `paper_execution` event. Original inference inputs, recommendation,
forecast and model cost remain intact. Trade costs continue to come only from actual
execution-day events; a supersession cannot double-count fills or fees. A late
disposition produces a new report revision and cutoff without backdating evidence.
A manual decision before the declared comparison horizon, or a decision created
when its hourly slot is already closed, can instead have `excluded_nontrading`.
This needs no replacement policy. Its immutable registry evidence and reason remain
inspectable, with no simulated execution. Pre-horizon rows and valuations remain in
the report but cannot create comparison coverage or score forecasts; late-created
slots remain failed coverage. Total recommendations/model cost stay visible alongside
comparison counts/cost. Private report files are always mode0600, including repaired
existing exports; newly created archive directories are0700. Only generated report
paths are changed; the approved demo and unrelated files remain untouched.

Current exports use reversible `modeling-report-gzip/1` transport: bounded outer
gzip, columnar normalized rows, shared lexical/chunk dictionaries, and lazy original
input/source records. The inline input matrix contains explicitly incomplete
consumption/capability summaries. Selecting a decision always decodes its captured
original bytes, verifies their SHA-256, and renders their complete structured content;
no prepared values are reconstructed from summaries. Original JSON number spellings,
escapes, whitespace, field order and repeated ordered values are retained. Source-row
inspection/download is separately labeled canonical report evidence, rather than an
original inference artifact. The source metadata and graph/provider layers remain
available independently. Archived revisions retain their own complete dictionaries.

Decode the matching JSON export with the deployed platform Python environment:

```python
import json
from pathlib import Path
from finplan_platform.reporting.transport import decode_transport
from finplan_platform.reporting.reports import captured_input_bytes
report = decode_transport(json.loads(Path("latest.json").read_bytes()))
run = report["runs"][0]
original = captured_input_bytes(report, report["inputs"][run["run_id"]])
Path(run["input_id"] + ".json").write_bytes(original)
```

The HTML needs a current browser with `DecompressionStream` and SHA-256 support;
no network, CDN, AWS browser login or credentials are used. Its JSON-download button
returns the same encoded export, avoiding eager serialization of all input content.
Only selected artifacts are decoded (at most two cached artifacts/64MiB and8MiB
of lexical bytes). Charts retain every observation as bounded-path SVG geometry;
holdings tables are built only when their approach is expanded. Query joins use
run/book/day indexes rather than matching every run against every observation.
The publication limits are150MiB encoded transport,384MiB decoded transport and
32MiB per original artifact. A size/integrity failure raises an explicit error before
updating aliases and preserves the last immutable revision; it never drops rows,
truncates evidence or treats incomplete reporting as success. News churn and future
model inputs can change size, so measured projection is not a guaranteed maximum.

The daily weekly timer writes a completed window using `--weekly --closed-week`
(end date is the provided New York reporting date minus one calendar day), then the
current provisional window. This allows a seven-closed-calendar-day window to mature
without misrepresenting the current partial day. Both retain immutable revisions;
`latest` follows the final current-window command.

Local acceptance artifacts are under `.worktmp/report-resilience/` (kept separate
from forward reports). The actual manual271-input export was11,578,567bytes before
packing and2,206,400bytes after packing. A same-source-corpus two-slot byte-edit
projection gave143.3MiB wire/233.5MiB decoded transport for168slots; this is an
estimate, excludes real8-asset/crypto/news-churn growth and is not live performance.
The controlled277-book×168-slot cardinality fixture contains46,536 original input
artifacts and valuation/status rows. It encoded to61,886,046bytes (142,526,604decoded)
in97.8seconds with2.11GiB producer peak RSS. Its cold Chromium heap after collection
was152.3MiB (153.2MiB after inspecting first/last-day inputs);4,988DOMnodes,
14.6seconds for cold open plus four verified historical downloads and the deferred
selection-race check. These are packaging/memory tests, not model or scheduling proof.
The fixture, original-byte/namespace/budget integrity tests, and actual manual
browser checks retain their checksums and purpose labels in the scratch evidence.

Rankings distinguish full calendar coverage from the actually observed comparable
period. A common missed first hour or partial first New York calendar day remains
visible in failure/coverage counts and warnings; it does not invent a zero return
or suppress a later, verified shared-window comparison. Within an identical cohort,
universe, initial capital and policy, eligible approaches use the intersection of
actual valid valuation timestamps. Opening/closing observation IDs, cash, all fees,
notional and flows inside that interval are retained; full-period metrics remain
separate. Cropped cash-flow windows require a persisted return for that exact window.
No successful evaluation, lost evidence, missing execution costs, unmatched windows,
or only closed-market cash observations still prevent ranking. Missing/stale chart
points break paths or show explicitly dashed carried marks; no interpolated price
is inserted. A rank describes that observed period, with its disclosed coverage,
and never promotes a policy or claims universal superiority.

Report I/O uses at most32 submitted immutable reads and32 report-owned client
connections. Scoped metadata pagination remains sequential; each run head is still
read consistently from DynamoDB. Input metadata plus its verified original bytes
are fetched once, then identity, checksum, payload and availability checks and
chunk packing run in deterministic query order on the calling thread. Failed
artifact reads retain their rows and evidence errors. Readers injected by tests
retain their existing interface; the optimized adapter applies to ModelingEvidence.

Set `FINPLAN_REPORT_CACHE_DIR` to a dedicated absolute EBS directory, for example
`/home/ec2-user/projects/.worktmp/beta-modeling/report-evidence-cache`, when enabling
the final EC2 release. The optional disposable cache stores only immutable artifact
bytes, with a4GiB stored-byte limit and200,000-entry limit. Every disk hit validates
bounded decompression, original size and full original SHA; the fresh DynamoDB
header/reference and approved prefix/bucket role are still checked. Beneficial
internal gzip reduces disk use without changing an input/artifact's original bytes
or exported encoding. A32MiB/2,048-entry in-memory cache removes repeat receipts
within one aggregation. Mutable run/portfolio heads are never cached.

The dedicated cache directory is700 and its files600, with an ownership/version
marker, atomic byte publication, process/thread locks and indexed bounded eviction.
Unrelated preexisting directories are refused. Cleanup touches only owned indexed
artifact files and exact owned temporary-name patterns. Cache corruption or I/O
failure disables that cache and falls back to verified S3 reads; `report_io` in the
JSON and coverage evidence records the operational failure without inventing a
model failure. To rebuild, stop report services, remove only this dedicated cache,
and resume; existing report archives and DynamoDB/S3 evidence remain authoritative.
A cold rebuild may take longer, so inspect the job deadline and preserve the last
successful alias if the next report cannot finish.

Controlled local scalability measurements using the archived first actual hourly
slot contain277 original model-input artifacts plus full source rows/observations.
Streaming32 controlled identity variants processed497.3MB of original lexical
bytes, without materializing a multi-GB original report. Lexical packing fell from
203.1seconds to103.0seconds, projecting1,066 to541seconds for168 matching-corpus
slots; measured stage peak RSS was221MiB. Cached original token→varint bytes and a
bounded1MiB C-level lexical fast path preserve token order and complete-byte checks.
Larger artifacts continue streaming. Transient fixed64KiB original-byte gzip staging
fell from4.84 to0.285seconds for11.65MB of277 actual inputs; the staging pool grew
from2.81 to3.00MB. Final cross-cohort lexical compression and public CDC APIs are
unchanged. Full report aggregation/joins/export memory also depends on row count;
the prior46,536-row controlled full-producer peak was2.11GiB.

A separate controlled46,536-approach I/O simulation verified all46,536 original
inputs and observations, retained ordering, and bounded submissions to32. It models
94,004 DynamoDB and139,608 immutable-artifact reads. At explicitly assumed12ms
DynamoDB,30ms S3 and0.3ms serialized local-cache operations, modeled cold I/O is
166seconds and warm I/O77seconds. Together with measured packing/staging projections,
a warm seven-day command is plausible within its900second child deadline; two
closed/current commands require the enclosing1,850second service deadline. These
are modeled latency and same-corpus CPU projections, not a live mature weekly run,
not guarantees under different news/scenario volume, and not investment results.
Source growth, throttling, cache eviction and final joins/gzip require headroom;
transport limits and failure-preserving aliases remain enforced. Scratch profiles
are under `.worktmp/report-io/`, with separate before/after CPU, transient staging
and bounded-I/O JSON/log files. The first actual hourly browser proof is preserved
under `.worktmp/beta-modeling/hourly-live-proof/slot01/`; it explicitly does not
satisfy the second required actual hourly slot.

The compressed-cache measurement on277 actual inputs stored11.65MB of original
bytes in2.43MB, with0.311seconds to populate and0.230seconds to read, bounded-
decompress and verify every original SHA (91MiB stage peak RSS). Its measured
0.830ms mean serialized get is more conservative than the0.3ms illustrative I/O
assumption above. Applying it to every modeled artifact read gives151seconds warm
I/O; packing541 + staging48 + warm I/O151 is roughly740seconds before joins,
parsing and final gzip. This is the operational headroom estimate, not an assertion
that a cold mature weekly report has already run or that source churn cannot
exceed the deadline. Cache failures remain visible; integrity/transport failure or
a deadline preserves the prior published revision.
