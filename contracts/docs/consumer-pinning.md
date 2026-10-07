# Consumer pinning and the 0.x beta-only rule

Task 7.4. Spec: contract-schemas, "Immutable, pinned contract releases" and "Semantic versioning
of contracts". Design: D3, D4, and Risks ("Contract churn before 1.0").

## Rules

1. **Pin an exact version and its SHA-256 digest.** Ranges (`^1.2`, `~=1.2`, `>=1.0`) are not
   allowed. The digest belongs to the exact artifact the consumer installs: the wheel for Python,
   the `.tgz` for npm, or the raw schema tarball.
2. **Record the pin in the release manifest.** Every deploy writes
   `/finplan/<env>/<repo>/release/manifest`, with `contract_version` set to the pinned version and
   `contract_digest` set to `sha256:<digest>` of the pinned artifact. A producer also lists the
   contract majors it serves in `served_contract_majors`.
3. **Verify the digest in the build.** If the downloaded artifact's digest differs from the
   pinned digest, the build fails ("Digest mismatch").
4. **Published versions are immutable.** The publish step refuses to overwrite an existing
   version, so a consumer re-pins to a new version and never to new bytes under an old number.
   A rollback never deletes a version: consumers re-pin to an earlier one.
5. **Never vendor or copy schemas.** Validate with the pinned package's validators
   (`copied-id` enforces this, CS-01).
6. **0.x is beta-only.** Versions below 1.0.0 are pre-release. A consumer may pin a 0.x version
   only for **beta**. Gamma and prod deploys require a pinned `contract_version` of 1.0.0 or later.
   Before 1.0.0, schemas may still change between 0.x versions.

## Where the package comes from

The registry is CodeArtifact (design D3). Its reference is
`/finplan/shared/financialplanning/contract/registry-ref`. Build stages authenticate with their
IAM role, so public repositories never need registry credentials. The domain, repository and
endpoint are resolved at build time from that parameter and are never written into a repository
file.

## Example pin

The values below are placeholders. Real digests come from `release-metadata.json` of the contract
build (`finplan-conformance digest build`), which is also recorded in FinancialPlanning's release
metadata.

**Python** (`requirements` file with hashes, installed with hash checking):

```text
finplan-contracts==1.0.0 \
    --hash=sha256:<sha256-of-finplan_contracts-1.0.0-py3-none-any.whl>
```

```sh
pip install --require-hashes -r requirements-contracts.txt
# or with uv:
uv pip install --require-hashes -r requirements-contracts.txt
```

**npm** (`package.json` pins the exact version; the build verifies the SHA-256 of the packed tarball):

```json
{ "dependencies": { "@finplan/contracts": "1.0.0" } }
```

```sh
npm pack @finplan/contracts@1.0.0
finplan-conformance digest verify --sha256 <sha256-of-finplan-contracts-1.0.0.tgz> finplan-contracts-1.0.0.tgz
```

**Build-stage check** (every consumer):

```sh
finplan-conformance conformance --mode consumer --expect-version 1.0.0
```

**Release manifest entry** (written at deploy; validated by `core/v1/release-manifest.json`):

```json
{
  "contract_version": "1.0.0",
  "contract_digest": "sha256:<sha256-of-the-pinned-artifact>",
  "served_contract_majors": [1]
}
```

## Compatibility across repositories

- A minor release adds only optional fields, values in open enums, or new schemas. Consumers
  pinned to an earlier minor keep passing conformance. Breaking changes need a new major, and the
  producer keeps serving the previous major until every consumer in that environment has migrated
  (OWN-07, `compat`).
- Before end-to-end tests or Gateway registration, the end-to-end gate compares each consumer's
  pinned major with the producers' `served_contract_majors` in the same environment:
  `finplan-conformance manifest-gate --env <env> --from-ssm` (read-only), or on files (OWN-08).

## Status of the checks in this procedure

- Pin format, `digest verify`, consumer conformance and the manifest fields are implemented and
  were tested offline.
- **Pending on bootstrap.** The example pin can only be shown to resolve from CodeArtifact in
  **beta** after the contract publish step (task 7.3) and the FinancialPlanning tooling bootstrap
  (task 10.5) have run. That bootstrap was approved in principle on 2026-10-07 (D12) and runs
  only after the bootstrap IaC exists. The beta resolution check (task 7.4 verification) waits on
  it.
- **Enforced by `manifest-gate`.** The rule "0.x is beta-only" is checked by the end-to-end
  gate: a gamma or prod manifest that pins a 0.x `contract_version` fails `manifest-gate` (OWN-08
  gate, `tests/test_manifest_gate.py`). The release-manifest schema itself still accepts 0.x,
  because the same schema serves beta manifests.
