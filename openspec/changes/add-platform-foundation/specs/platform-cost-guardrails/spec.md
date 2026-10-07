# Spec Delta

## Purpose

Keeps all project AWS spending within the user's USD 50 total budget (everything, not split per repository). It provides a project budget with staged alerts and an enforcement action, cost-allocation tagging, and a phase 1 rule that the platform deploys no always-on or GPU compute.

## ADDED Requirements

### Requirement: Project budget at the cost ceiling
The platform SHALL own one account-level AWS Budgets cost budget for this project, deployed with the pipeline (tooling) stack, not per environment. Its limit comes from `/finplan/shared/financialplanning/config/cost-ceiling-usd`, initially 50. Alerts MUST go to a human-configured notification target at 50%, 80% and 100% of actual spend, and at 100% of forecast spend.

#### Scenario: Eighty percent of actual spend
- **WHEN** actual project spend crosses USD 40 with the ceiling at 50
- **THEN** the notification target receives the 80% alert, and no deny is applied

#### Scenario: Forecast exceeds the cap
- **WHEN** forecast project spend exceeds USD 50
- **THEN** the configured notification target receives a forecast alert

#### Scenario: Ceiling raised
- **WHEN** the user changes the ceiling parameter and the tooling stack is updated by the authenticated bootstrap
- **THEN** the budget limit equals the new value, and no repository file contains the value as a literal other than the default in environment configuration

### Requirement: Default budget allocation
The bootstrap SHALL write `/finplan/shared/financialplanning/config/budget-allocation` with the default categories `platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25 and `reserve` 5 (USD) when the parameter is absent, and MUST leave an existing user-set value unchanged. An allocation whose categories sum to more than the ceiling MUST be rejected. TypeSafe Jev spend MUST NOT be counted.

#### Scenario: First bootstrap
- **WHEN** the tooling stack is bootstrapped and no allocation parameter exists
- **THEN** the parameter holds the five default categories summing to 50

#### Scenario: User-edited allocation preserved
- **WHEN** the user has set `gpu` to 20 and `reserve` to 10 and the tooling stack is updated
- **THEN** the parameter keeps the user's values

#### Scenario: Allocation above the ceiling
- **WHEN** the allocation categories sum to 60 with the ceiling at 50
- **THEN** the bootstrap stops with a validation error and pre-flight checks that read the allocation refuse paid work with `BUDGET_EXCEEDED`

### Requirement: Enforcement action at the cap
At 100% of actual spend, a budget action SHALL attach a deny policy to the roles published in each repository's `config/budget-enforced-role-names` (deployment, job-submission, ingestion and explanation-runtime roles). The policy blocks creating or starting billable compute, Bedrock model invocations and new pipeline executions. Read access and the plan API's read operations MUST keep working. Removing the deny MUST require a human.

#### Scenario: Cap reached
- **WHEN** actual project spend reaches the ceiling
- **THEN** the deny policy is attached, a new on-demand ingestion fails with `BUDGET_EXCEEDED`, and plan version reads still succeed

#### Scenario: Release after review
- **WHEN** a human detaches the deny policy after raising the ceiling
- **THEN** ingestion and deployments resume without any redeploy

### Requirement: Cost-allocation tagging
Every platform resource SHALL carry the contract package's cost tag keys: project, owning repository, environment and logical role. The build stage MUST fail if a synthesized resource that supports tags lacks them.

#### Scenario: Untagged resource
- **WHEN** the synthesized template contains a taggable resource without the environment tag
- **THEN** the build stage fails and names the resource

### Requirement: No always-on compute in phase 1
Phase 1 platform resources SHALL be serverless or on-demand: object storage, on-demand metadata capacity, functions, the API, the scheduler and the budget. The platform MUST NOT deploy provisioned-capacity databases, always-on containers or instances, NAT gateways, GPU resources or model endpoints.

#### Scenario: Provisioned resource introduced
- **WHEN** the synthesized template contains an instance, NAT gateway, endpoint or provisioned-capacity table
- **THEN** the build-stage cost check fails

### Requirement: Budget pre-check for on-demand ingestion
On-demand ingestion SHALL check the current budget state before calling a provider. It MUST refuse with `BUDGET_EXCEEDED` (`retryable` false) when the enforcement action is active.

#### Scenario: Ingestion while capped
- **WHEN** an agent tool triggers ingestion after the cap action is active
- **THEN** the ingestion returns `BUDGET_EXCEEDED`, makes no provider call and creates no snapshot
