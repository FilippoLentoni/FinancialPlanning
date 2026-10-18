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
includes existing encoded entries, Python object sizes, conservative per-token and
per-pool-entry overhead, new range tables and actual compressed page strings. During inference capture the estimate
also includes retained raw token keys. Conversion streams raw tokens through a
1 MiB page writer and retains raw bytes only for needed normalized-string matching.
It validates every dictionary entry, including entries supplied by a pre-compacted
read model, before publication. The legacy nonpaged lexical builder retains its
original 150 MiB token-wire guard. The final transport guards apply after paging,
so an intermediate older representation does not falsely reject a valid new one.

A decoded paged report must retain its ancillary-compaction marker. A mixed page
pool with newly appended legacy gzip chunks is rejected explicitly. Original
archived report bytes are never changed when a new renderer produces a revision.
Capacity tests use separately labeled controlled changing-ID/time/news fixtures;
they do not create financial observations or demonstrate strategy performance.
