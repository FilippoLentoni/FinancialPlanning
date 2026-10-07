# Spec Delta

## Purpose

Defines this repository's own CodePipeline and CodeBuild delivery of the platform through beta, gamma and prod. It applies the contract pipeline standard to the platform's artifacts, tests, published references and rollback.

## ADDED Requirements

### Requirement: Platform pipeline stages
The FinancialPlanning pipeline SHALL run, in order:
1. source from the verified GitHub connection on `main`;
2. build and test (unit, contract conformance, scans, synth, digest, `release_id`);
3. beta deploy plus integration-beta tests;
4. gamma deploy plus gamma tests;
5. manual approval;
6. prod deploy plus smoke tests.

Any failing stage MUST stop promotion.

#### Scenario: Gamma failure
- **WHEN** a gamma test fails
- **THEN** the pipeline stops before approval, and prod keeps its current release

### Requirement: Build-stage gates
The build stage SHALL run, and fail on any failure of:
- unit tests;
- the contract conformance suite for the pinned contract version;
- the ownership check;
- the identifier/secret leak scan;
- the live-financial permission scan;
- the cost-tag and no-always-on checks;
- the schedule-time and phase 1 provider configuration checks.

#### Scenario: Leaked identifier
- **WHEN** a commit contains an account ID pattern outside allow-listed placeholders
- **THEN** the build stage fails, and no artifact is produced

### Requirement: Same artifacts promoted
The build stage SHALL produce one digest-addressed cloud assembly and function bundle set per commit. Beta, gamma and prod MUST deploy that same set with only environment configuration differing.

#### Scenario: Digest equality
- **WHEN** a release reaches prod
- **THEN** its `artifact_digest` equals that of the beta and gamma manifests for the same `release_id`

### Requirement: Published references and manifest
Each deploy SHALL write the platform release manifest and `current-release-id`. It MUST publish under `/finplan/<env>/financialplanning/`: `api/plan-endpoint`, `api/ingestion-endpoint`, `config/run-staging-ref`, `config/ingest-schedule`, and the per-bucket `config/` parameters. Bucket parameter names are fixed in design.md.

#### Scenario: Consumer resolves the plan endpoint
- **WHEN** FinanceLambdasTool's gamma stage resolves the platform plan API
- **THEN** it reads `/finplan/gamma/financialplanning/api/plan-endpoint`, and the value points to the gamma deployment

### Requirement: Environment tests per stage
Beta SHALL run the integration-beta suite against beta resources. Gamma MUST run production-like tests, including isolation denials, against gamma resources. Prod MUST run smoke tests only against a dedicated synthetic portfolio and MUST NOT mutate any other plan.

#### Scenario: Prod smoke
- **WHEN** prod smoke runs
- **THEN** it creates, validates and publishes a version and records a paper execution, all on the synthetic smoke portfolio, and verifies read-back checksums

### Requirement: Rollback without rebuild
Rollback SHALL redeploy a recorded `release_id`'s stored artifacts through the pipeline and record `rolled_back_from`. Data MUST NOT be rolled back. Metadata schema changes MUST be expand-then-contract, so a previous release can read records written by a newer one within the same contract major.

#### Scenario: Rollback after newer writes
- **WHEN** prod rolls back from `rel_Y` to `rel_X` after `rel_Y` wrote records under contract 1.1.0
- **THEN** `rel_X`, pinned to 1.0.x, still reads those records, and the manifest records `rolled_back_from` `rel_Y`

### Requirement: Pipeline bootstrap prerequisites
The pipeline stack SHALL be created once by the bootstrap from the user's existing authenticated AWS CLI session, under the user's in-principle approval of 2026-10-07 and only after its IaC is implemented. The bootstrap MUST verify the account and region against local untracked configuration, MUST NOT refuse a root caller (it prints a scoped/MFA-role recommendation), MUST create the scoped pipeline, deploy and service roles, and MUST pass a source-stage dry run before enabling deploy stages.

#### Scenario: Bootstrap with existing root session
- **WHEN** the bootstrap runs with the user's existing CLI session as the root principal and the account matches configuration
- **THEN** it proceeds, prints the scoped/MFA-role recommendation, and later deploy stages use only the scoped deploy roles it created

#### Scenario: Stacks and cost shown before running
- **WHEN** the bootstrap is about to run under the in-principle approval
- **THEN** the exact stacks to deploy and a cost estimate are shown first, and nothing is deployed during spec work

#### Scenario: Connection not covering repository
- **WHEN** the source-stage dry run cannot fetch FinancialPlanning `main` through the configured existing connection
- **THEN** the bootstrap stops before enabling deploy stages and asks the user to extend the GitHub App installation, then rerun the dry run

### Requirement: Account-level bootstrap stacks
The bootstrap SHALL deploy exactly two account-level stacks, the pipeline store and then the tooling stack that holds the pipeline, because the tooling template exceeds CloudFormation's inline template size and is staged in the store. The per-environment deploy, CloudFormation execution and stage roles declared in the tooling stack MUST carry their environment's tag and permission boundary.

#### Scenario: Two account-level stacks
- **WHEN** the bootstrap plan is printed
- **THEN** it names exactly `finplan-shared-financialplanning-pipeline-store` and `finplan-shared-financialplanning-tooling`, in that order, and no environment stack

#### Scenario: Per-environment pipeline role boundary
- **WHEN** the tooling template is synthesized
- **THEN** each beta, gamma and prod deploy, execution and stage role is tagged with its environment and uses that environment's permission boundary, and the ownership and boundary checks report no problem
