# Design

## Context

See proposal.md. Four independently released repositories already provide beta portfolio advice and bounded research. Preserve the existing ownership boundaries and uncommitted archive work.

## Goals / Non-Goals

Goals: controlled state experiments, honest news eligibility, auditable results and the authorized ceiling. Non-goals: live trading, automatic strategy activation, paid GPU research or retrospective news fabrication.

## Decisions

Existing shared config defaults to 50 and bootstrap preserves existing SSM values. Update the explicit authorized ceiling/allocation (16 infrastructure, 25 CPU, 10 narration, 25 GPU, 24 reserve), synth and deploy the tooling owner, and verify AWS Budget and SSM. Preserve unrelated account budgets. Raising allocation is not a reset of spending. Retain prior consumption.

The user explicitly requested proposal followed by apply in this turn; that authorization overrides the generic propose skill planning-only pause. USD 100 is the total ceiling. Start with at most USD 5 incremental work and check billed/project-ledger headroom before each compute launch.

## Risks / Trade-offs

Inspected market path → development label and no promotion. Thin training history → report seed dispersion without equating seeds to independent market regimes. Missing news vintages → explicit ineligibility. Public repositories → synthetic fixtures only; private live receipts stay outside repos.

## Migration Plan

Validate locally, deploy owner artifacts through beta pipelines, preserve gamma/prod state and transition baselines, verify compatible consumer releases. Retain prior selected policy reference for rollback; no automatic activation.
