# Spec Delta

## Purpose

Complete the auditable daily paper-investment lifecycle so recommendations, human decisions, holdings revisions and observed outcomes can be reconstructed through MCP.

## ADDED Requirements

### Requirement: Persist issued decisions
The system SHALL retain an immutable issued recommendation from either algorithm family with its market snapshot, saved holdings revision, model identity, exact numerical output and creation timestamp.

#### Scenario: Issuance leaves holdings unchanged
- **WHEN** a recommendation is generated
- **THEN** a retrievable decision identifier is returned and the saved holdings revision is unchanged

### Requirement: Confirmed paper acceptance
The system SHALL apply an explicitly accepted paper recommendation at recorded reference prices with disclosed costs and fill semantics, commit one new holdings revision, and retain acceptance provenance. Rejection SHALL retain its decision record without changing holdings.

#### Scenario: Repeated acceptance cannot double apply
- **WHEN** the same accepted decision is submitted again or a stale revision is supplied
- **THEN** the original resolution is returned or a conflict is reported without a second portfolio update

### Requirement: Historical retrieval and postmortems
The system SHALL expose recent holdings revisions, issued decisions and approved market snapshots through MCP, with stable references joining decision, input, outcome and before/after state. Explanations SHALL distinguish model reasoning, input changes, observed paper accounting and unavailable calibrated forecasts.

#### Scenario: Review three previous states
- **WHEN** a user requests the last three portfolio revisions and related market inputs
- **THEN** stored records are returned newest first with their decision and snapshot links and missing evidence is explicit

### Requirement: Durable activity and research feedback
The system SHALL retain sanitized tool requests/results and agent turn evidence with caller, timestamps, correlation and version identity, while excluding credentials and download grants. Accepted/rejected decisions and observed discrepancy evidence SHALL be available to the existing bounded research review.

#### Scenario: Review a prior interaction
- **WHEN** a user requests evidence from a previous day
- **THEN** durable records can be retrieved independently of transient conversation memory

### Requirement: Beta rollout and budget isolation
The implementation SHALL deploy only to beta, preserve Gamma/prod references and serving strategy selection, and operate under the existing $50 project budget without starting retraining as part of this change.

#### Scenario: Deploy and verify beta
- **WHEN** the coordinated release is verified
- **THEN** both MCP and hosted-agent acceptance/history paths pass without promoting another environment
