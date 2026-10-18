# Persisted beta modeling reports

`platform/finplan_platform/reporting` is the production report builder. It does not
import `codex-jobs/build-report-demo.py`, invoke any market/news provider, regenerate
features, or substitute shared data for an inference input. The approved demo informs
the explorer/table interactions only. All report figures originate in the bounded
`ModelingEvidence.query_day` DynamoDB read path and checksum-verified immutable S3
artifacts. Reporting cannot make an unavailable model or stale quote succeed.

## Deployed beta pipeline (beta-competition-v1)

Since 2026-10-18 the beta competition runs in AWS, not on the dev box
(`infra/stacks/modeling.py`, the `Modeling` construct of the existing stack
`finplan-beta-financialplanning-daily-trigger`, so no new pipeline deploy action is needed; beta only via
`config/beta.json` `modeling_competition`). EventBridge Scheduler (America/New_York, Monday to
Friday, flexible window off, one retry, 30 minute maximum event age) passes
`<aws.scheduler.scheduled-time>` as the slot:

| Time (New York) | Target | Steps |
|---|---|---|
| 09:45, 15:45 | state machine `modeling-decision` | `CollectQuotes` (modeling-sources `quotes`) -> `RunModels` (`finplan-beta-financemodel-forward-runner` `{"action":"run"}`) -> `Report` (modeling-report `current`); a runner failure still renders the report, then fails the execution |
| 09:00, 15:00 | state machine `modeling-research` | `CollectResearch` (modeling-sources `research`) -> `PrepareLLMPrompt` (runner `{"action":"prepare_llm_prompt"}`) |
| 17:00 | Lambda `modeling-report` `eod` | finalized day report and the day review |
| Friday 17:25 | Lambda `modeling-report` `weekly` and `closed_week` | rolling and closed seven-day windows |

The reports bucket is write-once except for the `modeling-views/` prefix (bucket policy
`DenyPutWithoutIfNoneMatch` uses `NotResource` for it in beta). Every view is a copy of an
immutable revision, and `head.json` names that revision:

```
modeling/v1/<experiment>/forward/<period>/<revision>/report.html|csv|json|manifest.json   immutable
modeling/v1/<experiment>/review/<day>/<digest>/review.json|md                           immutable
modeling-views/v1/<experiment>/latest/report.html|csv|json, manifest.json, head.json   newest daily
modeling-views/v1/<experiment>/daily/<NY-date>/...                                       per day
modeling-views/v1/<experiment>/weekly/<from>_<to>/... and weekly/latest/...               weekly
modeling-views/v1/<experiment>/review/<NY-date>.md|json                                  day review
```

A view never regresses to an older period, an older data cutoff, or a non-final revision of
a finalized day. To open the latest report (the presigned URL embeds no credentials beyond
its signature and expires):

```bash
BUCKET=$(aws ssm get-parameter --region us-east-2 --name /finplan/beta/financialplanning/config/bucket-reports --query Parameter.Value --output text)
aws s3 presign "s3://$BUCKET/modeling-views/v1/beta-competition-v1/latest/report.html" --region us-east-2 --expires-in 3600
aws s3 cp "s3://$BUCKET/modeling-views/v1/beta-competition-v1/review/YYYY-MM-DD.md" - --region us-east-2
```

Manual runs go through the deployed functions (for example
`aws lambda invoke --function-name finplan-beta-financialplanning-modeling-report --payload '{"mode":"current"}' --cli-binary-format raw-in-base64-out out.json --region us-east-2`);
`{"mode":"preflight"}` / `{"action":"preflight"}` only read.

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
packing and2,206,400bytes after packing. The earlier same-source-corpus two-slot projection was superseded by the changing
actual01/02-based168-hour capacity fixture. Its baseline byte-page encoder was
correctly REJECTED at220,737,498 wire bytes by the unchanged150MiB publication
limit. The source fixture contains46,704 decisions,50,400 distinct ancillary
records and1,008 controlled crypto execution receipts. It changes identities,
timestamps, news contexts and five fixture-day numerical variants; this is a
capacity test, not financial performance or scheduled execution. The subsequent
bounded fragment/one-plain-base implementation and final measured result are
documented in `modeling-report-byte-pages.md`; no earlier projection establishes
weekly capacity.
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
connections. Their credential-provider objects (including refresh), TLS settings,
region, endpoint and signing configuration stay bound to the supplied backend;
scoped callers never fall back to a different default identity. Scoped metadata
pagination remains sequential; each run head is still
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

