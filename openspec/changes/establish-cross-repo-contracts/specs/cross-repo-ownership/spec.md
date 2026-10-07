# Spec Delta

## Purpose

Assigns each deployable resource of the financial research and planning system to exactly one owning repository. It also defines how consuming repositories reference those resources and the order and compatibility rules under which independently promoted repositories integrate.

## ADDED Requirements

### Requirement: Single owning repository per resource
Every deployable resource SHALL have exactly one owning repository, recorded in the ownership matrix maintained in FinancialPlanning. Only the owning repository's pipeline MUST create, update or delete the resource. Other repositories MUST NOT declare the resource in their IaC.

#### Scenario: Resource declared by a non-owner
- **WHEN** a repository's synthesized IaC declares a resource whose matrix owner is a different repository
- **THEN** that repository's ownership conformance check fails the build stage and names the resource and its recorded owner

#### Scenario: Resource missing from the matrix
- **WHEN** a pipeline would deploy a resource type/logical name that has no entry in the ownership matrix
- **THEN** the conformance check fails and the resource is not deployed until an owner is recorded

#### Scenario: Every synthesized type of an owned construct has a row
- **WHEN** FinancialPlanning synthesizes its environment and account-level stacks, including the API Gateway resources, deployment, stage and gateway responses, handler roles and log groups, SSM reference parameters, the schedule's dead-letter queue, queue policy and alarm, the metadata sweeper, the permission-boundary policies, the budget topic policy and subscriptions, and the pipeline store's bucket policy
- **THEN** every resource maps to an owned matrix row or is attributed to its owned parent, and the ownership check reports zero problems without any list of accepted exceptions

### Requirement: CDK-generated helper resources are attributed, never ignored
The ownership check SHALL attribute every CDK-generated helper resource in a synthesized template either to the owner of its parent construct or to an entry of an explicit, reviewed allow-list in the ownership matrix. A helper MUST NOT be silently ignored, and the check's report MUST list how each helper was attributed.

#### Scenario: Helper attached to an owned parent
- **WHEN** a repository's template contains the default `AWS::IAM::Policy` that CDK attaches to a role the repository owns, and an `AWS::Lambda::Permission` on a function it owns
- **THEN** the check attributes both helpers to the parent's matrix row and the report lists each attribution

#### Scenario: Helper of a resource owned by another repository
- **WHEN** a helper references a parent resource whose matrix owner is a different repository
- **THEN** the check fails the build stage and names the helper, its parent and the parent's owner

#### Scenario: Helper with no parent and no allow-list entry
- **WHEN** a template contains a CDK-generated resource with no logical role, no parent construct in the template and no matching allow-list entry
- **THEN** the check fails the build stage; it passes only after the resource is attributed to an owned parent or a reviewed allow-list entry is added

#### Scenario: Allow-listed CDK resource
- **WHEN** a template contains `AWS::CDK::Metadata` or a CDK custom-resource provider function and role that match a reviewed allow-list entry
- **THEN** the check attributes them to the template's repository through that entry and lists them in the report; removing the entry makes the same template fail

#### Scenario: Helper types covered
- **WHEN** a template contains CDK-generated `AWS::IAM::Policy`, `AWS::Lambda::Permission`, `AWS::CDK::Metadata`, custom-resource provider functions or roles, log-retention helpers, or untaggable `AWS::ApiGateway::Method` resources
- **THEN** each one is checked and attributed like any other resource; none is skipped by type, and a method on another repository's API fails

#### Scenario: Unreviewed allow-list entry
- **WHEN** an allow-list entry lacks its reason, its review record, its resource types or a logical-ID or construct-path pattern
- **THEN** loading the ownership matrix fails and no template is checked against it

### Requirement: Baseline ownership assignments
The ownership matrix SHALL assign at least: FinancialPlanning: platform buckets, metadata tables, plan APIs, ingestion, scheduler, contract package, manifest schema. FinanceModel: research storage, SageMaker jobs/containers, model artifacts and registry, the Jev API key reference. FinanceLambdasTool: MCP adapter Lambdas. FinanceAgent: AgentCore Runtime, Gateway, tool registration, explanation-provider configuration, identity provider. Each repo: its own pipeline.

#### Scenario: Looking up the owner of the plan API
- **WHEN** any repository needs to know who owns the plan lifecycle API
- **THEN** the matrix states FinancialPlanning as owner and lists FinanceLambdasTool, FinanceAgent (via Gateway tools) and the website as consumers

#### Scenario: Looking up the owner of the Jev API key
- **WHEN** any repository needs the TypeSafe Jev API key
- **THEN** the matrix states FinanceModel as owner of the reference, lists only FinanceModel Jev job roles as consumers, records the secret as account-level shared and pre-existing, and gives `/finplan/shared/financemodel/secret-ref/jev-api-key` as the reference

#### Scenario: Looking up the explanation provider
- **WHEN** a repository looks up the explanation-provider row
- **THEN** the matrix states FinanceAgent as owner of the Amazon Bedrock provider configuration (IAM auth, model ID at `/finplan/<env>/financeagent/config/explanation-model-id`) and contains no OpenAI secret row

#### Scenario: External resources
- **WHEN** a repository looks up the GitHub CodeConnection, Bedrock model access or the market-data provider source
- **THEN** the matrix marks each as `external`, owned by no repository and referenced only through configuration

#### Scenario: Market-data provider is reached only through the platform
- **WHEN** a repository other than FinancialPlanning needs market data
- **THEN** the matrix lists FinancialPlanning ingestion as the only consumer of the `yfinance` provider source, and the repository reads approved platform snapshots (FinanceModel) or calls the platform ingestion/snapshot API (FinanceLambdasTool) instead of the provider

