# Design

## Context
See proposal.md. The paid runner is operator scoped; platform raw/curated buckets and IAM APIs already exist. Supplemental model gate is not integrated.

## Goals / Non-Goals
Goals: immutable import, bounded date-scoped reads, input feature protocol, optimizer sensitivity comparison and tool access. Non-goals: unconstrained news scraping, paid LLM loops, live orders or automatic production promotion.

## Decisions
Use additive contract 1.8.0. Central platform API imports operator-authenticated deliveries into frozen raw objects and derived curated manifests. A record ID hashes its canonical content. Day indexes support bounded reads. Rights declarations require evidence; raw search text remains untrusted. Deterministic ETF feature extractor produces sentiment, event count, uncertainty and age with missingness. Model compares bounded risk/return perturbations under the same solver constraints; effects are sensitivity assumptions, not learned forecasts. PPO/LLM adapters publish compatible inputs without changing released production artifacts.

## Risks / Trade-offs
Unknown licenses → quarantine; missing publication times → quarantine. Thin new corpus → report historical comparison unavailable. Synthetic fixture comparisons verify mechanics, not investment returns. Search descriptions do not establish relevance. Root retains funded-wallet check and cumulative USD 10 ledger, never resets it.

## Migration Plan
Publish contracts, deploy platform before model and tools; beta-only routes; verify actual delivery and hosted tool before claiming complete. Roll back additive consumers and disable paid collection while retaining immutable evidence.