Managed forward jobs publish `finplan-managed-model-cost/1` events independently
of recommendation reuse. Permanent `managed:<job_id>` identities deduplicate an
embedded recommendation reference and ancillary `managed_model_cost:<job_id>`
observations. Costs belong to `occurred_at` in America/New_York, while the receipt's
`observed_at` records actual platform import availability. Compute/vendor observed
USD, estimates, source evidence and unknown billing remain separate. Elapsed time
multiplied by a rate is an estimate. An expired/unconsumed job still contributes
its actual-day cost event. These observations never become equity marks, fills,
cash debits or portfolio revisions. Comparison/observed-window costs include only
events whose actual occurrence lies in that window; recommendation reuse adds no
second charge. Conflicting permanent events fail integrity validation.

`FINPLAN_REPORT_REFRESH_DIR` enables private durable receipt tasks from the runtime
importer, written after verified platform persistence and before the Model outbox
acknowledgment. A normal daily CLI handles at most128 matching tasks and two prior
cost dates, archives those dated revisions first, and generates its requested
current report last. Strong observation-ID/checksum reads inject missing GSI rows
into only the correct bounded day query; the report must contain the exact event
receipt before its task can be removed. Archive failure, changed/concurrent tasks,
queue overflow and unavailable evidence retain pending work. Tasks contain IDs and
checksums, never financial ledger values. Daily and weekly queues are private,
separately bounded to10,000 files; each read accepts at most128 tasks.

After a dated cost-day archive succeeds, at most seven deterministic existing
weekly alias paths are checked. Affected windows receive versioned durable repair
tasks under `$FINPLAN_REPORT_REFRESH_DIR/weekly/`. The separate hourly trailing
command is the usual consolidated CLI arguments plus `--repair-weekly-from-queue`.
It repairs at most one seven-day window per invocation, performs no ordinary daily
build or model/provider call, and preserves every newer `weekly/latest.*` alias.
Normal17:10 closed/current weekly commands continue producing only their requested
windows. Later revisions retain prior immutable archives. With no tasks, weekly
repair returns without AWS reads. Remove no queued receipt to suppress a failure;
inspect the exact observation/checksum and retry after resolving the cause.

RSS inputs retain their actual supplied symbols and captured coverage. Selected
payloads without attribution provenance show an unverified constituent-attribution
warning; earlier captures used ticker-token matches. Missing selected metadata
does not prove that a newer captured raw source lacked verified attribution. No old input or recommendation
is rewritten. New items show their recorded `symbol_attribution` version/method,
dated universe provenance, matched security content and excluded ambiguous tokens.
Metadata-only GDELT inputs retain their distinct provider semantics and are not
relabeled as RSS or article text. Approved native managed-result reuse counts as
an evaluated signal when its captured invocation state and original evidence are
valid; an uninvoked attempt merely labeled reused still cannot rank.
If a legacy/concurrent producer left more than the queue cap, the reader processes
a bounded prefix and reports a lower-bound pending count; it does not discard
excess tasks or refuse all progress. Weekly task publication serializes its capacity
check with a private lock. Concurrent/new task files survive acknowledgments.

Report publication uses a target-directory `flock` shared by all writers of that
alias directory. Private `.report-heads/<period>.json` and `latest.json` bind the
current aliases to a checksum-verified immutable archive. A durable
`.report-publication.pending.json` records at most two alias updates before the
first export changes. The next writer completes interrupted publication from
that exact archive before considering a new revision; it verifies an existing
newer head before recovering an older intent. Immutable source files and their
parent directories are fsynced before this intent is written. Dated aliases never
regress either their data cutoff or generated timestamp. The latest alias also
never regresses its reporting period; historical repairs use
`update_latest=False`. Existing aliases without heads are migrated once by a
bounded transport decode and direct verification against their computed archive
revision, with no archive directory scan. A mismatched or corrupt legacy export
fails visibly rather than replacing evidence whose freshness cannot be proved.

