# Tasks

Test IDs are defined in the mapping table at the end. CI suites never call `yfinance` or SageMaker. **Deployed** tests run against the deployed beta, gamma or prod environment.

## 1. Contracts 1.1.0

- [x] 1.1 Add the 1.1.0 schemas and fields from `recommendation-contracts`, with conformance fixtures and ownership rows. Verify CON-01 (conformance and ownership check) and CON-02 (compatibility gate against 1.0.0 without the 0.x exemption; 1.0.0 upgrade fixtures keep their checksums).
- [ ] 1.2 Publish 1.1.0 through the pipeline and pin it in the platform. Verify the pin check in beta and gamma, and that the consumer promotion gate rejects a 1.1.0 consumer where 1.0.0 is served (CON-02).

## 2. Universe dataset

- [x] 2.1 Register `equity-etf-daily` (instruments with kinds, cash assumption, start date, disclosures) in the dataset registry and config schema. Verify UNI-01 unit tests (both datasets configured; a missing kind or disclosure fails the build), with the existing `etf-daily` tests unchanged.
- [x] 2.2 Implement the full-history multi-ticker fetch, normalization and `source_revised` diffing. Verify UNI-02 unit tests on synthetic mocked responses (split continuity, rewritten history flagged, previous checksum unchanged).
- [x] 2.3 Implement `approval-v2-universe` and `bias_disclosures` on snapshots. Verify UNI-03 (one missing ticker stays `committed`; the policy simulation denies a FinanceModel read) and UNI-04 contract tests.
- [x] 2.4 Add synthetic universe fixtures for phase 1 and CI. Verify that the data-hygiene check passes.

## 3. Daily recommendation trigger

- [x] 3.1 Implement the state machine (T1) with the snapshot, strategy and budget checks and the outcome record. Verify DLY-01 to DLY-03 unit tests with stubs (`skipped_snapshot`; `skipped_no_strategy` makes zero job-API calls; `skipped_budget`).
- [x] 3.2 Implement submission (idempotent key), polling with a 45 min bound, and the call to the existing acceptance. Verify DLY-04 (a duplicate start gives one `run_id`) and DLY-05 (unpublished `model_run` version with disclosures) unit tests.
- [x] 3.3 Add the grants (FinanceModel `submit_job`/`get_job_status`, strategy-key read), deny publish to the trigger and scheduler roles, and add the `research-plan-ref` post-deploy step. Verify DLY-06 policy simulation and a re-run of the idempotent post-deploy step.
- [x] 3.4 Document the trigger outcomes and per-run cost in `docs/daily-trigger.md`. Verify that the outcome names match the code's enum.

## 4. Deployed verification

- [ ] 4.1 **DLY-07 (beta and gamma, deployed, phase 1):** start the state machine with the strategy key absent, then read the outcome record through the deployed API. Verify `skipped_no_strategy`, no request in the FinanceModel job API log, and an unchanged version list.
- [ ] 4.2 **UNI-05 (beta, deployed):** set beta `phase: 2` and deploy. After the next 09:00 ET scheduled run, read the latest universe snapshot through the deployed API. Verify:
  - `yfinance` lineage;
  - 5 tickers from 2010-10-01;
  - `approved` under `approval-v2-universe`;
  - both disclosures;
  - the SPY `etf-daily` snapshot is still produced.
- [ ] 4.3 **DLY-08 (beta, deployed, real job):**
  1. Select `buy_and_hold` through the FinanceModel selection operation.
  2. Start the trigger with `run_tag=it-<build>`.
  3. Verify one FinanceModel run with cost tags, a `validated` origin-`model_run` version with disclosures, and no new publication.
  4. Publish it through the existing publish route as the operator (user approval path) and verify the publication.
  5. Clear the key, restart the trigger, and verify `skipped_no_strategy`.
- [ ] 4.4 Promote phase 2 to gamma (UNI-06: the gate refuses without beta evidence, unit-tested), then repeat UNI-05 and DLY-08 in gamma.
- [ ] 4.5 After manual approval, enable prod phase 2. Run the **prod smoke (deployed, non-mutating)**: the latest scheduled universe snapshot is approved with disclosures, and the latest trigger outcome is `skipped_no_strategy` or `pending_approval`. No job is submitted.

## Requirement-to-test mapping

| Capability | Requirement | Test ID | Type |
|---|---|---|---|
| research-universe-dataset | Universe dataset identity | UNI-01, UNI-05 | unit + **beta/gamma deployed** |
| research-universe-dataset | Adjusted daily history from 2010-10-01 | UNI-02, UNI-05 | unit + **beta/gamma deployed** |
| research-universe-dataset | All-or-nothing universe approval | UNI-03, UNI-05 | unit + policy sim + **deployed** |
| research-universe-dataset | Hindsight and survivorship disclosures | UNI-01, UNI-04, UNI-05 | unit + contract + **deployed** |
| research-universe-dataset | Beta-first provider enablement | UNI-06, UNI-05 | unit (gate) + **beta/gamma/prod deployed** |
| daily-recommendation-trigger | Triggered only by an approved scheduled universe snapshot | DLY-01 | unit |
| daily-recommendation-trigger | No production strategy means nothing runs | DLY-02, DLY-07, DLY-08 | unit + **beta/gamma deployed** |
| daily-recommendation-trigger | Budget pre-check | DLY-03 | unit |
| daily-recommendation-trigger | One daily job per session | DLY-04, DLY-08 | unit + **beta/gamma deployed** |
| daily-recommendation-trigger | Result is an unpublished plan version | DLY-05, DLY-06, DLY-08 | unit + policy sim + **beta/gamma deployed** |
| recommendation-contracts | Contracts 1.1.0 additions | CON-01 | contract |
| recommendation-contracts | Backward compatible with 1.0.0 | CON-02 | contract + pipeline gate |

## Workflow follow-up

- Archive after the gamma deployed tests and the prod smoke pass.
