# Horizon evaluation and recursive research — beta verification

Verified at **2026-10-11 01:01:42.912508+00:00**. The beta agent and remote MCP tools support frozen-policy
horizon evaluation, persisted bounded research cycles and identified Qwen/TypeSafe Jev job
adapters. These tests establish software behavior; they do not establish investment skill.

## Deployed sources

| Repository | Runtime source commit | Beta release |
|---|---|---|
| financialplanning | `286ae1e4e116d437527d6bf5b2b77169d1aca523` | `rel_01M4M3FBKRSQ3MY1C8GGP3PQNN` |
| financemodel | `d6edabee2a8abc18d0876bd84cde45d404db6d86` | `rel_01M4M6Q83GFWVT2VSDA0J7NHNP` |
| financelambdastool | `551a6dec81d8737c9a7abc3b8b43a176ef273dee` | `rel_01M4M5Y48RFXZSZANM1EER76PV` |
| financeagent | `a31b0ecf12fa454dedf84bcdc2dc28296f1a7745` | `rel_01M4M6FFG469ZPJQBGV698KXQC` |

All four beta pipeline build/deploy/integration gates passed. Executions were stopped before
Gamma, and original transition settings were restored. Documentation/task follow-up commits
may be newer than these explicitly pinned runtime sources. No Gamma/prod rollout occurred.

Contracts **1.6.0** were published for Python and TypeScript. All four repositories vendor
the same Python wheel, SHA-256
`d1975fd07907edcab39e9a7609c584141281eaa4498a303f6214df5aa22aab2e`.
Pin, conformance and legacy compatibility checks passed, including 645 shared fixtures.

## Live verification

The focused acceptance recorded **33 cases**, including **6 hosted AgentCore turns**,
using an isolated $10,000 paper book. All three acceptance phases passed.

- Direct MCP and hosted AgentCore returned identical six-path PPO horizon evidence: original
  issued sequential policy, unchanged holdings, equal weight, minimum variance, mean variance
  and CVaR. Full private replay evidence is sealed; its checksum and source identities are public.
- Traditional replay passed. A backdated request was explicitly retrospective and ineligible
  for prospective skill evidence. 1 available forward session matured only the one-session diagnostic;
  the original 64-session PPO horizon stayed partial. A latest-snapshot decision correctly
  reported no forward observations. Missing observed paper execution stayed unavailable.
- Recursive preview, immutable retrieval and new turnover feedback passed. Feedback selected
  the released turnover profile. Unconfirmed execution and CI paid execution were denied.
  Previews did not authorize scheduled compute or activate any strategy.
- Qwen and Jev previews produced their matching typed sandbox requests. Hosted and direct
  cost estimates matched. Each launch request paused for confirmation on the exact same cycle.
  No paid benchmark was confirmed or approved.
- Hosted research archives were retrieved through MCP, including bounded chunk reassembly
  where needed. Event/payload checksums, nested research evidence after documented storage
  redaction, and exact skill versions were verified. Public HTTP citations round-trip through a documented lossless
  representation; private locations, credentials and signed URLs remain redacted.
- These deterministic hosted routes used zero additional Bedrock model invocations.

The first post-deployment Qwen preview returned `INTERNAL` with `TimeoutError`, before
any compute launch. Repeating the preview passed without a runtime configuration change;
the failed receipt is retained as `live-acceptance/qwen-research-preview-timeout.json`.
The bounded acceptance is not a sustained-load or availability guarantee.

Native deployed-suite results (explicit skips are retained, not counted as executed checks):

- financialplanning: integration-beta: {'tests': 7, 'failures': 0, 'errors': 0, 'skipped': 2, 'executed': 5}
- financemodel: integration-beta: {'tests': 8, 'failures': 0, 'errors': 0, 'skipped': 1, 'executed': 7}
- financelambdastool: integration-beta: {'tests': 61, 'failures': 0, 'errors': 0, 'skipped': 1, 'executed': 60}
- financeagent: integration-beta: {'tests': 19, 'failures': 0, 'errors': 0, 'skipped': 0, 'executed': 19}

Required unit/contract/build gates and strict OpenSpec validation passed. Focused suites
covered sequential replay and original actors; research claims/idempotency/frozen approval;
benchmark dispatch/import/lease cleanup; actual daily-decision guards; citation validation;
and hosted archive reconstruction. Full GPU readiness was not inferred from fake compute.

## Implemented scope

New decisions freeze their strategy, original holdings, market snapshot, objective and
horizon protocol. Evaluation separates daily PnL/cost attribution from a policy's declared
horizon, uses common counterfactual next-open execution assumptions and preserves partial,
legacy, hindsight and unavailable evidence. It never diagnoses model failure from one loss.

