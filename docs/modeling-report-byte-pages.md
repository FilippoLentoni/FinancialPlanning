# Lossless report byte pages

`modeling-report-byte-pages/1` preserves the captured original JSON lexical bytes,
including whitespace, number spelling, ordering and escapes. It retains each
original token-index chunk SHA-256. It groups the original index and lexical byte
streams into independent gzip pages of at most 1 MiB, each with a size and SHA-256.
An offset/length table replaces separately base64-encoded gzip fragments. Repeated
normalized strings may reference their exact original quoted-string token. They are
resolved in increasing byte-offset order, without changing dictionary order.

The self-contained HTML uses native browser gzip decompression. Original downloads
assemble bytes before UTF-8 decoding and verify the complete captured input SHA-256.
A token or Unicode character may cross pages. The decoded page LRU retains at most
8 MiB; each page expansion is bounded to 1 MiB. The existing two-artifact/64 MiB
cache and 8 MiB lexical cache remain separate. No external CDN or network is needed.
Python and JavaScript still decode the previous unpacked and columnar formats.
Ancillary execution attempts/dispositions retain their complete original canonical
source-row artifact; their normalized display rows omit only fields available in
that exact lazy artifact, just as recommendations and valuations already do.

Final limits remain 150 MiB wire, 384 MiB decoded transport and 32 MiB per captured
artifact. Failure preserves the previous report; evidence is never truncated.
The paged builder has a separate 1 GiB incremental-memory estimate, not an EC2
resource allocation or a promise that total process RSS is below 1 GiB. Its estimate
includes newly owned Python object sizes, conservative per-token and per-pool-entry
overhead, new range tables and actual compressed page strings. Conversion references
pre-existing encoded source objects without charging those same objects as new
allocations. Fresh lexical and index-chunk builders share one allocation estimate
that counts newly created encoded bytes, digest keys, cached varints and table
overhead. A bounded lookaside retains small raw tokens. Conversion streams raw
tokens through a 1 MiB page writer and retains raw bytes only for needed
normalized-string matching.
It validates every dictionary entry, including entries supplied by a pre-compacted
read model, before publication. The legacy nonpaged lexical builder retains its
original 150 MiB token-wire guard. The final transport guards apply after paging,
so an intermediate older representation does not falsely reject a valid new one.

A decoded paged report must retain its ancillary-compaction marker. A mixed page
pool with newly appended legacy gzip chunks is rejected explicitly. Original
archived report bytes are never changed when a new renderer produces a revision.
Capacity tests use separately labeled controlled changing-ID/time/news fixtures;
they do not create financial observations or demonstrate strategy performance.

## Changing-corpus measurements

The initial actual01/02-corpus168-hour baseline was rejected at220,737,498 wire
bytes by the unchanged150MiB final guard (111.12seconds,3,853,716KiB total peak
RSS). No report was published. An earlier incorrect conversion estimate also
counted referenced pre-existing source strings as new allocations; its retained
failure is separate from the true final wire rejection. The complete controlled
fixture has46,704 decisions,50,400 ancillary rows and1,008 execution receipts;
554 actual-original oracles and3,098 first/last reconstruction checks are retained.
It spans168 elapsed decision hours across New York boundary dates, not a financial
seven-calendar-day performance window. The known-outcome seven-calendar-day report
proof remains a separate fixture.

Large lexical tokens now reuse fixed4KiB original byte fragments, with complete
original token size/SHA and ordered fragment references. UTF-8 is decoded only
after full reconstruction. Canonical token-index chunks may use a sparse patch
against exactly one PLAIN base from the same approach/layer/chunk position. No
chains or arbitrary graphs are allowed; original index size/SHA is verified after
reconstruction. Noncanonical or unhelpful targets remain plain. The plain-base
index cache is bounded to16MiB independently of the8MiB decoded page cache.

Fresh paged dictionaries retain32-byte SHA-256 token keys and integer varint-cache
keys while preserving original first-appearance index order. An existing digest
must match the original decoded token; a collision fails rather than aliasing
input bytes. A separately charged8MiB lookaside retains only small common raw
tokens. Legacy builders retain their original keys and wire guard. Fresh lexical
and index allocations share the1GiB estimate; actual dictionary table growth is
counted once, along with owned bytes, without charging shared field-name literals
or the same checksum string twice. Pre-compacted capacity fixtures require an
independent fresh-build equivalence check, so conversion alone cannot hide a fresh
producer memory failure. The baseline fresh estimate1,331,640,724bytes exceeded
this budget before the digest-key change; final verification remains pending.

Normalized recommendation, valuation and ancillary tables use column vectors in
new paged reports; legacy row vectors remain readable. Exact input-object presence
masks distinguish a missing key from an explicit null. Sparse base references use
an integer position in the existing original-chunk table, which resolves to the
original checksum before reconstruction; legacy checksum strings remain readable.
These transformations preserve every normalized value as well as every source byte.

The first fragment/delta candidate was still rejected at 178,574,458 wire bytes
(344,221,438 decoded bytes; 307.3 seconds including failure profiling, peak total
RSS 3,740,240 KiB). Its normalized tables were measured individually before adding
column vectors. A separate fresh-build proof rebuilt all 1,076,103 lexical tokens
and 403,361 index records with identical encoded values and original identities:
1,043,533,995 estimated new bytes, 33.06 seconds, 2,512,604 KiB total peak RSS.
This fresh-builder pass alone is not final wire-capacity or whole-CLI proof.

Canonical unsigned varints are checked locally for an overlong terminal zero,
uint32 bounds, truncation and count bounds. A measured redundant re-encoding pass
was removed; randomized controlled tests compare accepted original bytes with the
previous canonical representation. Original index and artifact checksums remain
mandatory. DST tests account for 169 hourly slots in a seven-day New York fall-back
window and 167 in a spring-forward window; no missing slot becomes an invented run.

Original chunk descriptors retain each 256-bit SHA verbatim in independently
verified1MiB gzip pages, with canonical bounded uint32 varints for size, byte offset,
plain/sparse kind, patch size and base-table index. Readers bound the row count to
1,000,000 and each encoded record to53bytes, reject trailing/truncated/overlong data,
and preserve the plain-base rule. Small lexical ranges encode offsets relative to
the preceding small range; fragment offsets remain absolute. All offsets restore
before string selection, and sums are bounded to uint32. Older JSON descriptors
and absolute ranges remain readable. A nonmonotone legacy descriptor table keeps
its old representation. The full controlled projection fits within fixed limits,
but final production and whole169-slot CLI verification remains pending.
