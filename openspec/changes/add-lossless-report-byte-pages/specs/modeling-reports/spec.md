## ADDED Requirements

### Requirement: Lossless bounded offline evidence pages
The report transport SHALL retain every original captured input byte and checksum,
including original number spellings, JSON ordering, escapes and whitespace. It SHALL
retain original token-index chunk checksums and independently verify compressed byte
pages before reconstructing any historical input. Its final wire, decoded and
per-artifact limits SHALL remain unchanged.

#### Scenario: Original Unicode crosses a page boundary
- **WHEN** a captured UTF-8 token spans independently compressed byte pages
- **THEN** the report SHALL assemble original bytes before decoding and verify the
  complete original input checksum before offering a download

### Requirement: Compatible bounded evidence access
The report SHALL continue decoding older report formats. Its decoded page cache
SHALL be bounded, and malformed page sizes, offsets, checksums and mixed encodings
SHALL fail explicitly. Ancillary evidence SHALL remain fully inspectable through
its original canonical source-row artifact without multiplying financial rows.

#### Scenario: A later revision exceeds the fixed report budget
- **WHEN** lossless encoding still exceeds a final report limit
- **THEN** publication SHALL fail visibly and preserve the previous immutable report
  and current alias without dropping or truncating source evidence

#### Scenario: Compact metadata still identifies original evidence
- **WHEN** a new revision encodes original hashes as binary bytes, offsets as deltas,
  or normalized rows as columns
- **THEN** its reader SHALL restore the same original hashes, numeric metadata and
  row values, including missing versus null input keys, before evidence selection
- **AND** sparse reconstruction SHALL accept only one verified plain base and
  validate the original target chunk and complete captured-input checksums