#### Scenario: Looking up the identity provider
- **WHEN** the Gateway, the Runtime, the website or a CI client needs the identity provider of an environment
- **THEN** the matrix states FinanceAgent as the final owner of that environment's Cognito user pool, referenced through `/finplan/<env>/financeagent/agent/user-pool-ref` and `/finplan/<env>/financeagent/agent/authorizer-metadata-ref`, with the CI client secret name at `/finplan/<env>/financeagent/secret-ref/ci-test-client`

#### Scenario: Looking up the owner of SageMaker job definitions
- **WHEN** a tool wrapper needs to submit an experiment
- **THEN** the matrix states FinanceModel owns the job definitions and the submission interface, and FinanceLambdasTool consumes them only through the published job-submission reference

### Requirement: Consumers reference resources only through published references
A consuming repository SHALL resolve another repository's resources only via that environment's release manifest or SSM parameters defined by the environment-promotion capability. Hard-coded identifiers (account IDs, ARNs, bucket names, endpoint URLs) MUST NOT appear in any repository file.

#### Scenario: Consumer resolves a Lambda reference
- **WHEN** the FinanceAgent gamma pipeline registers Gateway tools
- **THEN** it reads the gamma FinanceLambdasTool Lambda references from gamma SSM parameters or the gamma release manifest, never from a literal in source

#### Scenario: Literal identifier committed
- **WHEN** a commit contains a string matching an AWS account ID, ARN or bucket-name pattern outside allow-listed placeholders
- **THEN** the repository's secret/identifier scan fails the build stage

### Requirement: No duplicate authoritative plan state
Only FinancialPlanning SHALL hold authoritative portfolio, plan, plan-version, publication and execution state. Other repositories MUST NOT persist their own authoritative copy and MUST NOT mint platform identifiers other than those the identifier spec delegates to them.

#### Scenario: Tool wrapper caches a plan
- **WHEN** a FinanceLambdasTool Lambda returns plan data
- **THEN** it obtains it from the platform plan API by `plan_version_id`, and any cache it keeps is non-authoritative and keyed by that immutable ID

#### Scenario: Model worker produces plan output
- **WHEN** a FinanceModel production worker finishes a run
- **THEN** its output is written to a staging location and committed as a plan version only by the platform after validation

### Requirement: Explicit cross-resource permissions
Each cross-repository invocation or data access SHALL be granted explicitly by the owning repository through a scoped resource policy or role trust naming the consumer's environment-specific principal. Wildcard cross-environment grants MUST NOT be used.

#### Scenario: Gateway invokes a tool Lambda
- **WHEN** the gamma Gateway invokes a gamma tool Lambda
- **THEN** the invocation succeeds because FinanceLambdasTool granted invoke permission to the gamma Gateway principal published in gamma configuration

#### Scenario: Cross-environment invocation attempt
- **WHEN** the gamma Gateway attempts to invoke a prod tool Lambda
- **THEN** the call is denied by policy

### Requirement: Integration order
Initial integration SHALL follow the order platform (FinancialPlanning), then model service (FinanceModel), then tool wrappers (FinanceLambdasTool), then agent and Gateway (FinanceAgent). A consumer MUST NOT promote to an environment where its required producer release is absent. Fixture-backed tools MAY release before model compute exists.

#### Scenario: Tool wrapper deploys before model service
- **WHEN** FinanceLambdasTool deploys to beta and the model-service references are absent in beta
- **THEN** only fixture-backed tool operations are enabled and model-backed operations return a `DEPENDENCY_UNAVAILABLE` error

#### Scenario: Agent promoted without tool release
- **WHEN** the FinanceAgent gamma stage finds no compatible FinanceLambdasTool release recorded in gamma
- **THEN** Gateway registration is skipped and the stage fails with a dependency-missing message

#### Scenario: Gateway principal published before tool grants
- **WHEN** FinanceLambdasTool deploys to an environment where `/finplan/<env>/financeagent/agent/gateway-principal-ref` does not exist yet
- **THEN** it deploys with direct-test invoke grants only, and once the FinanceAgent bootstrap has published the principal, a configuration-only redeploy of the same `release_id` adds the Gateway grant without a rebuild

### Requirement: Backward-compatible publish before consumer update
A producer SHALL publish a backward-compatible interface or contract version to an environment before any consumer depending on the new behavior is promoted there. A breaking change MUST be introduced as a new major contract version served alongside the previous major until all consumers in that environment have migrated.

#### Scenario: Additive field
- **WHEN** the platform adds an optional field to a response schema
- **THEN** it releases a minor contract version and existing consumers pinned to the previous minor continue to pass conformance tests

#### Scenario: Breaking change
- **WHEN** a field is removed or its meaning changes
- **THEN** a new major contract version is released, the previous major remains served, and a migration entry records the consumers that must move and the removal criterion

### Requirement: End-to-end validation requires compatible releases in the same environment
End-to-end and Gateway-registration tests SHALL run only when every participating repository has a release recorded in the same environment whose pinned contract versions satisfy each other's compatibility ranges.

#### Scenario: Compatible releases present
- **WHEN** gamma holds platform, model, tool and agent releases whose pinned contract majors match
- **THEN** the gamma end-to-end suite runs against gamma resources only

#### Scenario: Incompatible contract majors
- **WHEN** gamma holds a tool release pinned to contract major 2 and a platform release that serves only major 1
- **THEN** end-to-end validation reports an incompatibility and the consumer's promotion is blocked
