# Durable beta multi-approach modeling evidence

## Why
Automatically evaluated approaches need inspectable point-in-time inference inputs, permanent hourly slots and isolated synthetic portfolio histories. Existing platform ledger tables remain authoritative. Source labels or later reconstructed features cannot reproduce a decision.

## What Changes
- Add beta-only research-input, research-run and portfolio-observation tables with experiment/day and experiment/approach queries; retain existing keys, encryption and immutable S3 artifacts.
- Capture exact prepared input before inference, immutable output and valuation artifacts, permanent slot identity and recoverable bounded invocation leases.
- Commit authorized isolated experiment book revisions, existing decision/history rows and permanent run execution status atomically; retain explicit user confirmation for ordinary portfolios.
- Publish compatible contract 1.12.0 with honest language-model, agentic and control family identifiers and table ownership registrations.

## Deployment
The central owner deploys through the existing beta pipeline. No source change or test here establishes live deployment. No migration replaces existing tables or historic records; new evidence begins forward-only, with controlled replay kept in separate experiments.
