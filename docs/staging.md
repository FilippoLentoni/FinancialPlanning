# Staged-output acceptance: worker-facing protocol

This page is for FinanceModel production workers: how to hand run output to the platform, and how the platform decides. It belongs to the OpenSpec change `add-platform-foundation` (spec `staged-output-acceptance`, tasks 7.1 to 7.5, design P7). Code: `platform/finplan_platform/core/staging.py`. Tests: `tests/unit/test_staging.py`.

The manifest schema is the contract `core/v1/staged-output-manifest.json`, with the `finance/v1/staged-output-payload.json` payload. The platform pins `finplan-contracts` **1.0.0**, and the example below is checked against the pinned schema (`test_staging_doc_example_manifest_validates`). The outcome read `GET /v1/staged-outputs/{run_id}` answers with the contract `api/get-staged-output-response` (added in 0.2.0).

## 1. Where to write

- Resolve `/finplan/<env>/financialplanning/config/run-staging-ref` in your own environment. Its value is the staging prefix of the platform's run-output area. Never hard-code a bucket name.
- Write every file under `<run-staging-ref><run_id>/`, using names relative to that prefix, for example `plan-content.json` or `metrics/summary.json`.
- The FinanceModel job role may only **create** objects under `staging/run_*/` (STG-01). It cannot:
  - read, list or delete anything, including its own run;
  - write anywhere else;
  - reach another environment's staging area.
- Staging objects expire after the staging window (`retention.staging_window_days`, 7 days by default). Accepted output is copied out before then.

## 2. Write the manifest last

Write `manifest.json` only after every other file of the run is fully written. It is the **only** completion marker; there is no separate marker file.

The platform never acts on the write itself. Acceptance starts only from an explicit platform call. If that call arrives before the manifest exists, it fails with `PRECONDITION_FAILED` and details `staged_output_incomplete`, and nothing changes (STG-02).

### Manifest fields

| Field | Required | Meaning |
|---|---|---|
| `run_id` | yes | The FinanceModel-minted run ID. It must equal the staging folder name |
| `model_version` | yes | The registry model version |
| `configuration_id` | yes | `cfg_` plus the JCS SHA-256 of the experiment configuration |
| `input_snapshot_id` | yes | The approved platform snapshot the run used |
| `plan_id` | yes | The plan the output is for. It must equal the plan in the accept call |
| `parent_plan_version_id` | no | The version this output revises (`null` for a root version) |
| `completion_status` | yes | `succeeded`, `failed`, `cancelled` or `timed_out` |
| `solution_status` | when `succeeded` | `optimal`, `feasible`, `no_effect`, `infeasible` or `unbounded`. Must be absent otherwise |
| `evaluator_version` | yes | The evaluator that scored the run |
| `contract_version` | yes | Your pinned contract version. Its major must be one the platform serves |
| `files` | yes | Every staged file except the manifest: `name`, `checksum` (`sha256:`), `size_bytes`, optional `content_type` |
| `domain`, `domain_schema_version` | yes | `finance`, `1.0` |
| `payload` | no | The finance payload: `plan_content` (the proposed plan) and `metrics` |
| `written_at` | yes | When the manifest was written (UTC) |
| `synthetic` | phase 1 | `true` for all phase 1 output |

The plan content comes from the listed file `plan-content.json` if present, otherwise from `payload.plan_content`. When both are present they must be identical after RFC 8785 canonicalization.

### Example manifest

This example is synthetic, and every identifier is a placeholder ULID.

<!-- example-manifest -->
```json
{
  "run_id": "run_01KDVDNAZ83BAMMYCEGWF33DPM",
  "model_version": "mv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "configuration_id": "cfg_9eda1821d7a3f8b5c965f4375058e544a3aba022644d6a94d9e446a63100a5f0",
  "input_snapshot_id": "snap_01KDVDNAZ83BAMMYCEGWF33DPM",
  "plan_id": "pl_01KDVDNAZ83BAMMYCEGWF33DPM",
  "parent_plan_version_id": "pv_01KDVDNAZ83BAMMYCEGWF33DPM",
  "completion_status": "succeeded",
  "solution_status": "optimal",
  "evaluator_version": "eval-1.0.0",
  "contract_version": "1.1.0",
  "files": [
    {"name": "plan-content.json", "checksum": "sha256:a1eb0afabfbba3ff3db159497c3ff95028044dffbeead1075cd43e4a9043a890", "size_bytes": 512, "content_type": "application/json"},
    {"name": "metrics/summary.json", "checksum": "sha256:03ecd7ccd61ddffe9481c627ab90c65a1f7171ccbcd3109d1e44116de39a92c2", "size_bytes": 256, "content_type": "application/json"}
  ],
  "domain": "finance",
  "domain_schema_version": "1.0",
  "payload": {
    "plan_content": {
      "base_currency": "USD",
      "allocation": {"weights": [{"instrument_id": "SPY", "weight": 0.6}], "cash_weight": 0.4},
      "constraints": {"long_only": true, "max_weight": 0.8},
      "fees": {"transaction_cost_bps": 5}
    },
    "metrics": {"expected_return": 0.05}
  },
  "written_at": "2026-01-10T09:31:00Z",
  "synthetic": true
}
```
<!-- /example-manifest -->

