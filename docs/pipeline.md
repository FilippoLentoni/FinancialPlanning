# Platform pipeline and cost guardrails

Tasks 9.1 to 9.4 and 10.1 to 10.4 of `add-platform-foundation`. Specs: platform-pipeline
(PIPE-01 to PIPE-07) and platform-cost-guardrails (COST-01 to COST-06). Contracts: D6, the
[pipeline standard](../contracts/docs/pipeline-standard.md), D4, D10 and D11. The one-time
bootstrap is in [bootstrap.md](bootstrap.md).

## Stages (PIPE-01)

`finplan-shared-financialplanning-pipeline` is a CodePipeline V2 pipeline with CodeBuild. It lives
in the tooling stack (`infra/stacks/pipeline.py`), and its stages run in this order:

| Stage | Actions |
|---|---|
| Source | CodeConnections on `main`. The connection resolves from `/finplan/shared/financialplanning/config/codeconnection-ref` (a CloudFormation SSM parameter, never a literal ARN). `#{SourceVariables.CommitId}` is passed to the build |
| Build | `scripts/build_stage.py`: pre-synth gates, then `cdk synth` once (`scripts/synth.py`), post-synth gates, asset publishing, then packaging. Output: the single artifact `BuildOutput` |
| Beta | Deploy `storage`, `metadata`, `api`, `ingestion` (one CloudFormation action each, in dependency order); `PublishManifest`; `IntegrationBetaTests` |
| Gamma | The same deploys; `PublishManifest`; `GammaTests` |
| Approval | `ApproveProd`, one Manual approval action |
| Prod | The same deploys; `PublishManifest` (with the approver and time); `SmokeTests` |

- **A failing action stops promotion.** A gamma failure stops before the approval, so prod keeps
  its release.
- **Deploy action roles.** Deploy actions run under `finplan-shared-financialplanning-deploy-role-<env>`.
  CloudFormation then uses `...-deploy-role-<env>-exec`, which is limited to
  `finplan-<env>-financialplanning-*` resources and may create only roles that carry
  `finplan-<env>-permission-boundary`. These two roles and the stage role
  `finplan-<env>-financialplanning-operator-pipeline-stage` are declared in the account-level
  tooling stack but tagged `environment=<env>` and bounded by `finplan-<env>-permission-boundary`
  (contracts D13 "Per-environment pipeline roles", ENV-21), so the environment boundary denies
  them every other environment's resources. The pipeline and build roles stay `shared`.
- **The contract check** `finplan_contracts.pipeline_check` (ENV-09) passes on the synthesized
  template. The same check runs in the unit suite and as a build gate.

## Artifacts are built once (PIPE-03)

- **Synthesizer.** The environment stacks are synthesized with
  `infra.stacks.tooling.deployment_synthesizer()`. Their Lambda code lives in the pipeline store at
  `assets/<sha256>.zip`, and their templates carry no CDK bootstrap-version rule. The
  CloudFormation actions therefore need no `CDKToolkit` stack.
- **Asset publishing.** `scripts/publish_assets.py` uploads each asset once, write-once
  (`If-None-Match: *`), as a deterministic zip.
- **What later stages read.** Every action after Build reads only `BuildOutput`, which holds the
  cloud assembly, `release-info.json` and the files the stage actions need. No later stage reads the
  source checkout or runs `cdk synth`; the stage build spec contains neither.
- **Release identity.** `release-info.json` records one `release_id` (`rel_` + ULID) and one
  `artifact_digest`, a SHA-256 over every file of the assembly. Beta, gamma and prod deploy the same
  `BuildOutput`, so their manifests carry the same digest. The cross-environment digest-equality
  check runs in prod smoke and depends on the bootstrap (task 11.5).
- **Lambda bundles.** Before it synthesizes, the build stage builds one code bundle per platform
  function with `scripts/lambda_bundle.py`. Each bundle holds the locked runtime closure from
  `uv.lock` (`uv export --frozen --no-dev --no-emit-project`, installed with `--require-hashes
  --only-binary :all: --python-platform aarch64-manylinux_2_28 --python-version 3.12`), including
  the pinned vendored `finplan-contracts` wheel, plus the `finplan_platform` package (with its
  shipped calendars) and `config/`. `ingestion` and `plan-api` get the `providers` extra (the API
  runs `POST /v1/ingestions` in process); `sweeper` gets the base dependencies. Functions with the
  same extras get byte-identical bundles, so the assembly holds two code assets. Sizes (unzipped,
  limit 250 MB): `ingestion`/`plan-api` 178 MiB (63 MiB zipped), `sweeper` 29 MiB (18 MiB zipped).
  The synth then runs in release mode (`FINPLAN_RELEASE_BUILD=1`, `FINPLAN_LAMBDA_BUNDLE_DIR`), in
  which `infra.stacks.common.lambda_code` fails on a missing or incomplete bundle. The build project
  sets `FINPLAN_RELEASE_BUILD=1`, and any CodeBuild synth counts as release mode unless
  `FINPLAN_RELEASE_BUILD=0` (the offline test environment sets it). A local synth without the flag
  still packages the source tree, which is fine for tests and not deployable.
