# Change: Preserve weekly evidence within bounded offline reports

Repeated hourly evidence and changing input identities can exceed the existing
self-contained report limits despite shared lexical dictionaries. Add lossless
checksummed byte pages and lazy ancillary evidence while preserving original
historical input bytes, full downloads and previous-format readers.

New paged revisions also preserve original hashes in a bounded binary descriptor
table, encode monotone offsets reversibly, and group normalized values by column.
Missing versus null input keys remain distinct. Sparse index patches refer to one
verified plain base only; large lexical fragments retain complete original hashes.
All final size guards and prior-format readers remain in force.
