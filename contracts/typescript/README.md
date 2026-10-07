# @finplan/contracts (TypeScript)

TypeScript side of the FinancialPlanning contract package. It ships the same JSON
Schemas, domain registry, synthetic fixtures and conformance cases as the Python
distribution `finplan-contracts`, at the same version (`contracts/VERSION`).

## What is in the package

| Entry | Use |
| --- | --- |
| `@finplan/contracts` | Validators (Ajv, JSON Schema 2020-12), RFC 8785 canonicalization, `configurationId`, `requestHash`, `deriveProxiedKey`. No Node built-ins; the schemas are embedded at build time. |
| `@finplan/contracts/node` | Loading a contracts root from disk and the fixture conformance suite (`runConformance`). |
| `@finplan/contracts/data/*` | The raw contract files: `core/`, `finance/`, `domains/`, `fixtures/`, `conformance/`, `ownership/`, `VERSION`. |
| `finplan-conformance-ts` (bin) | `conformance`, `validate`, `configuration-id` and `proxied-key` subcommands. |

```ts
import { validate, configurationId, deriveProxiedKey } from "@finplan/contracts";

const res = validate(request, "tools/get-plan-request");
if (!res.valid) return res.toErrorEnvelope(correlationId); // code: VALIDATION_FAILED, INVALID_IDENTIFIER, ...

const cfg = configurationId(configurationDocument); // "cfg_" + sha256(RFC 8785 JCS)
const key = deriveProxiedKey(callerIdentity, "beta", "create_override_version", idempotencyKey); // "lt_" + sha256
```

`validate` also runs the semantic checks a schema declares in `x-finplan-checks`
(domain payloads, storage-location and leak rules, budget ceiling, retention and so on).
Check context goes in `validate(doc, "budget-allocation", { context: { cost_ceiling_usd: 50 } })`.
As in the Python validator, `format` is an annotation, not an assertion. Use
`createValidator(store, { assertFormats: true })` to assert it.

## Parity with Python

The TypeScript and Python helpers must agree exactly:

- `fixtures/vectors/configuration_id.json` (ID-02) and `fixtures/vectors/proxied_keys.json`
  (ID-10) are reproduced byte for byte (`test/canonical.test.mjs`, `test/keys.test.mjs`);
- every fixture validates or fails with the error code in `conformance/cases.yaml`
  (`test/fixtures.test.mjs`);
- `test/parity.test.mjs` runs the Python validator (from `contracts/python/.venv`,
  offline) over every fixture and thousands of mutations of them. It requires the
  same outcome, error code and `(code, pointer)` issues from both validators.
  It is skipped when the virtualenv is absent.

## Conformance runner

```sh
npx finplan-conformance-ts conformance                       # producer: source checkout
npx finplan-conformance-ts conformance --mode consumer \
    --expect-version 0.2.0 --documents ./my-requests --schema tools/get-plan-request
```

Consumer mode checks the data bundled in the installed package (the version a
consumer pinned). It confirms that the embedded schemas equal those files and can
validate the consumer's own documents. The leak scan, domain-neutrality check and
copied-`$id` detector belong to the Python runner (`finplan-conformance conformance`).
Run both in a build stage.

## Build and test

```sh
npm install
npm run build   # scripts/sync.mjs (version, data/, src/bundle.ts) + tsc
npm test        # build + node --test
npm pack        # runs the build via prepack
```

`src/version.ts`, `src/bundle.ts`, `data/` and `dist/` are generated and git-ignored.
Everything runs offline and needs no AWS credentials.
