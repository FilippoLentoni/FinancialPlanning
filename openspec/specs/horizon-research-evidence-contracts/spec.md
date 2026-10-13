# horizon-research-evidence-contracts Specification

## Purpose
Provide compatible, reproducible evidence contracts for evaluating sequential portfolio policies and running bounded improvement cycles through hosted agents and remote MCP.

## Requirements

### Requirement: Horizon-aware evidence remains distinct from daily attribution
The shared contracts SHALL preserve decision, snapshot and strategy identity and distinguish observation maturity, sequential policy replay, frozen-allocation holds and recorded paper outcomes. A daily loss SHALL NOT by itself be classified as a failed long-term strategy.

#### Scenario: Partial policy horizon
- **WHEN** an evaluation has fewer completed forward sessions than its recorded primary horizon
- **THEN** the evidence reports a partial observation and the agent cannot present a mature strategy verdict.

#### Scenario: Sequential replay
- **WHEN** a preserved policy can be replayed
- **THEN** the evidence distinguishes its repeated decisions from a hypothetical hold of the first allocation and records the source strategy and cost assumptions.

### Requirement: Recursive improvement has immutable bounded lineage
The shared contracts SHALL describe persisted research cycles and iterations, evidence references, candidate experiments, stopping reasons and proposal-only activation. Requests that launch compute SHALL require an idempotency key and recorded user confirmation.

#### Scenario: Read and estimate
- **WHEN** an authorized caller requests an improvement review without approving compute
- **THEN** the result contains a persisted cycle and proposed experiment or a reason no experiment is appropriate, without launching a paid job.

#### Scenario: Approval and repeat
- **WHEN** an approved launch request is repeated with the same idempotency key
- **THEN** the producer returns the same launch outcome and does not create a second job.

### Requirement: Benchmark families retain independent budget and approval gates
The system SHALL keep Qwen self-hosted compute within the GPU allocation and explicit cost-estimate approval, and SHALL track Jev vendor consumption separately. Missing model readiness, exhausted budget or unavailable fresh evaluation data SHALL remain explicit blockers rather than fabricated successful evaluations.

#### Scenario: GPU request without approval
- **WHEN** a research cycle proposes a Qwen GPU benchmark without approved estimated spend
- **THEN** its result is awaiting approval and no GPU resource starts.

### Requirement: Additive contracts support isolated beta deployment
The package SHALL retain existing valid portfolio lifecycle requests and responses while adding recursive research operations. Producer and consumer releases SHALL use matching compatible beta contracts; gamma and prod SHALL retain their prior deployed releases.

#### Scenario: Existing request after upgrade
- **WHEN** an existing valid evaluation request is checked with the new package
- **THEN** validation succeeds without new required request fields.

#### Scenario: Beta-only rollout
- **WHEN** the new package and services are deployed
- **THEN** beta acceptance exercises hosted AgentCore and direct MCP, while gamma/prod manifests and the original paper holdings remain unchanged.