## 3. Acceptance call

A platform-side caller makes the acceptance call: the scheduled workflow, an operator or the website path. FinanceLambdasTool role classes and FinanceModel roles are denied this route (`FORBIDDEN`).

```
POST /v1/plans/{plan_id}/staged-outputs/{run_id}/accept
{"expected_revision": <plan head revision>, "idempotency_key": "<key>"}
```

## 4. What the platform checks, in order

1. **Already decided.** If the run already has an outcome, the call fails with `CONFLICT` naming that outcome, and `plan_version_id` when the run was accepted. A run produces at most one decision and at most one plan version (STG-06). The exception is an exact retry (same caller, key and body), which returns the original result.
2. **Manifest present** (STG-02). See section 2.
3. **Manifest structure.** The manifest must be strict JSON, valid against the contract schema (including the payload), and its `run_id` and `plan_id` must equal the call's. A `plan_id` mismatch is treated as a request error and is not recorded.
4. **Registry** (STG-03). `run_id` and `model_version` must exist in `/finplan/<env>/financemodel/model/registry-ref`.
   - If FinanceModel has no release in the environment, the call fails with `DEPENDENCY_UNAVAILABLE` (retryable) and nothing is recorded.
   - An unknown `model_version` or `run_id` is rejected, and the rejection names the field.
5. **Outcome** (STG-04). A run with `failed`, `cancelled` or `timed_out` is recorded as `rejected`, with an error envelope (`PRECONDITION_FAILED`, `run_not_succeeded`). No version is created, and the call itself returns normally.
6. **Files and lineage** (STG-03). This step applies to succeeded runs only.
   - Every listed file must exist with its size and SHA-256.
   - No unlisted file may exist.
   - `input_snapshot_id` must be in this environment's snapshot catalog.
   - A named parent must be a version of the plan.
   - The plan content must be present and valid against the plan content schema.

   Any failure is recorded as `rejected` (`rejection_kind: structural`, with the offending files listed) and the call fails with `VALIDATION_FAILED`. No version is created.
7. **No version.** A run with `succeeded` and `infeasible` or `unbounded` is recorded as `no_version` with the solution status. The call returns normally.
8. **Commit.** This applies to `succeeded` with `optimal`, `feasible` or `no_effect`.
   - The manifest and listed files are copied write-once to the platform's accepted-output area.
   - One transaction then creates the plan version: origin `model_run`, lineage taken from the manifest, and the accepted manifest as its `source_artifact` (kind `staged_output_manifest`).
   - The same transaction moves the plan head under `expected_revision` and records the outcome.
   - If the head moved, the call fails with `CONFLICT` and nothing is written. The staged output stays eligible: retry with the new revision.
9. **Validation gate** (STG-05). Before the response, the version is validated with the deterministic plan rules plus the acceptance rule `required_instruments`. That rule requires every instrument of the parent version's allocation to be present in the staged allocation.
   - A partial output ends `invalid`, with a finding that lists `missing_instruments`. It can never become `validated`, and publishing it fails with `PRECONDITION_FAILED`.
   - A complete, reconciling output ends `validated`.

## 5. Reading the outcome

`GET /v1/staged-outputs/{run_id}` is readable by the FinanceModel job-API role and by tool readers. It returns one of three outcomes:

| `outcome` | Also returned |
|---|---|
| `accepted` | `plan_version_id`, the version's current `plan_version_status` (`validated` or `invalid`), `checksum` and lineage |
| `rejected` | `error` (contract error envelope), `rejection_kind` (`run_outcome` or `structural`) and, for structural rejections, `rejected_files` |
| `no_version` | `solution_status` |

No response names a bucket or an object key. Staged files appear only under their manifest-relative names.

## Interface still owed by FinanceModel

The registry lookup itself (does `run_id`/`model_version` exist?) goes through FinanceModel's job API, according to design P7. FinanceModel has not published that interface yet. Until it does:

- the platform resolves `/finplan/<env>/financemodel/model/registry-ref`;
- if the parameter is absent, there is no release, and the call fails with `DEPENDENCY_UNAVAILABLE`;
- if the parameter exists but no lookup client is wired, the call also fails with `DEPENDENCY_UNAVAILABLE`, so the platform never guesses.

Tests inject a registry double.
