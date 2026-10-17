# Platform storage

Task 2.6 of OpenSpec change `add-platform-foundation`. Requirements: spec
[platform-storage](../openspec/changes/add-platform-foundation/specs/platform-storage/spec.md),
design P2. Code: `infra/stacks/storage.py`, `infra/stacks/policies.py`,
`platform/finplan_platform/core/artifacts.py`, `platform/finplan_platform/core/sweeps.py`.

> Public repository. This page names no account ID, ARN, bucket name or endpoint. Physical
> names are built at deploy time from the `AWS::AccountId` pseudo parameter and are read by
> consumers only from SSM parameters.

## Bucket roles

One customer-managed KMS key and six buckets per environment (beta, gamma, prod: 18 buckets).

| Role | SSM parameter (`/finplan/<env>/financialplanning/config/…`) | Contents | `logical-role` tag |
|---|---|---|---|
| `raw` | `bucket-raw` | provider responses as received, rejected records, Excel uploads | `raw-input-bucket` |
| `curated` | `bucket-curated` | normalized observations, one object per dedupe key | `curated-input-bucket` |
| `snapshots` | `bucket-snapshots` | snapshot manifests and payloads | `snapshot-artifact-bucket` |
| `plans` | `bucket-plans` | plan-version content, Excel exports | `plan-artifact-bucket` |
| `outputs` | `bucket-outputs`, `run-staging-ref` | run-output staging, accepted outputs | `output-artifact-bucket` |
| `reports` | `bucket-reports` | validation and ingestion reports | `report-artifact-bucket` |

Every bucket also carries the tags `project`, `owner-repo`, `environment` and `bucket-role`.

**Physical names** follow `finplan-<env>-financialplanning-<stem>-<account-id>`, where the stem
is the role, except for `outputs`, whose stem is `run-staging-area`. The contract research
permission boundary (`finplan-<env>-research-permission-boundary`, contracts D5) lets
FinanceModel job roles write only to buckets named
`finplan-<env>-financialplanning-run-staging-area*`. The run-output staging area is the
`staging/` prefix of the outputs bucket, so the outputs bucket carries that stem. The region is
not part of the name (single region, contracts D8); names fit the 63-character limit.

`run-staging-ref` holds the staging prefix URI of the outputs bucket (`s3://<bucket>/staging/`).
A FinanceModel worker writes `<run-staging-ref><run_id>/…` and `manifest.json` last
(see `docs/staging.md`).

## Key layout

| Role | Key layout | Write-once |
|---|---|---|
| raw | `provider/<provider_id>/<dataset>/<retrieved_at>/<ulid>.json` | no |
| raw | `uploads/incoming/<import_id>.xlsx` (presigned POST target, expires after 1 day) | no |
| raw | `uploads/excel/<sha256>.xlsx` (Excel source lineage) | yes |
| curated | `<dataset>/<instrument>/<session_date>/<kind>/<source_ts>.json` | no (dedupe uses conditional creates) |
| snapshots | `<input_snapshot_id>/manifest.json`, `<input_snapshot_id>/payload/*` | yes |
| plans | `<plan_id>/<plan_version_id>/content.json`, `…/export-<template_version>.xlsx` | yes |
| outputs | `staging/<run_id>/…` | no |
| outputs | `accepted/<run_id>/…` | yes |
| reports | `<record_id>/<artifact_id>` | yes |

The `key_*` helpers in `core/artifacts.py` build every key, so all modules share one layout.

## Encryption and access

- **SSE-KMS** with the environment key, S3 Bucket Keys on. Uploads that declare another
  encryption mode or another key are denied (`DenyNonKmsEncryption`, `DenyOtherKmsKey`); an
  upload that declares nothing gets the bucket default, which is the environment key. Writers
  pass the key ARN (`FINPLAN_KMS_KEY_ARN`) or nothing, never `aws:kms` without a key ID (S3 would
  then use the AWS-managed key).
- **TLS only** (`DenyInsecureTransport`), Block Public Access on all four settings,
  BucketOwnerEnforced object ownership (no ACLs).
- **Environment isolation.** `DenyOtherEnvironmentPrincipals` denies every action by a
  `finplan-<other-env>-*` role. `DenyObjectAccessOutsideEnvironment` denies object actions to every
  principal except this environment's platform roles (`finplan-<env>-financialplanning-*`), the
  operator role and the consumers granted on the bucket. The contract environment permission
  boundary adds name- and tag-based denies on the principal side.
