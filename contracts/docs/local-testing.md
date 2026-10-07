# Local, credential-free testing with fixtures

Task 6.6. Spec: contract-schemas, "Shared synthetic fixtures": *Colleague tests a Lambda directly*.

Everything on this page runs **offline**. It needs no AWS credentials, no network access (once
the dependencies are installed) and no market-data provider. It is meant for colleagues who test
a tool Lambda or a producer handler before the Gateway exists, and for every repository's build
stage.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) and Python 3.11 or newer
- Node 20 or newer, for the TypeScript package only

Install the dependencies once, while online. After that, every command also works with
`uv run --offline`.

```sh
cd contracts/python
uv sync
```

## In this repository (producer side)

Run all commands from `contracts/python`:

| Command | What it checks | Verified offline |
|---|---|---|
| `uv run pytest -q` | All unit and contract tests (mocks only, no AWS) | yes |
| `uv run finplan-conformance conformance --mode producer` | Every schema has valid and invalid fixtures, every valid fixture validates, every invalid one fails with the expected code, and the fixture hygiene checks pass (CS-02, CS-09, CS-10) | yes |
| `uv run finplan-conformance validate --schema plan-version ../fixtures/plan-version/valid/*.json` | One schema against chosen documents | yes |
| `uv run finplan-conformance validate --schema execution --envelope ../fixtures/execution/invalid/live-mode.json` | Prints the error envelope (`OPERATION_NOT_PERMITTED`) and exits 1 | yes |
| `uv run finplan-conformance validate --schema budget-allocation --context cost_ceiling_usd=50 ../fixtures/budget-allocation/valid/defaults.json` | Semantic checks that need context | yes |
| `uv run finplan-conformance neutrality` | No finance terms in `core/v1` (DOM-01) | yes |
| `uv run finplan-conformance leak-scan ..` | No account IDs, ARNs, bucket names, endpoints or secrets (OWN-03, ENV-08) | yes |
| `uv run finplan-conformance live-perm-scan ../templates` | No live trading, payment or wallet permissions (ENV-05) | yes |
| `uv run finplan-conformance compat --first-release` | Compatibility gate; later releases pass `--old <previous-release-dir-or-tarball>` (CS-03) | yes |
| `uv run finplan-conformance budget defaults` / `budget check-allocation` | Default allocation and the sum-at-most-ceiling check (ENV-17) | yes |
| `uv run finplan-conformance budget preflight --category cpu_research --estimate 9` | Exhausted category: prints `BUDGET_EXCEEDED` and exits 1 | yes |
| `uv run finplan-conformance ssm-path keys` | Registered and retired SSM keys (ENV-07) | yes |
| `uv run finplan-conformance pipeline-check tests/data/infra/pipelines/valid/standard.json` | Pipeline standard on a fixture template (ENV-09) | yes |
| `uv run finplan-conformance ownership-check tests/fixtures/templates/financialplanning-beta-ok.json` | Ownership check on a fixture template (OWN-01) | yes |
| `uv run finplan-conformance manifest-gate --env gamma tests/data/infra/manifests/gamma-compatible` | End-to-end compatibility gate on fixture manifests (OWN-08) | yes |
| `uv run finplan-conformance digest build --out <dir> --offline` | Reproducible wheel, npm tarball and schema tarball, with digests (CS-04); two builds give identical SHA-256s | yes |
| `cd ../typescript && npm ci --offline && npm test` | TypeScript package build and tests, including Python/TypeScript validator parity (CS-10) | yes |
| `cd ../typescript && node dist/cli.js conformance` | TypeScript producer conformance over the same fixtures (CS-10) | yes |
| `cd ../typescript && node dist/cli.js validate --schema job-status ../fixtures/job-status/valid/running.json` | Validate a document with the TypeScript validator (exit 1 and the issue list when invalid) | yes |

Exit codes: `0` means pass, `1` means a check failed, `2` means a usage or configuration error.
Every check accepts `--json` (see `--help`).

> **Verified.** All commands above were run offline on 2026-10-07 against contracts 0.1.0 and
> again against 0.2.0, and produced the expected result, including the TypeScript build, tests
> and conformance run.

## In a consumer repository

A consumer installs the **pinned** package (see [consumer-pinning.md](consumer-pinning.md)). The
installed wheel bundles the schemas, fixtures and conformance cases, so a consumer never needs a
copy of `contracts/`.

```sh
# consumer conformance: the installed package data against its validators
uv run finplan-conformance conformance --mode consumer --expect-version <pinned-version>

# also validate the consumer's own request documents and look for copied contract schemas (CS-01)
uv run finplan-conformance conformance --mode consumer --expect-version <pinned-version> \
  --documents tests/requests --schema tools/get-plan-version-request --repo .
uv run finplan-conformance copied-id .
uv run finplan-conformance leak-scan .
```

## Invoking a tool Lambda directly with a fixture

The following script needs no credentials and no provider. It loads a request fixture from the
installed package, calls the handler in-process, and validates both the request and the
response against the pinned schemas. A fixture-backed handler returns fixture data. A
model-backed handler with no producer release available must return a valid
`DEPENDENCY_UNAVAILABLE` error envelope (schema `error`).

```python
import json
from pathlib import Path

from finplan_contracts.schemas import contracts_root
from finplan_contracts.validate import validate

FIXTURES = Path(contracts_root()) / "fixtures"


def load_fixture(schema: str, case: str) -> dict:
    return json.loads((FIXTURES / schema / "valid" / f"{case}.json").read_text())


def handler(event, context):
    # Stand-in for your Lambda handler (for example: from my_tool.app import handler).
    return load_fixture("tools/get-plan-version-response", "compact")


request = load_fixture("tools/get-plan-version-request", "by-id")
assert validate(request, "tools/get-plan-version-request").valid

response = handler(request, None)
result = validate(response, "tools/get-plan-version-response")
if not result.valid:
    result = validate(response, "error")  # a producer error must still be a valid envelope
print(result.to_dict())
assert result.valid
```

Rules for direct tests:

- Use only package fixtures (all flagged `synthetic: true`) or documents you build from them.
  Never use real holdings, account exports or retrieved market data.
- Leave the AWS SDK unconfigured, or point it at a stub. A direct test that needs credentials is
  a beta integration test, not a direct test.
- Never pass storage locations (`s3://...` or paths). Inputs are identifiers and trusted artifact
  references only (CS-08).
- Proxied state-changing calls forward the derived key `lt_` + SHA-256 of
  `caller|env|tool|idempotency_key`. Check your implementation against
  `fixtures/vectors/proxied_keys.json`.
