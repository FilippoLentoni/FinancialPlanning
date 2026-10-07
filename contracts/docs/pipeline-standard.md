# Pipeline standard

Task 10.1. Design D6. Spec: environment-promotion, "Standard pipeline stages", "Immutable
artifact promotion", "Rollback recording and procedure" and "Phase 1 fixture-backed deployment".
`finplan-conformance pipeline-check` checks this standard mechanically against a synthesized
pipeline template (ENV-09).

Every repository (FinancialPlanning, FinanceModel, FinanceLambdasTool, FinanceAgent) has **its
own** pipeline. Each pipeline uses CodePipeline V2 and CodeBuild, runs in the primary region from
environment configuration (initially `us-east-2`), and is created once by the
[bootstrap](bootstrap-runbook.md).

## Stage order

| # | Stage | Contents |
|---|---|---|
| 1 | **Source** | CodeConnections source action on branch `main`. The connection comes from `/finplan/shared/<repo>/config/codeconnection-ref` and is never a literal ARN in the template |
| 2 | **Build** (and test) | Unit tests; contract conformance for the pinned version; `leak-scan`, `copied-id`, `live-perm-scan`, `ownership-check`, `pipeline-check` and the policy checks; `cdk synth`; artifact digests; assign `release_id` (`rel_<ULID>`) |
| 3 | **Beta** | Deploy, publish the beta manifest, integration-beta tests |
| 4 | **Gamma** | Deploy, publish the gamma manifest, gamma tests (including the isolation suite) |
| 5 | **Manual approval** | A Manual approval action. A human approves promotion to prod |
| 6 | **Prod** | Deploy, publish the prod manifest, smoke tests on the synthetic prod portfolio |

A failing stage stops promotion. A failure in gamma stops the pipeline before the approval stage,
so prod is unchanged.

## Artifact-only promotion

- The Build stage produces **one** set of immutable, digest-addressed artifacts per commit: the
  cloud assembly plus container image digests.
- Beta, gamma and prod deploy **the same artifacts**. Only the environment configuration differs.
- No stage after Build reads the source checkout or runs `cdk synth`, and nothing is rebuilt.
- Every manifest of one build records the same `release_id` and `artifact_digest`. The prod
  manifest's `artifact_digest` equals those of beta and gamma for that `release_id` (ENV-10).
- Deploy actions assume the **scoped deploy role** created by the bootstrap, never the identity
  that ran the bootstrap.

## Release manifest and approval recording

Each deploy writes `/finplan/<env>/<repo>/release/manifest`, which validates against
`core/v1/release-manifest.json`. It also writes `/finplan/<env>/<repo>/release/current-release-id`.
The manifest records `repo`, `environment`, `region`, `release_id`, `source_commit`,
`artifact_digest`, `contract_version` (plus `contract_digest`), `deployed_at`,
`previous_release_id`, `outputs` (logical key to SSM parameter name, own segment only) and
`served_contract_majors`.

- **Prod approval.** The prod manifest must carry `approved_by` and `approved_at`; the schema
  rejects a prod manifest without them. Together with `release_id`, they record who approved
  which release, and when.
- **Rollback.** A rollback manifest also carries `rolled_back_from` (see [rollback.md](rollback.md)).
- **Rollback ledger.** SSM parameter history plus a copy of each manifest in the pipeline
  artifact store. A manifest larger than 4 KB uses the advanced parameter tier.

## Smoke tests on a synthetic portfolio

Prod smoke tests use one dedicated portfolio flagged `synthetic: true`. They read and validate,
and may create paper or simulated records on that synthetic portfolio only. They never mutate
real plans and never assert changing market values. Real-provider tests are optional and
rate-limited, and they never run in prod smoke.

## Rollback by `release_id`

The pipeline declares the variable `rollback_to_release_id`. Setting it redeploys the stored
cloud assembly of that recorded release through the normal deploy actions, without rebuilding.
CloudFormation's automatic rollback handles a failed deploy inside one stack update. Data is never
rolled back. Full procedure: [rollback.md](rollback.md).

## Mechanical check (ENV-09)

```sh
cd contracts/python
uv run finplan-conformance pipeline-check cdk.out/<PipelineStack>.template.json
# fixture examples:
uv run finplan-conformance pipeline-check tests/data/infra/pipelines/valid/standard.json            # PASS
uv run finplan-conformance pipeline-check tests/data/infra/pipelines/invalid/missing-approval.json  # FAIL
```

The check fails when any of these is true:

- A stage is missing or out of order; for example, the approval stage is missing or comes after prod.
- The source is not `main`, or it names a literal connection ARN.
- A post-build stage consumes the source artifact or runs `cdk synth`.
- A deploy action runs without a scoped role.
- Prod has no smoke tests.
- The pipeline is not V2 or has no `rollback_to_release_id` variable.

## Phase 1

In phase 1 every repository deploys through all three environments using only synthetic fixtures
and provider mocks. There is no GPU compute, no live market-data contract and no live trading.
Model-backed operations return `DEPENDENCY_UNAVAILABLE` until their producer release exists in
that environment.
