# Beta EC2 operator access

## Why
The EC2 development role already has administrator identity permissions, but resource-policy deny allowlists prevent it from reading beta storage and APIs. The owner requests autonomous inspection and beta data-model work.

## What Changes
- Configure exact development operator role names in beta only.
- Exempt those names from access-control deny allowlists and admit them in API authorization.
- Preserve append-only history, immutable artifacts, encryption, environment isolation, cost limits, explicit strategy confirmation, and submitter/approver separation.
- Gamma and prod configuration and permissions remain unchanged.