- **Container images are not supported yet.** If the ingestion function must become a container
  image (task 6.17), an image repository is needed. The ownership matrix has no FinancialPlanning
  image-repository row (a contract gap), and the asset publisher refuses image assets until one
  exists.

## Build-stage gates (PIPE-02)

`scripts/build_gates.py` runs these gates. Any failure fails the build, and nothing is written to
`BuildOutput`:

| Gate | Stage | Reuses |
|---|---|---|
| `contracts-pin` | pre | `scripts/check_contracts_pin.py --rebuild` (exact version plus wheel digest) |
| `config` | pre | `finplan_platform.core.config` (ING-02 schedule time, ING-10 phase 1 provider, ING-13 daily only); `scripts/check_ingest_pins.py`; `scripts/check_data_hygiene.py` |
| `leak-scan` | pre | `finplan_contracts.leak_scan` over the repository |
| `copied-id` | pre | `finplan_contracts.copied_id` (`contracts/`, the producer source, is excluded; `openspec/` is scanned) |
| `conformance` | pre | `finplan-conformance conformance --mode consumer --expect-version <pin>` |
| `unit` | pre | `pytest tests/unit tests/contract -m "not live_provider"` |
| `ownership` | post | `finplan_contracts.ownership` per template. Every problem fails the gate; there is no accepted-gap list |
| `boundaries` | post | `check_role_boundaries` (ENV-18) and `check_shared_resources` (ENV-16), on the templates as synthesized |
| `live-perm-scan` | post | `finplan_contracts.live_perms` |
| `pipeline-structure` | post | `finplan_contracts.pipeline_check` and `bootstrap.check_deploy_roles` |
| `cost` | post | `scripts/cost_checks.py` (COST-03 tags, COST-04 no always-on compute) |
| `lambda-bundle` | post | every code asset that carries `finplan_platform` is a complete bundle from `scripts/lambda_bundle.py` (manifest, `finplan_contracts`, `jsonschema`, `rfc8785`, config, calendars), built for `aarch64-manylinux_2_28`, only arm64 ELF objects, at most 250 MB unzipped |

To run them locally:

```sh
uv run python scripts/build_gates.py --stage pre --skip-unit
uv run python scripts/synth.py --release --out cdk.out   # builds the arm64 bundles, release-mode synth
uv run python scripts/build_gates.py --stage post --assembly cdk.out
```

A plain `scripts/synth.py --out cdk.out` packages the source tree, and the `lambda-bundle` gate
rejects that assembly. To check that the handlers import from a bundle alone on this machine, build
for the host platform: `uv run python scripts/lambda_bundle.py --out /tmp/b --local --import-check`
(the unit suite does the same in `tests/unit/ops/test_lambda_bundle.py`).

## Release manifest and published references (PIPE-04)

`PublishManifest` (`scripts/stage_runner.py publish`, implemented in `scripts/release.py`) runs
after every deploy. It does four things:

1. **Checks the outputs.** It verifies that the deploy published every platform output:
   `api/plan-endpoint`, `api/ingestion-endpoint`, `config/run-staging-ref`,
   `config/ingest-schedule` and `config/bucket-{raw,curated,snapshots,plans,outputs,reports}`. A
   missing output fails the stage.
2. **Validates the manifest.** The manifest must validate against the pinned
   `core/v1/release-manifest.json`. A prod manifest carries `approved_by` and `approved_at`, read
   from the `ApproveProd` action of the same pipeline execution. A rollback carries
   `rolled_back_from`.
3. **Writes three parameters,** each checked with `finplan_contracts.ssm.check_write` as the
   `pipeline` writer bound to the environment:
   - `/finplan/<env>/financialplanning/release/manifest`;
   - `/finplan/<env>/financialplanning/release/current-release-id`;
   - `/finplan/<env>/financialplanning/config/budget-enforced-role-names`, holding the
     ingestion-handler and plan-api-handler roles.
