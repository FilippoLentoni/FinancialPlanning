# Hourly crypto research (beta)

The platform's existing ingestion release now also owns `hourly-crypto-ingest`:
minute 5 of every hour, UTC, including weekends, until 2026-10-21 00:00 UTC.
It fetches BTC-USD and ETH-USD public Coinbase Exchange candles at 3600-second
granularity, with a bounded 168-hour lookback. There is no API credential or
wallet involved in public market-data retrieval. Daily equity ingestion is unchanged.

The scheduled timestamp pins the collection slot. Unfinished, missing, invalid,
conflicting or unaligned candles are rejected; no missing prices are fabricated.
Provider rows and response digests are frozen separately from the normalized
hourly snapshot, using conditional S3 creates and checksum verification. Retry
reuses the original slot and source evidence. Collection checks the shared budget
state before provider calls and retains the existing ingestion retry/DLQ path.

`GET /v1/crypto-research/latest` returns the latest fresh bounded snapshot through
IAM-authenticated API Gateway to beta readers and FinanceModel consumers. Raw S3
locations, secrets and provider credentials are never returned. Daily-equity model
snapshots and their approval requirements are a separate dataset interface.

FinanceModel consumes the snapshot at minute 10 and owns the paper book and model
comparison evidence. See its `docs/hourly-crypto-research.md`. Contract release
1.7.0 adds crypto-specific schemas and four MCP request/response pairs, with
positive/negative synthetic fixtures; all consumers must pin the exact wheel digest.

The first run is a seven-day forward study. The USD 100 simulated capital is not
an AWS charge or an authorization for real investment. The AWS project ceiling
remains USD 100; the controller requires USD 5 of reported project headroom.
No hourly GPU training or automatic paid news/LLM requests are scheduled.
