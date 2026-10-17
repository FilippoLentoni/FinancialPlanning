# Preserve native crypto identity in the isolated paper ledger

## Why
The existing platform crypto API supplies immutable completed hourly BTC/ETH inputs,
not a daily market snapshot ID. Reusing an unrelated daily snapshot or manufacturing
an ID would misrepresent the data consumed by those existing approaches.

## What Changes
- Publish compatible contract 1.13.0 with a bounded native input reference linking
  the exact captured shared input and original platform crypto dataset checksum/times.
- Permit a null snapshot ID only in explicitly automatic isolated synthetic
  experiment proposal/resolution/history records carrying that valid native reference.
- Verify native source schema, provenance, availability, universe and complete hourly
  ordering before the existing atomic portfolio/decision/history transaction.
- Keep the ordinary user decision request, confirmation and daily snapshot rules.

## Impact
No new table, key, index, provider, budget or ledger is introduced. Existing artifacts
remain immutable and readable. New records begin forward-only; no backfill changes
old decisions. All four consumers pin the exact published wheel before beta rollout.