An external reader opening separate files while a publication is in progress
should use the immutable archive path/manifest for a single revision. After a
crash, invoke the normal report command to recover the small pending publication
before using the mutable aliases. No original captured input or archived report
is rewritten by this recovery.


If a concurrent newer publication prevents a dated late-cost repair alias from
being published, its immutable archive is retained and the task is not
acknowledged. The CLI returns75 (`PublicationDeferred`) so the job wrapper can
retry only this report command after30seconds with a fresh generated timestamp,
within its existing900second deadline. Integrity/unsafe-evidence failures remain
nonretryable exit2. No financial or model invocation is repeated by this retry.

Approved managed-worker attempts have a separate **Managed worker actual input &
outcome evidence** explorer. It shows the original approved binding, exact
per-role/model request messages and numerical preparation, public response or
failure receipts, final policy when supplied, original worker times, and the
origin hourly run/input/slot. Selectors remain independent of the primary hourly
input explorer and portfolio filters. Every original artifact has an exact-byte,
SHA-256-verified download; failed and partial attempts remain inspectable after
the current book or data changes. A before-invocation capture without its original
response is explicitly unconfirmed, and an approved binding is not a model input.

These immutable `managed_inference_evidence:<managed_run_id>` observations use
`finplan-managed-inference-evidence/1`, at most32 JSON artifacts/8MiB original
bytes per event. They add a nontrading CSV/JSON evidence row, never a fill, hourly
comparison opportunity, or second model-cost charge. Model ingestion durably
queues both the original inference receipt and its separate cost receipt before
acknowledging the private outbox. Strong receipt reads cover day-index lag;
report tasks are removed only after the exact receipt appears in a successfully
published dated archive. The same late-day and bounded weekly repair queue
preserves historical worker inputs and costs. Controlled worker fixture tests
are replay evidence, not live GPU/model performance.

If a managed policy is issued before New York midnight and the managed job ends
after midnight, its immutable `finplan-managed-inference-issue-link/1` observation
is indexed at the original `issued_at`. It references the original inference and
cost observation IDs/checksums without copying artifacts or moving costs. The
issue-day archive uses at most two strong receipt reads per link, including when
the day index lags. The terminal-day view labels the same policy as an outcome
update, not a new recommendation. A weekly window contains one permanent worker
attempt/recommendation and one cost event. The third prior-day refresh task must
be durable before Model outbox acknowledgement; report acknowledgement still
requires its exact link receipt in the published dated archive. Controlled tests
exercise NY23:59:58 issue/00:00:05 terminal, both dated archives, original artifact
downloads, weekly deduplication and no portfolio revision/fill changes.

The HTML CSV button exports the same headers and rows as the server CSV,
including managed recommendations/outcome updates and missing-slot statuses.
Full historical normalized row fields are lazily verified on export, one row at
a time; no second full CSV payload is embedded in the report. Formula-like
strings (including leading tab/CR) are escaped while negative numeric values
retain their numeric meaning. JSON-valued cells preserve the same values; JSON
spacing and equivalent numeric formatting can differ. A new renderer revision
binds its current embedded client checksum while retaining the original model
input/output bytes and model/source versions.

All report CLI jobs targeting the same output root take a nonblocking private
`.render.lock` before AWS reads and aggregation. Contention returns75 through
the existing bounded retry path; model, research and snapshot jobs do not take
this lock. This prevents simultaneous hourly, closing and weekly builders from
retaining duplicate large read models. The separate publication lock still
protects immutable revisions and monotonic HTML/CSV/JSON aliases.

The CLI owns its ephemeral read model: it freezes CSV, completes lossless
compaction, then releases the original in-memory row/capture containers before
outer encoding. Library `write_report` callers retain their original mapping by
default. Temporary64KiB input staging uses gzip level1 to reduce local CPU;
final artifact bytes, original SHA checks, final compression and all size limits
remain unchanged. Read-only weekly generation has its own declared1500-second
child deadline (3050-second two-window service); native model/paid-compute
900-second controls remain separate. Capacity acceptance requires measured
whole-job completion and exact downloads, not a small-fixture extrapolation.