4. **Copies the manifest** to the release ledger at `releases/<release_id>/manifests/<env>.json`.

## Rollback (PIPE-06)

1. Start the pipeline with the variable `rollback_to_release_id=rel_...`.
2. The Build stage fetches `releases/<release_id>/build-output.zip`, verifies its digest and
   re-emits it with `rollback: true`. Nothing is rebuilt and no gate reruns.
3. The deploy stages redeploy it, and each manifest records `rolled_back_from`.

Data is never rolled back. The gamma rollback drill is task 11.4 and depends on the bootstrap.

## Environment tests (PIPE-05)

`scripts/stage_runner.py tests` runs the suite for each environment:

- **beta:** `tests/integration` (the integration-beta suite);
- **gamma:** `tests/integration` (the gamma suite);
- **prod:** `tests/smoke`.

`FINPLAN_TARGET_ENV` and `FINPLAN_SUITE` tell the tests where they run, and the opt-in
live-provider test is always excluded. Every stage fails when its suite executes fewer than one
test (nothing collected, or everything skipped), in beta and gamma as in prod.

The beta and gamma suites are `tests/integration/test_deployed_environment.py` (tasks 11.1 to
11.3). They run in the pipeline only, as the stage role
`finplan-<env>-financialplanning-operator-pipeline-stage`, calling the plan API with SigV4 at the
endpoint in `/finplan/<env>/financialplanning/api/plan-endpoint`
(`tests/smoke/transport.py`). The lifecycle flow is `tests/integration/lifecycle_suite.py`. Each
run creates a fresh synthetic portfolio and plan and uses run-unique idempotency keys. The flow:

1. It ingests a fixture snapshot through `POST /v1/ingestions`.
2. It creates a root version, retries it with the same key, and reuses the key with another
   body, which must fail with `IDEMPOTENCY_KEY_REUSED`.
3. It creates a `no_effect` override and two concurrent overrides on one revision (exactly one
   commits, the other gets `CONFLICT`). A stale `expected_revision` must also get `CONFLICT`.
4. It checks that an unvalidated version cannot be published, then validates and publishes.
5. It records a paper execution and its retry. A `live` execution must get
   `OPERATION_NOT_PERMITTED`.
6. It reads everything back. The `plan_version_id` and checksum must match the direct read and
   the downloaded bytes.

The suites run four tests in gamma and three in beta. The extra gamma test is the isolation check
(ENV-03): the gamma stage role must be denied the prod SSM segment, the prod tables and the prod
buckets. A credential probe checks that the deployed suite kept the stage role's credentials. The
same flow runs offline against the deployment double (`tests/unit/ops/test_integration_double.py`).

Before the suite, `scripts/stage_runner.py tests` sends one signed diagnostic
`GET /v1/plans/<synthetic id>` through the deployed API. A healthy API answers 404 `NOT_FOUND`. A
5xx (API Gateway returns 502 when the function crashes) or an `INTERNAL` envelope fails the stage
at once, with a message that names a Lambda init or import failure as the likely cause. The
integration suite's first read test reports the same hint.

### Incident: source-only Lambda bundle (2026-10-07)

**Symptom.** Every deployed platform function (`plan-api`, `ingestion-handler`,
`metadata-sweeper`, in every environment) failed at init with `Runtime.ImportModuleError: No module
named 'finplan_contracts'`. The code package was about 150 KB. The beta integration suite saw
HTTP 502 from the API. The plan-api log group had no log streams at all.

**Causes.**

- `lambda_code()` packaged the `platform/` source tree unless `FINPLAN_LAMBDA_BUNDLE` was set, and
  nothing in the build stage ever built a bundle or set the variable. The synth succeeded, the
  gates checked only templates, and the source-only asset was published and deployed.
- The plan-api role is an explicit role (`platform_role`), so it has no
  `AWSLambdaBasicExecutionRole`, and CDK grants nothing for an explicit `log_group`. The role had
  no `logs:CreateLogStream` or `logs:PutLogEvents`, so the init failure left no log. The
  permission boundary was not the cause: it allows everything that the environment-tag and
  other-environment denies do not cover. The ingestion and sweeper roles have the managed policy;
  their log groups were empty only because they had not been invoked.

**Fixes.**

