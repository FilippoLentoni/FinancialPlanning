# Change: permit approved managed workers to inspect isolated beta books

## Why
A managed Jev forward worker must verify current isolated holdings before paid
inference. Its existing role can read snapshots, but portfolio/state API reads
are rejected by platform resource and application authorization.

## What Changes
- Add exactly two existing GET routes for the exact beta managed job role.
- Restrict application reads to original isolated synthetic experiment metadata.
- Preserve writes, ordinary user portfolio denials, and higher environments.
- Factor beta tool route denials without changing their effective permissions so
the API policy retains the existing512byte size reserve.

## Impact
Platform API authorization/IaC and tests only. No table/index/contract changes.
The Model owner supplies the matching beta identity policy. Deployment and actual
approved-worker verification remain centrally controlled.
