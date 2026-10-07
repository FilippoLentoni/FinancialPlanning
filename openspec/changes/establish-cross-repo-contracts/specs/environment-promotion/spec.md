# Spec Delta

## Purpose

Defines how each repository is built and promoted through isolated beta, gamma and prod environments. It covers environment and permission boundaries, the release manifest and SSM naming convention for cross-repository references, immutable artifact promotion, rollback recording and the one-time pipeline bootstrap.

## ADDED Requirements

### Requirement: Three isolated environments
The system SHALL have exactly three deployment environments: `beta` (integration), `gamma` (production-like validation) and `prod` (released system). Each owning repository MUST deploy separate per-environment instances of its resources, named and tagged with the environment. Resources MUST NOT be shared across environments, except the account-level shared resources listed in "Account-level shared resources".

#### Scenario: Platform buckets per environment
- **WHEN** the platform deploys to beta, gamma and prod
- **THEN** three distinct sets of buckets and metadata tables exist, each tagged with its environment and owning repository

### Requirement: Single-account environment isolation
All three environments SHALL live in one AWS account in the primary region (user decision, 2026-10-07). Isolation MUST use environment-qualified names, the `environment` tag, separate per-environment buckets, tables and endpoints, and a permission boundary on every pipeline-created role denying actions on another environment's resources. A later move to multiple accounts MUST need only environment configuration changes.

#### Scenario: Role acts on another environment's resource
- **WHEN** a beta role created by any pipeline calls an action on a resource tagged `environment` `gamma`
- **THEN** the permission boundary denies the call, and the isolation test records the denial as a pass

#### Scenario: Role created without the boundary
- **WHEN** a synthesized template declares an IAM role without the environment permission boundary
- **THEN** the build-stage policy check fails and names the role

#### Scenario: Boundary written as CDK synthesizes it
- **WHEN** a role's boundary is an ARN that CDK writes as `Fn::Join` of the partition and account pseudo parameters and the boundary name
- **THEN** the policy check resolves it without any normalization step and accepts it only if the name is the role's environment boundary

#### Scenario: Boundary accepted by IAM
- **WHEN** the permission-boundary policies are synthesized
- **THEN** every name-based deny is a per-service ARN pattern built from the partition, region and account pseudo parameters, with no wildcard service segment. Each policy passes CloudFormation template validation and stays within IAM's managed-policy size limit

### Requirement: Account-level shared resources
Only pipeline tooling, the project budget and its action, the contract registry, immutable digest-addressed image repositories, and pre-existing third-party credential secrets referenced by name (initially the Jev API key, owner FinanceModel) MAY exist once per account. Each MUST have one owning repository, carry `environment` `shared` where taggable, and hold no environment data. Account-level quotas are external limits and MUST NOT be modelled as a shared lease.

#### Scenario: Quota shared by environments
- **WHEN** a gamma FinanceModel job cannot start because a beta job holds the account's only processing slot
- **THEN** the gamma run is re-queued by FinanceModel's per-environment controls, and no cross-environment lease resource exists

#### Scenario: Shared third-party credential
- **WHEN** a FinanceModel Jev strategy job runs in gamma
- **THEN** its gamma job role reads only the secret named in `/finplan/shared/financemodel/secret-ref/jev-api-key`, no repository declares or writes that secret in IaC, and no other repository's role can read it

#### Scenario: Permission boundaries are pipeline tooling
- **WHEN** the FinancialPlanning tooling stack creates the permission-boundary policies of beta, gamma, prod and the shared tooling roles
- **THEN** they are account-level pipeline tooling owned by FinancialPlanning, hold no environment data and pass the ownership check

#### Scenario: Environment data in a shared resource
- **WHEN** a synthesized template places environment data (snapshots, plans, run outputs) in a resource tagged `shared`
- **THEN** the ownership conformance check fails the build

### Requirement: Per-environment pipeline roles
A pipeline role that serves exactly one environment (its deploy, CloudFormation execution or stage role) MAY be declared in the repository's account-level pipeline stack. Such a role MUST be tagged with that environment and MUST carry that environment's permission boundary. The ownership matrix MUST record it in a per-environment pipeline-role row owned by the same repository.

#### Scenario: Per-environment pipeline role in the account-level pipeline stack
- **WHEN** a repository's pipeline stack declares its gamma deploy role
- **THEN** the role is tagged `environment` `gamma`, uses the gamma permission boundary, maps to the repository's per-environment pipeline-role row, and passes the ownership and boundary checks

#### Scenario: Same logical role at account level
- **WHEN** a role with the same logical role is tagged `environment` `shared`
- **THEN** it maps to the repository's account-level pipeline row and must carry the shared boundary