The recursive controller persists evidence, feedback, proposals, iteration/run lineage and
results in immutable research records. Released PPO candidates vary state features,
turnover-aware rewards and episode/discount horizon, comparing seeds and common controls.
Classical candidates and both LLM pilots decide daily; a weekly research schedule does not
change investment decision cadence. Qwen/Jev proposals use an explicitly limited 21-decision
pilot on aligned market sessions, retaining earlier history for warm-up. They are not full
2026 holdout results or directly interchangeable with a longer benchmark window.

Ordinary recursive CPU research remains limited to one job/week, $0.50/week, $2/month and
three iterations within the $50 project budget. A preview alone starts no scheduled research.
GPU/vendor experiments retain their separate approval gates. Candidate activation requires
human review and fresh forward evidence. Arbitrary new code, model families and architectures
become reviewed implementation proposals; automatic source rewriting is not implemented.

Qwen uses `Qwen/Qwen3.6-27B`, revision
`6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`, a pinned vLLM image, exact staged-weight
verification and a temporary network-isolated Training Job with fixed-role arbitration.
Released code is retained under `releases/qwen-code/`, outside the 14-day scratch lifecycle.
Published code identity, bucket lifecycle and job-role read/image permissions were checked.
There is no always-on inference endpoint. Exact weights have not yet been staged in finplan.

Jev uses the verified TypeSafe interface, bucketed descriptors, funding-constrained allocation,
bounded retries/calls/tokens and cutoff-correct calibration. The configured alias and returned
model/drift are retained; an immutable vendor version pin is not claimed. Historical LLM
pretraining leakage and limited calibration labels require explicit disclosure and forward testing.

## Cost and isolation

The conservative incremental estimate for this implementation/verification round is
**$1.17**, within its $5 rollout cap. It includes CodeBuild,
CodePipeline, 3 short CPU integration-lifecycle jobs and a $0.25 other-service allowance.
It is an estimate, not an AWS invoice. AWS Budgets last reported **$9.577/$50**
at `2026-10-10 23:12:43.002000+00:00`; billing updates lag this rollout.

- gamma prod release parameters not written during round.
- production strategy restored.
- advisory policy unchanged.
- all gamma prod manifests unchanged.
- all original transition states restored.
- no active processing or training jobs.
- zero training jobs created this round.
- owned executions all terminal.
- no active round codebuilds.
- no gamma prod actions created this round.
- all beta sources match owned executions.
- all required beta actions passed.
- no new sagemaker endpoints.
- original user paper portfolio unchanged.

No Qwen weight download, GPU benchmark or Jev paid inference was started. All created CPU
test jobs are terminal; test login credentials were removed after acceptance. The user's
original saved paper holdings remained unchanged. The advisory pointer was untouched, and
the production-strategy selection ended in its exact baseline state after integration tests.

## Remaining paid validation

Qwen staging plus the released 900-second pilot estimates about $3.16 AWS; Jev estimates
$0.0675 AWS plus at most $0.0084 planned vendor credits at the verified planning rate.
These are planning estimates, not hard billing caps. The earlier AWS Qwen example took about
4,754 seconds, so loading/evaluating the exact model within the 900-second pilot is unverified.
A full daily historical benchmark needs separately reviewed higher limits and a common window.
No real-GPU readiness, vendor inference success or portfolio-performance advantage is claimed.

The existing [project decisions](/home/ec2-user/projects/_planning/decisions-2026-10-07.md), item 4, require:
“Every GPU run still needs explicit user approval with a cost estimate.” Jev remains
approval-gated under item 7. Deployment does not supply these paid-run approvals.

Detailed behavior: [horizon evaluation](https://github.com/FilippoLentoni/FinanceModel/blob/codex/advisory-policy-agent/docs/decision-horizon-evaluation.md),
[recursive research](https://github.com/FilippoLentoni/FinanceModel/blob/codex/advisory-policy-agent/docs/recursive-improvement.md),
[benchmark deployment/costs](https://github.com/FilippoLentoni/FinanceModel/blob/codex/advisory-policy-agent/docs/llm-benchmarks.md),
and [hosted workflows](https://github.com/FilippoLentoni/FinanceAgent/blob/codex/advisory-policy-agent/docs/horizon-and-recursive-workflows.md).

Raw evidence in the shared workspace: `.worktmp/finplan-horizon-recursive/` contains
`rollout.json`, `cost-isolation-report.json`, `native-integration-results.json`,
`gpu-job-role-readiness.json`, `operator-verify.json`, test-identity cleanup receipt,
and `live-acceptance/` request/result/SSE JSON. Authoritative analysis and activity IDs
in these receipts retrieve the persisted beta records through MCP.
