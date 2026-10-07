# Spec Delta

## Purpose

Defines the platform-owned, per-environment object storage for raw inputs, curated observations, immutable snapshots, plan artifacts, run outputs and reports, including encryption, access, immutability, retention and lifecycle.

## ADDED Requirements

### Requirement: Per-environment artifact buckets
The platform SHALL deploy, for each of beta, gamma and prod, six separate buckets with the logical roles `raw`, `curated`, `snapshots`, `plans`, `outputs` and `reports`. Each bucket MUST be tagged with environment, owning repository and logical role. Its name MUST be published only as an SSM parameter under `/finplan/<env>/financialplanning/config/`.

#### Scenario: Buckets per environment
- **WHEN** the platform release is deployed to beta, gamma and prod
- **THEN** 18 distinct buckets exist, each tagged with its environment, `financialplanning` and its logical role, and no bucket is shared across environments

#### Scenario: Bucket name not in source
- **WHEN** the repository's identifier leak scan runs
- **THEN** no bucket name appears in any repository file, and consumers resolve buckets only through the published configuration parameters or trusted artifact references

### Requirement: Encryption at rest and in transit
Every platform bucket SHALL enforce server-side encryption with the environment's platform-owned KMS key and MUST deny requests that do not use TLS. Unencrypted or differently keyed uploads MUST be rejected.

#### Scenario: Upload without TLS
- **WHEN** a principal sends a request to a platform bucket over a non-TLS connection
- **THEN** the bucket policy denies the request

#### Scenario: Upload with another key
- **WHEN** a principal uploads an object specifying a KMS key other than the environment's platform key
- **THEN** the upload is denied

### Requirement: No public access and least-privilege grants
Platform buckets SHALL block all public access and use bucket-owner-enforced ownership. Each bucket MUST grant access only to that environment's named principals: the platform's own roles, a read-only grant on approved snapshot artifacts for the FinanceModel job role, and write-only access to the run-output staging prefix.

#### Scenario: Cross-environment read
- **WHEN** a gamma principal attempts to read an object in a prod platform bucket
- **THEN** access is denied

#### Scenario: Model job reads approved snapshot
- **WHEN** the beta FinanceModel job role reads an object under an approved snapshot in the beta `snapshots` bucket
- **THEN** the read succeeds, and a read of the beta `plans` bucket by the same role is denied

#### Scenario: Model job writes outside staging
- **WHEN** the FinanceModel job role attempts to write to any location other than the run-output staging prefix
- **THEN** the write is denied

### Requirement: Write-once immutable artifacts
The platform SHALL write snapshot, plan-version, accepted-output, report and Excel source-file artifacts once. Each is written under a key derived from its platform-minted identifier or content checksum, using a conditional create that fails if the key already exists. No platform role MUST be able to overwrite or delete these artifacts outside retention expiry.

#### Scenario: Overwrite attempt
- **WHEN** any process attempts to write an artifact to a key that already exists in the `snapshots` or `plans` bucket
- **THEN** the write fails and the stored artifact and its checksum are unchanged

#### Scenario: Delete attempt by application role
- **WHEN** the plan API role attempts to delete an object in the `snapshots` bucket
- **THEN** the request is denied

### Requirement: Object versioning is protection only
Platform buckets SHALL have object versioning enabled for accidental-change protection. Versioning MUST NOT be used as plan-version or snapshot identity, and no API response MUST expose an object version ID.

#### Scenario: Version identity in responses
- **WHEN** a client reads a plan version or snapshot through the platform API
- **THEN** the response identifies it only by platform identifiers, checksum and trusted artifact references, never by an object version ID

### Requirement: Per-environment retention and lifecycle
Each bucket SHALL have lifecycle rules from environment configuration. They MUST expire incomplete multipart uploads and noncurrent object versions, and MUST expire staging objects not committed within the configured staging window. Beta and gamma MUST expire all artifacts after their configured retention. Prod snapshots, plans and reports MUST NOT expire unless a retention value is configured.

#### Scenario: Abandoned staged output
- **WHEN** a staged output under the staging prefix is neither accepted nor rejected within the configured staging window
- **THEN** it is expired by lifecycle and never becomes a plan version

#### Scenario: Beta retention
- **WHEN** a beta snapshot artifact exceeds the beta retention period
- **THEN** it is expired, and its catalog record is marked `expired` so that reads return `NOT_FOUND` with details naming the expiry

#### Scenario: Prod plan artifacts retained
- **WHEN** no prod retention value is configured for `plans`
- **THEN** no lifecycle rule expires current prod plan artifacts

### Requirement: Excel uploads isolated in raw storage
Uploaded workbooks SHALL land in a dedicated upload prefix of the `raw` bucket. They MUST be accepted only through a platform-issued, time-limited, size-limited upload grant, and MUST NOT be readable by any principal other than the platform import role and the environment's operators.

#### Scenario: Upload grant expired
- **WHEN** a client uploads a workbook after its upload grant has expired
- **THEN** the upload is rejected and no import is started
