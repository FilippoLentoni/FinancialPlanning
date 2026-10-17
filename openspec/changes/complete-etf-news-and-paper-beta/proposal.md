# Proposal

## Why
The isolated $100 paper profile needs model access to its declared instrument universe. The authenticated read-only brokerage Gateway also needs explicit canonical resource ownership and a narrowly scoped credential-read exception.

## What Changes
- Grant the model API read-only access to portfolio metadata without adding state-write permission.
- Register Agent-owned beta brokerage Lambda/Gateway resources and release a new exact contract pin.
- Permit only the dedicated beta brokerage Lambda's exact read-only credential read in the live-permission gate; all other live-financial secret access remains rejected.

## Capabilities
### New Capabilities
- `paper-research-profile-access`: Scoped metadata reads for isolated paper research profiles.
### Modified Capabilities
None.

## Impact
Platform portfolio GET authorization, canonical ownership/live-permission checks, contract 1.9.0, downstream exact pins and beta release evidence. No trading or secret-write permission is granted.