### Requirement: Project cost ceiling
The total AWS cost ceiling SHALL be read from `/finplan/shared/financialplanning/config/cost-ceiling-usd`, initially USD 50 for everything (user decision, 2026-10-07), not split per repository. Spend MUST be divided by the category allocation in `/finplan/shared/financialplanning/config/budget-allocation`, summing to at most the ceiling. Paid work MUST pass a pre-flight check against its category's remaining allocation or be refused with `BUDGET_EXCEEDED`.

#### Scenario: Shared budget
- **WHEN** the FinancialPlanning bootstrap deploys the tooling stack
- **THEN** exactly one project budget exists, tagged `environment` `shared`, with its limit taken from the cost-ceiling parameter, and the allocation parameter holds the defaults `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25 and `reserve` 5 (USD)

#### Scenario: Allocation exceeds the ceiling
- **WHEN** the allocation parameter's categories sum to more than the ceiling
- **THEN** the bootstrap and every pre-flight check reject the allocation as invalid, and no paid work starts

#### Scenario: Category exhausted
- **WHEN** FinanceModel receives a paid CPU research job whose estimated upper bound exceeds the remaining `cpu_research` allocation
- **THEN** the job is refused with `BUDGET_EXCEEDED` and nothing starts

#### Scenario: Third-party spend outside the budget
- **WHEN** a TypeSafe Jev call is made
- **THEN** its cost is recorded separately as TypeSafe prepaid-credit usage and is not counted against the AWS ceiling or any category

### Requirement: Budget alerts and enforcement action
The project budget SHALL notify a human-configured target at 50%, 80% and 100% of actual spend against the ceiling and MUST, at 100%, apply a budget action that attaches a deny policy blocking new billable compute, model invocations and pipeline executions to the roles published at `/finplan/<env>/<repo>/config/budget-enforced-role-names`. Only a human MAY remove the deny.

#### Scenario: Ceiling reached
- **WHEN** actual project spend reaches 100% of the ceiling
- **THEN** the deny policy is attached to the enforced roles, `budget-state` is set, and new paid work in any repository is refused

#### Scenario: Eighty percent alert
- **WHEN** actual project spend crosses 80% of the ceiling
- **THEN** the notification target receives an alert and no deny is applied

#### Scenario: Only the budget action undoes its own deny
- **WHEN** a human reverses the budget action, or AWS Budgets resets it
- **THEN** the detach of the deny policy by the action's execution role succeeds, while any other role under a finplan permission boundary is denied that detach and `budgets:ExecuteBudgetAction`

### Requirement: GPU runs require explicit user approval
Every GPU run SHALL require an explicit, recorded user approval of a cost estimate before it starts, even when the `gpu` allocation covers it. A run without a recorded approval MUST NOT start.

#### Scenario: GPU run without approval
- **WHEN** a Qwen3.6-27B or RL GPU job is submitted with a cost estimate within the `gpu` allocation but no recorded user approval
- **THEN** it stays `awaiting_approval` and no GPU instance is requested

### Requirement: Primary deployment region
All environments SHALL deploy to the configured primary region, initially `us-east-2`. The region MUST come from environment configuration, not literals in application code. Every cross-repository reference in an environment MUST resolve to resources in that same region.

#### Scenario: Region configuration
- **WHEN** a pipeline synthesizes IaC for gamma
- **THEN** it reads the region from the gamma environment configuration and the release manifest records it

### Requirement: Gamma and beta never touch production data
Beta and gamma principals SHALL be unable to read, write or execute against prod data, prod plan state or prod services, enforced by IAM policy and the environment permission boundary inside the single account. Gamma data MUST come from synthetic fixtures or gamma's own ingestion.

#### Scenario: Gamma role reads prod bucket
- **WHEN** a gamma tool Lambda role attempts to read a prod platform bucket
- **THEN** access is denied and the gamma isolation test records the denial as a pass

#### Scenario: Gamma Gateway wiring
- **WHEN** gamma Gateway tools are registered
- **THEN** every target resolves from gamma configuration to gamma Lambdas, gamma platform APIs and gamma model services only

### Requirement: Research compute cannot mutate published state
FinanceModel research and job-execution roles SHALL have at most read access to approved, published input snapshots, and write access only to research storage and the platform's run-output staging area. They MUST NOT be able to write plan versions, publications, executions or authoritative metadata.

#### Scenario: Research job writes a publication
- **WHEN** a SageMaker job role attempts a write to the platform publication table
- **THEN** the write is denied by IAM

#### Scenario: Worker output staged
- **WHEN** a production worker finishes
- **THEN** it writes only to the staging prefix, and the platform's own role validates and commits the plan version

### Requirement: Live financial permissions are separated
No role created by these four pipelines SHALL hold credentials or permissions for live trading, brokerage or exchange accounts (including Coinbase), AgentCore payments or wallet spending. Any future live-financial capability MUST use a separately approved change, separate roles and a separate permission boundary.

#### Scenario: Live permission scan
- **WHEN** a pipeline's IaC policy check runs
- **THEN** it fails if any policy or secret reference grants live-trading, payment or wallet actions

### Requirement: Release manifest per repository and environment
Each successful deployment SHALL publish a release manifest that validates against the contract package schema: `repo`, `environment`, `region`, `release_id`, `source_commit`, `artifact_digest`, `contract_version`, `deployed_at`, `previous_release_id` and an `outputs` map from logical output keys to SSM parameter names.

#### Scenario: Manifest after gamma deploy
- **WHEN** FinanceLambdasTool deploys release `rel_X` to gamma
- **THEN** a gamma manifest for FinanceLambdasTool is written with `release_id` `rel_X`, the pinned contract version and the previous gamma release ID

### Requirement: SSM parameter naming convention
Cross-repository references SHALL be published as SSM parameters named `/finplan/<environment>/<repo>/<category>/<name>`. `<repo>` is the lowercase repo name, `<category>` is one of `release`, `contract`, `api`, `lambda`, `job`, `model`, `agent`, `config`, `secret-ref`. Only the owning repository's pipeline MUST write under its `<repo>` segment.

#### Scenario: Tool Lambda reference
- **WHEN** FinanceLambdasTool publishes the gamma `refresh_market_data` Lambda reference
- **THEN** it writes `/finplan/gamma/financelambdastool/lambda/refresh-market-data-arn` and FinanceAgent reads that name

#### Scenario: Write to another repo's namespace
- **WHEN** the FinanceModel pipeline role attempts to write under `/finplan/gamma/financialplanning/`
- **THEN** the write is denied

#### Scenario: Environment-independent value
- **WHEN** an account-level value such as the contract registry reference is published
- **THEN** it uses the reserved environment segment `shared` (for example `/finplan/shared/financialplanning/contract/registry-ref`) and is written only by the bootstrap or contract-publish step

#### Scenario: Registered identity and role references
- **WHEN** a repository needs another repository's identity provider, direct-test principal or job roles
- **THEN** it reads the registered keys `/finplan/<env>/financeagent/agent/user-pool-ref`, `/finplan/<env>/financeagent/agent/authorizer-metadata-ref`, `/finplan/<env>/financeagent/secret-ref/ci-test-client`, `/finplan/<env>/financelambdastool/config/direct-test-principal-name`, `/finplan/<env>/financemodel/job/job-role-ref` or `/finplan/<env>/financemodel/job/job-api-role-ref`, each written only by its owning repository

#### Scenario: Current release pointer
- **WHEN** any consumer needs the current platform release in prod
- **THEN** it reads `/finplan/prod/financialplanning/release/current-release-id` and `/finplan/prod/financialplanning/release/manifest`

### Requirement: Secrets referenced by name only
Secret values SHALL live only in AWS Secrets Manager (or the AgentCore identity/credential store where applicable). Repositories and manifests MUST contain only the SSM parameter that holds the secret's name, under the `secret-ref` category, never the value.

#### Scenario: Third-party API key
- **WHEN** a FinanceModel Jev strategy job needs the TypeSafe API key in gamma
- **THEN** it reads the secret name `finplan/shared/financemodel/jev-api-key` from `/finplan/shared/financemodel/secret-ref/jev-api-key` and fetches the value at runtime with a gamma role scoped to that one secret

#### Scenario: Explanation provider without a secret
- **WHEN** the FinanceAgent explanation agent calls its default provider, Amazon Bedrock
- **THEN** it authenticates with its IAM role, reads the model ID from `/finplan/<env>/financeagent/config/explanation-model-id`, and no API-key secret or `secret-ref` parameter exists for it

### Requirement: Standard pipeline stages
Each repository SHALL have its own pipeline with stages in this order: source, build and test, beta deploy and tests, gamma deploy and tests, manual approval, prod deploy and smoke tests. A failing stage MUST stop promotion. Prod deployment MUST require a recorded human approval.

#### Scenario: Gamma test failure
- **WHEN** gamma integration tests fail
- **THEN** the pipeline stops before the approval stage and prod is unchanged

#### Scenario: Approval recorded
- **WHEN** an approver approves promotion to prod
- **THEN** the approver identity, timestamp and `release_id` are recorded with the prod release manifest

### Requirement: Immutable artifact promotion
The build stage SHALL produce one set of immutable, digest-addressed artifacts per commit. Beta, gamma and prod MUST deploy those same artifacts with only environment configuration differing. Later stages MUST NOT rebuild.

#### Scenario: Digest equality across environments
- **WHEN** release `rel_X` reaches prod
- **THEN** the prod manifest `artifact_digest` equals the beta and gamma manifests' digest for `rel_X`

### Requirement: Rollback recording and procedure
Each environment SHALL retain the previous release's manifest and artifacts. Rollback MUST redeploy a previously recorded `release_id`'s artifacts through the pipeline without rebuilding, then publish a new manifest that records the rollback source.

#### Scenario: Prod rollback
- **WHEN** prod smoke tests fail after deploying `rel_Y`
- **THEN** operators redeploy `previous_release_id` `rel_X`, and the new manifest records `rolled_back_from` `rel_Y`

#### Scenario: Rollback blocked by contract incompatibility
- **WHEN** rolling back a producer would remove a contract major still pinned by a consumer in that environment
- **THEN** the rollback check reports the conflicting consumer and requires a coordinated rollback

### Requirement: One-time authenticated pipeline bootstrap
Pipeline infrastructure SHALL be created once per repository by a human-run, user-approved IaC bootstrap using the operator's existing authenticated AWS CLI session (user decision, 2026-10-07). It MUST verify the account and region against configuration and create the scoped pipeline, deploy and service roles; later deployments MUST use only those roles. A root caller MUST NOT be refused; the bootstrap instead recommends a scoped, MFA-protected role for later.

#### Scenario: Bootstrap with existing credentials
- **WHEN** the bootstrap runs with the user's existing CLI session and the caller is the account root principal
- **THEN** it proceeds after the account and region checks pass, prints the scoped/MFA-role recommendation, and creates only the scoped automation roles and the pipeline stack

#### Scenario: Bootstrap under in-principle approval
- **WHEN** the bootstrap IaC is implemented and the bootstrap is about to run under the user's in-principle approval of 2026-10-07
- **THEN** the operator first shows the exact stacks to be deployed and a cost estimate, and the bootstrap does not run before the bootstrap IaC exists or during spec work

#### Scenario: Account mismatch
- **WHEN** the STS account differs from the account in local untracked configuration
- **THEN** the bootstrap stops before creating anything

#### Scenario: Deploy after bootstrap
- **WHEN** a release is deployed after bootstrap
- **THEN** the deploy actions assume the scoped deploy roles created by the bootstrap, never the bootstrap caller's identity

### Requirement: Verified source connection
Each pipeline's source stage SHALL reuse an existing AVAILABLE GitHub CodeConnection in the primary region, referenced only through `/finplan/shared/<repo>/config/codeconnection-ref` and never by ARN in repository files. The bootstrap MUST verify repository access with a source-stage dry run (a pipeline execution limited to the source stage on `main`) before any deploy stage is enabled, and MUST fail if the connection is not AVAILABLE or the dry run cannot fetch the repository.

#### Scenario: Dry run succeeds
- **WHEN** the bootstrap runs the source-stage dry run for FinanceModel and the source action fetches `main`
- **THEN** the bootstrap enables the remaining stages and records the dry-run result

#### Scenario: Connection does not cover repository
- **WHEN** the source-stage dry run cannot access the FinanceModel repository through the configured connection
- **THEN** bootstrap stops before enabling deploy stages, with a message asking the user to extend the GitHub App installation to that repository and rerun the dry run

### Requirement: Research jobs are not pipeline deployments
Pipelines SHALL deploy job definitions, containers and the submission interface. Individual experiment runs MUST be submitted at runtime through the job interface and MUST NOT need a pipeline execution, CodeBuild training or Lambda training.

#### Scenario: New experiment
- **WHEN** a researcher submits a backtest with a new configuration
- **THEN** a `run_id` is minted and a SageMaker job starts without triggering any pipeline

### Requirement: Phase 1 fixture-backed deployment
Phase 1 releases SHALL be deployable and testable through all three environments using only synthetic fixtures and provider mocks. No GPU model compute, live market-data contract, or live trading is required. Model-backed operations MUST return `DEPENDENCY_UNAVAILABLE` until their producer release exists in that environment.

#### Scenario: Phase 1 end-to-end in beta
- **WHEN** platform, tool and agent phase 1 releases are present in beta
- **THEN** an agent tool call reads a fixture-backed plan version through Gateway and reports the same `plan_version_id` and checksum as a direct platform plan API read
