# Tasks

Scope: phase 2 enablement plus the daily loop. Test IDs are defined in the mapping table at the end.
- "integration-beta" and "gamma" tests run against the deployed environment.
- CI unit and contract suites never call `yfinance`, Bedrock or SageMaker.

## 1. Contracts 1.1.0

- [ ] 1.1 Add the 1.1.0 schemas and fields from the `recommendation-contracts` spec, with valid and invalid conformance fixtures for each. Verify CON-01 (conformance suite) and CON-02 (compatibility gate passes against 1.0.0 without the 0.x exemption; 1.0.0 schema-upgrade fixtures validate unchanged).
- [ ] 1.2 Register the production-strategy key, writer and readers in the ownership matrix, and the `benchmark_report` reference kind. Verify CON-03 with the ownership check.
- [ ] 1.3 Publish 1.1.0 to the contract registry through the pipeline and pin it in the platform by version and digest. Verify CON-04: the pin check passes in beta and gamma, and the consumer promotion gate rejects a 1.1.0 consumer in an environment still serving 1.0.0.

## 2. Universe dataset

- [ ] 2.1 Add `equity-etf-daily` to the dataset registry and config schema (instrument list with kinds, start date, return basis, cash assumption, mandatory disclosures), leaving `etf-daily` unchanged. Verify UNI-01 unit tests (both datasets configured; undeclared kind or missing disclosure fails the build) and the existing ING-11/ING-13 tests still pass.
- [ ] 2.2 Extend the `yfinance` adapter to fetch full history per ticker with the existing rate limits, and normalize `adj_close`, `dividend` and `split_ratio`. Verify UNI-02 unit tests with synthetic mocked responses (split continuity, range before start rejected).
- [ ] 2.3 Implement full-history snapshot assembly with `source_revised` diffing against the previous snapshot. Verify UNI-03 (rewritten history flagged; previous snapshot checksum unchanged).
- [ ] 2.4 Implement `approval-v2-universe` and the `universe` block with the cash instrument. Verify UNI-04 (one missing ticker keeps the snapshot `committed`; a FinanceModel role read is denied in the policy simulation) and UNI-05 (the cash entry has no observations).
- [ ] 2.5 Add `bias_disclosures` to universe snapshots and their reads. Verify UNI-06 contract tests.
- [ ] 2.6 Add synthetic universe fixtures (`synthetic: true`) for phase 1 and CI. Verify the data-hygiene check (ING-19) passes and no fixture matches the real-data signature list.

## 3. Recommendation approval

- [ ] 3.1 Set `review_state: pending_approval` on validated acceptance results, and supersede older pending versions in the same transaction. Verify REV-01 and REV-04 unit tests against the DynamoDB fake (exactly one pending per plan after concurrent acceptances).
- [ ] 3.2 Require the `approval` block on publish for pending versions, with an atomic publication and `approved` transition. Verify REV-02 (missing block is `PRECONDITION_FAILED`; manual-override publish unchanged).
- [ ] 3.3 Add `POST /v1/plan-versions/{id}/review` (reject, reason, idempotency). Verify REV-03 (rejected is unpublishable; a duplicate returns the original).
- [ ] 3.4 Add the `review_state` filter to the version list, with `current_publication_id`. Verify REV-05 contract tests.
- [ ] 3.5 Grant the review route to the tool `plan-writer`, operator and website roles; deny it and publish to the scheduler and loop roles. Verify REV-06 policy simulations, and REV-07 (approval creates no execution).

## 4. Daily loop

- [ ] 4.1 Implement the state machine (L1) with the snapshot, strategy and budget checks and the outcome record. Verify DLY-01 to DLY-03 unit tests with stubbed SSM, budget state and job API (`skipped_snapshot`, `no_session`, `skipped_no_strategy` makes zero job-API calls, `skipped_budget`).
- [ ] 4.2 Implement submission with the key `daily-<env>-<session_date>` (optional `run_tag`), polling with the 45 min bound, and acceptance. Verify DLY-04 (a duplicate start yields one `run_id`), DLY-05 (`run_timeout`) and DLY-06 (`pending_approval` and `no_version` paths) unit tests.
- [ ] 4.3 Add the outcome read route and the research-plan post-deploy step (`research-plan-ref`). Verify DLY-07 contract tests, and that the post-deploy step is idempotent on a re-run.
- [ ] 4.4 Add the cross-repo grants (FinanceModel `submit_job`/`get_job_status` with the `production_candidate` purpose; read of the strategy key). Verify DLY-08 policy simulation, including the loop role denied publish and review.
- [ ] 4.5 Document the loop, its outcomes and the cost per run in `docs/daily-loop.md`, and verify that the documented outcome names match the contract enum.

## 5. Phase 1 deployment (no real provider, no strategy)

- [ ] 5.1 Deploy through the pipeline to beta and gamma with phase 1 config. Verify **DLY-09 (beta and gamma, deployed)**: invoke the state machine for the fixture session with the strategy key unset, then read the outcome record through the deployed API (`skipped_no_strategy`). The FinanceModel job API access log shows no request, and the version list is unchanged.

## 6. Phase 2 in beta (real data)