- **FinanceModel job role** (name pattern from `config/<env>.json`, `consumer_principals`):
  - `snapshots`: `GetObject` only on objects tagged `snapshot-status=approved` (the catalog
    status mirrored as an object tag); reads of other objects and all writes are denied.
  - `outputs`: `PutObject` only under `staging/run_*/`; no read, list or delete anywhere in the
    bucket, so one run can never read another run's output.
  - every other bucket: no access.
  - Its own identity policy must also allow the S3 actions and `kms:GenerateDataKey`/`kms:Decrypt`
    on the platform key through S3 (FinanceModel owns that policy; the key policy delegates to IAM).
- **Excel uploads** (`raw/uploads/*`) are readable only by the plan-API (import) role and
  operators. Uploads arrive only through a presigned POST: exact key under `uploads/incoming/`,
  `content-length-range` from configuration (5 MiB), 15-minute expiry. The platform refuses a
  commit on an expired grant (`PRECONDITION_FAILED`, `upload_grant_expired`).

## Write-once artifacts

Snapshots, plan versions, accepted outputs, reports and Excel source files are written once
(STO-04):

- `ArtifactStore.put_once` sends `If-None-Match: *` with an S3-verified SHA-256 and records the
  checksum in object metadata; a second write fails with `IMMUTABLE_RECORD` and the stored bytes
  are unchanged. Readers re-hash on every read.
- The bucket policies deny any `PutObject` without the `If-None-Match` condition
  (`DenyPutWithoutIfNoneMatch`) and every delete (`DenyDeleteImmutableArtifacts`) on those
  prefixes. The only exception is the orphan sweeper role
  (`finplan-<env>-financialplanning-metadata-sweeper-role`), which may delete in `plans` and
  `snapshots` only, and only keys without a metadata row older than the 24-hour grace period.
- **Versioning** is on for accidental-change protection only. Object version IDs are never used
  as identity and never appear in an API response (STO-05); clients see trusted artifact
  references (`core/v1/artifact-ref.json`).
- **S3 Object Lock** is not enabled in phase 1 (**PQ-2**, open; decide before non-synthetic
  portfolios are allowed in prod).

## Retention and lifecycle

Values come from `config/<env>.json` (`retention`). Beta preserves linked evidence
for the user-requested longitudinal paper-portfolio lifecycle. Gamma/prod retain
their existing settings.

| | beta | gamma | prod |
|---|---|---|---|
| raw, curated, snapshots, plans, outputs, reports | no expiry (unset) | expire 30 d | no expiry (unset) |
| `outputs/staging/` (uncommitted staged output) | 7 d | 7 d | 7 d |
| `raw/uploads/incoming/` (unclaimed uploads) | 1 d | 1 d | 1 d |
| noncurrent versions | 1 d | 7 d | 30 d |
| incomplete multipart uploads | 1 d | 1 d | 1 d |

There are no storage-class transitions in phase 1 (tiny objects; transitions carry per-request
charges). Prod buckets and the prod key are retained when a stack is deleted; beta and gamma are
can be deleted only once they are empty; automatic current-object expiry no longer
empties beta financial evidence.

Beta's issued decisions, accepted/rejected resolutions, holdings revisions and
sanitized activity use dedicated immutable prefixes in `reports` with dedicated
DynamoDB indexes. See [paper-portfolio lifecycle](paper-portfolio-lifecycle.md).

**Expired snapshots.** The daily sweeper marks a catalog row `expired` (audited conditional
transition, `expired_at` recorded) when the snapshot is past the `snapshots` retention or its
manifest object is gone. Every snapshot read goes through `ensure_snapshot_readable`, which
returns `NOT_FOUND` with details `reason: expired`, `expired_at`, `expiry_reason` and the
retention.

## Open questions

| ID | Question | Interim |
|---|---|---|
| PQ-1 | Beta SSE-S3 instead of a per-environment KMS key (one key's fixed monthly charge) | KMS key in all environments. Decide with current AWS pricing; no price is recorded here |
| PQ-2 | S3 Object Lock (governance) for prod before real data | Deny policies |
| PQ-3 | Retention durations per environment and bucket | P2 defaults above |

## Tests

| Test ID | Where |
|---|---|
| STO-01, STO-02, STO-05, STO-06 (template) | `tests/unit/test_infra_storage_metadata.py` |
| STO-02, STO-03, STO-04, STO-07 (policy simulation) | `tests/unit/test_policy_simulation.py` |
| STO-04, STO-05, STO-07 (writer, grants) | `tests/unit/test_artifacts.py` |
| STO-06 (expired catalog read) | `tests/unit/test_sweeps.py` |