- `scripts/lambda_bundle.py` builds the arm64 bundles from `uv.lock` (see "Artifacts are built
  once"), and the build stage builds them before the synth.
- A release-mode synth fails for a function without a complete bundle, and the post-synth
  `lambda-bundle` gate rejects any source-only or non-arm64 code asset. A source-only package can
  no longer reach `BuildOutput`.
- The plan-api role gets `logs:CreateLogStream` and `logs:PutLogEvents` on its own log group
  (statement `OwnLogStreams`).
- The stage runner probes the API before the suite and names the import problem on 502 or
  `INTERNAL`.
- Tests (`tests/unit/ops/test_lambda_bundle.py`) build the bundles for real, for the host platform.
  Each function's handler must import in a `python -I -S -B` whose only non-stdlib path is the
  bundle. A release-mode synth without bundles must fail, and the gate must reject the offline
  (source-only) assembly.

### Incident: false pass in beta and gamma (first pipeline run)

The first pipeline run passed the beta and gamma test actions with zero executed tests.
`tests/integration` then held only the opt-in live yfinance test, which the stage always
excludes, and the stage runner only required executed tests for prod smoke. The same run's prod
smoke failed with `UnrecognizedClientException`. The cause was `tests/conftest.py`, which
replaced the real AWS credentials with offline fakes for every suite, deployed ones included.

There were two fixes:

- `scripts/stage_runner.py` now requires at least one executed test in every environment
  (`tests/unit/ops/test_smoke_double.py`).
- The offline-safety environment (fake credentials, metadata service disabled, no profile or
  config files; `tests/offline_env.py`) now applies only to offline suites. `tests/unit` and
  `tests/contract` always apply it. The root `tests/conftest.py` skips it when the stage runner
  sets `FINPLAN_TARGET_ENV` (`tests/unit/test_offline_env.py`).

Run deployed suites on their own directory, as the stage runner does.

The smoke suite is `tests/smoke/smoke_suite.py`:

1. It reuses or creates the dedicated `synthetic: true` smoke portfolio. The portfolio ID is kept
   in `/finplan/<env>/financialplanning/config/smoke-portfolio-id`.
2. It creates a plan for the run and ingests a fixture snapshot.
3. It creates a root version, checks the JCS SHA-256 checksum and the downloaded bytes, then
   validates and publishes the version.
4. It records a paper execution and checks that neither the publication nor the version changed.

It never touches another plan. In prod it calls the API with SigV4 (`tests/smoke/transport.py`)
as the stage role. Locally it runs against a deployment double: the in-process router, the
DynamoDB fake, moto S3 and the fixture provider (`tests/unit/ops/test_smoke_double.py`).

## Cost guardrails

### Budget (COST-01, task 9.1)

There is one AWS Budgets `COST` budget, `finplan-shared-financialplanning-project-budget`, in the
tooling stack.

- **Limit.** The limit is the SSM parameter `/finplan/shared/financialplanning/config/cost-ceiling-usd`,
  resolved by CloudFormation at deploy time. The bootstrap writes the default from
  `config/shared.json` when the parameter is absent.
- **Alerts.** Alerts fire at 50%, 80% and 100% of ACTUAL spend and at 100% of FORECASTED spend.
  They go to the SNS topic `finplan-shared-financialplanning-budget-alert-topic`. A human supplies
  the e-mail subscriber at bootstrap; it is never committed.
- **Scope.** The budget covers the whole account until the `project` cost-allocation tag is
  activated (`ScopeBudgetToProjectTag`).

### Default allocation (COST-06, task 9.1a)

`scripts/bootstrap.py ensure_budget_allocation` writes
`/finplan/shared/financialplanning/config/budget-allocation` only when it is absent. The defaults
are `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25 and `reserve` 5, read
from the pinned contract schema. A user-set value is preserved, and a sum above the ceiling stops
the bootstrap.

### Enforcement action (COST-02, task 9.2)

At 100% of ACTUAL spend the Budgets action attaches `finplan-budget-enforcement-deny`. This is the
contract policy that denies:

- billable compute;
- Bedrock invocations;
- pipeline executions and builds.

The action applies to the tooling roles and to every published `budget-enforced-role-names`. Reads,
plan reads and Lambda invocation (so ingestion still answers with `BUDGET_EXCEEDED`) are not denied.
Only a human lifts the cap, by `REVERSE_BUDGET_ACTION` or a manual detach ([bootstrap](bootstrap.md),
"When the cap is reached"). The action's own execution role is the only principal a boundary lets
detach the policy (contracts 0.2.2), so AWS Budgets can reset or reverse its action; the action's
logical ID is `BudgetEnforcementActionV2` since the 2026-10-07 reset failure.

The budget-state writer (`platform/finplan_platform/handlers/budget_state.py`, inlined into the
tooling stack) sets `/finplan/shared/financialplanning/config/budget-state` to `enforced` on an
ACTUAL alert at or above the budgeted amount, or on an executed budget action. It never clears the
flag.

### Pre-check (COST-05, task 9.4)

`finplan_platform.core.budget.budget_precheck(ctx, category, *, estimated_usd=0, state=..., reader=None)`
reads the flag through `SsmBudgetStateReader` and applies `finplan_contracts.budget.preflight`:

- **Flag set:** it raises `BUDGET_EXCEEDED` (not retryable) before any provider call.
- **Flag unreadable:** it raises `DEPENDENCY_UNAVAILABLE` (fails closed).

The ingestion gate (`core/ingestion_budget.py`) makes the same decision for `platform_infra`; the
unit suite asserts the two agree.

### Cost checks (COST-03 and COST-04, task 9.3)

`scripts/cost_checks.py` fails the build in two cases:

- **A missing cost tag.** Every taggable resource must carry `project`, `owner-repo`,
  `environment` and `logical-role`; the finding names the resource.
- **Always-on or provisioned resources.** Instances, NAT gateways, interface endpoints, load
  balancers, provisioned databases and tables, always-on containers, model endpoints, provisioned
  Lambda concurrency, API caches and GPU instance types are all refused.

## Contract gaps

Contracts 0.2.0 (design D13) resolved the gaps this change first recorded:

1. **Ownership matrix.** The rows now list every type the platform synthesizes (API Gateway
   sub-resources, handler roles and log groups, SSM parameters, the schedule's dead-letter queue,
   queue policy and alarm, the budget roles, topic policy and subscriptions, the pipeline store's
   bucket policy), plus rows for the metadata sweeper, the permission-boundary policies and the
   per-environment pipeline roles. The ownership gate passes on all 14 synthesized templates with
   zero problems; `scripts/ownership_known_gaps.json` is deleted.
2. **Per-environment pipeline roles** carry their environment's tag and boundary (above).
3. **Boundary checker.** `check_role_boundaries` resolves pseudo-parameter references inside
   `Fn::Join`, so the gate no longer normalizes templates first.
4. **Copied-id scanner.** It skips files that are not plain JSON instead of crashing on YAML
   dates, so `openspec/` is scanned again.
5. **Boundary resource patterns.** The cross-environment deny uses one ARN pattern per service
   with the partition, region and account pseudo parameters (CloudFormation `Fn::Sub`). cfn-lint
   no longer reports `E3510`.

Contracts 0.2.1 (design D14) adds the `AWS::Logs::LogGroup` type to the FinancialPlanning
pipeline row, for the explicit 30-day log groups of the four CodeBuild projects (the
budget-state writer's group already matches the budget row), and drops two deny entries that
named no IAM action (`bedrock-agentcore-control:*`, `bedrock:Converse*`).

Still open:

- **Image assets.** There is no FinancialPlanning image-repository row.
- **Contracts version.** The pin is 0.2.2, which is beta-only. Gamma and prod need 1.0.0 once it
  is published (task 1.2).

### CloudFormation lint

`uvx cfn-lint` over every synthesized template (`cdk.out`, including the nested stage
assemblies) reports no errors. The remaining warnings were reviewed and none indicates a deploy
failure:

- `W3005`: an explicit `DependsOn` that a `Ref`/`GetAtt` already implies (CDK-generated).
- `W3037`: action names cfn-lint does not know in the live-financial deny of every boundary
  (speculative prefixes such as `brokerage:*` or `wallet:*`, and `bedrock-agentcore:*Wallet*` and
  `*Funds*`, which match no current AgentCore action). IAM accepts unknown actions in a policy; a
  deny on an action that does not exist has no effect. Contracts 0.2.1 removed the two entries
  that could never match (`bedrock-agentcore-control:*`, not an IAM prefix, and
  `bedrock:Converse*`, not an IAM action).

## Interfaces for other modules

- `infra/app.py` keeps the CDK default synthesizer for `npx aws-cdk@2 synth`. The pipeline
  synthesizes through `scripts/synth.py`, which uses `deployment_synthesizer()`. An environment
  stack needs nothing else.
- The plan API must admit the stage role `finplan-<env>-financialplanning-operator-pipeline-stage`.
  The current resource policy admits every `finplan-<env>-financialplanning-*` principal.
- Paid or provider operations call `budget_precheck(ctx, "<category>", reader=SsmBudgetStateReader(ssm))`.