- [ ] 6.1 Set beta `phase: 2`, provider `yfinance`, datasets `etf-daily` and `equity-etf-daily`, and deploy. Verify **UNI-07 (beta, deployed)**: after the next 09:00 ET scheduled run, read the latest universe snapshot through the deployed plan API. It must show `yfinance` lineage and pinned library version, 5 instruments from 2010-10-01, `approved` under `approval-v2-universe`, both disclosures, and the `etf-daily` SPY snapshot still produced.
- [ ] 6.2 **DLY-10 (beta, deployed, real job):**
  1. Write `{"strategy_id":"buy_and_hold"}` to the beta production-strategy key through the FinanceModel selection operation (not a raw SSM put).
  2. Start the loop with `run_tag=it-<build>`.
  3. Verify: one FinanceModel run with cost tags; an accepted version with origin `model_run`, `review_state` `pending_approval` and `bias_disclosures`; no publication.
- [ ] 6.3 **REV-08 (beta, deployed):**
  1. Reject the version from 6.2 through the deployed review route as the operator, and verify that a later publish fails with `review_state_rejected`.
  2. Clear the strategy key, start the loop again, and verify `skipped_no_strategy`.

## 7. Gamma and prod promotion

- [ ] 7.1 Promote phase 2 to gamma. Verify UNI-07 in gamma, and that the promotion gate refuses without beta evidence (UNI-08 unit test on the gate).
- [ ] 7.2 **REV-09 (gamma, deployed):**
  1. Run DLY-10.
  2. Approve the recommendation through the deployed publish route with an approval block on behalf of the test user.
  3. Verify that a publication exists, `review_state` is `approved`, there is no execution record, and that an older pending version (from a second tagged run) is `superseded` and cannot be approved.
- [ ] 7.3 After the manual approval stage, enable phase 2 in prod. Verify the **prod smoke (deployed, non-mutating)**: the latest scheduled universe snapshot is `approved` with disclosures; the strategy key is unset unless the user has chosen one; and the latest loop outcome is `skipped_no_strategy` or `pending_approval`. No job is submitted by the smoke.

## Requirement-to-test mapping

| Capability | Requirement | Test ID | Type |
|---|---|---|---|
| research-universe-dataset | Universe dataset identity | UNI-01 | unit |
| research-universe-dataset | Adjusted daily observations from 2010-10-01 | UNI-02, UNI-07 | unit + **beta/gamma deployed** |
| research-universe-dataset | Modeled cash instrument | UNI-05 | unit + contract |
| research-universe-dataset | Adjustment revisions are recorded | UNI-03 | unit |
| research-universe-dataset | Universe snapshot approval rule | UNI-04, UNI-07 | unit + policy sim + **beta deployed** |
| research-universe-dataset | Hindsight and survivorship disclosures | UNI-06, UNI-07 | contract + **beta/gamma deployed** |
| research-universe-dataset | Beta-first real-provider rollout | UNI-07, UNI-08 | **beta/gamma deployed** + unit (gate) |
| daily-recommendation-loop | Loop starts only after a successful scheduled ingestion | DLY-01 | unit |
| daily-recommendation-loop | No production strategy means no run | DLY-02, DLY-09, REV-08 | unit + **beta/gamma deployed** |
| daily-recommendation-loop | Budget pre-check before submission | DLY-03 | unit |
| daily-recommendation-loop | One daily_recommendation job per session | DLY-04, DLY-10 | unit + **beta deployed** |
| daily-recommendation-loop | Run tracking with a bounded wait | DLY-05 | unit |
| daily-recommendation-loop | Staged output accepted as a pending recommendation | DLY-06, DLY-10 | unit + **beta/gamma deployed** |
| daily-recommendation-loop | Loop outcome record | DLY-07, DLY-09 | contract + **deployed** |
| daily-recommendation-loop | Automated principals cannot publish | DLY-08 | policy sim |
| recommendation-approval | Model-run versions start pending approval | REV-01, DLY-10 | unit + **beta deployed** |
| recommendation-approval | Publication requires explicit user approval | REV-02, REV-09 | unit + **gamma deployed** |
| recommendation-approval | Rejection | REV-03, REV-08 | unit + **beta deployed** |
| recommendation-approval | Newer recommendation supersedes older pending ones | REV-04, REV-09 | unit + **gamma deployed** |
| recommendation-approval | Pending recommendations are listable | REV-05 | contract |
| recommendation-approval | Approval never executes trades | REV-07, REV-09 | unit + **gamma deployed** |
| recommendation-contracts | Contracts 1.1.0 content | CON-01 | contract |
| recommendation-contracts | Backward compatibility with 1.0.0 | CON-02 | contract (compat gate) |
| recommendation-contracts | Production-strategy document and key | CON-03 | unit (ownership check) |
| recommendation-contracts | Published before consumers | CON-04 | unit + pipeline gate |

## Workflow follow-up

- The cross-repo end-to-end check (FinanceAgent review flow approving a gamma recommendation) is owned by FinanceAgent `add-recommendation-review-flow`.
- Archive after the gamma deployed tests and the prod smoke pass.
