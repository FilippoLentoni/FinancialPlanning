# Bootstrap runbook

Tasks 10.2 and 14.4 (the run itself is task 10.5). Design: D6, D11, D12 and Migration Plan.
Spec: environment-promotion, "One-time authenticated pipeline bootstrap" (ENV-12) and "Verified
source connection" (ENV-13).

The bootstrap runs **once per repository**. It creates the pipeline tooling, meaning the scoped
pipeline, deploy and service roles and the pipeline itself. After it has run, **only those
scoped roles deploy**, and nothing uses the identity that ran the bootstrap again.

## Approval status

- **The user approved the one-time bootstrap in principle on 2026-10-07 (D12).**
- **It runs only after the bootstrap IaC is implemented and synthesized.** That IaC comes from
  tasks 10.5 and 14.x plus the platform change's bootstrap tasks. The pre-run step refuses to
  continue without a synthesized cloud assembly.
- **At run time the operator first shows the exact stacks and a cost estimate (task 14.4).** The
  estimate comes from current AWS pricing; no price is hard-coded. The bootstrap then runs under
  the in-principle approval, after the operator confirms the shown plan.
- **Nothing is deployed during spec work.**

## Human prerequisites

1. **An authenticated AWS CLI session.** The user's existing credentials are acceptable (D11). No
   pre-existing scoped human role is needed.
2. **Local, untracked configuration** that names the account and the existing CodeConnection to
   reuse. It must live **outside the repository**: the bootstrap refuses a configuration file
   inside the repository tree. Default location: `~/.finplan/bootstrap.json`.

   ```json
   {
     "account_id": "<account-id>",
     "primary_region": "us-east-2",
     "repo": "financialplanning",
     "codeconnection_arn": "<codeconnection-arn>",
     "github_repository": "FilippoLentoni/FinancialPlanning",
     "pipeline_name": "<pipeline-name>"
   }
   ```

   Lookup order: `--config PATH`, then `$FINPLAN_BOOTSTRAP_CONFIG`, then
   `~/.finplan/bootstrap.json`. The environment variables `FINPLAN_ACCOUNT_ID`,
   `FINPLAN_PRIMARY_REGION` and `FINPLAN_CODECONNECTION_ARN` override file values. `.gitignore`
   excludes `.finplan/` and `bootstrap*.json`. Never commit this file or paste its values into
   issues or docs.
3. **A budget notification address** for the alerts at 50%, 80% and 100%. It is supplied at
   deploy time, not committed.
4. The AWS extra for the CLI: `uv sync --extra aws` (boto3; only the read-only checks and the
   bootstrap itself use it).

## Steps

The `finplan-conformance bootstrap-precheck` commands are the read-only parts. The full sequence
is `finplan_contracts.bootstrap.run_bootstrap`, which the bootstrap IaC entry point drives. It
stops at the first failing step, and nothing after that step runs.

| # | Step | Command / behavior | Stops when |
|---|---|---|---|
| 1 | **Pre-run plan** (14.4) | `uv run --extra aws finplan-conformance bootstrap-precheck prerun --assembly cdk.out` prints the exact stacks and their resource types, plus a cost estimate from the AWS pricing API. Types without a pricing rule are listed as "not estimated" | No synthesized assembly (`cdk.out/manifest.json`) exists |
| 2 | **Caller check** | `sts get-caller-identity` account must equal `account_id` in the local configuration. A **root caller proceeds** and the scoped/MFA-role recommendation is printed | The account does not match; the bootstrap stops before creating anything |
| 3 | **Region check** | The session region must equal `primary_region` (`us-east-2`) | The region differs |
| 4 | **Connection check** | The configured existing CodeConnection must be `AVAILABLE` (read-only `get-connection`) | Any other status |
| 5 | **Scoped roles** | Every deploy action in the synthesized pipeline must assume a scoped deploy role declared by the bootstrap stacks, never the caller | A deploy action has no role, or uses the caller |
| 6 | **Approval** | The operator confirms the stacks and estimate shown in step 1 | Not confirmed; nothing is deployed |
| 7 | **Connection reference** | Writes the connection reference to `/finplan/shared/<repo>/config/codeconnection-ref` (a bootstrap-only `shared` key) | n/a |
| 8 | **Deploy bootstrap stacks** | Authenticated CDK deploy of the tooling and pipeline stacks only. This creates the scoped pipeline, deploy and service roles. For FinancialPlanning it also creates the project budget (ceiling from `/finplan/shared/financialplanning/config/cost-ceiling-usd`, USD 50), the default allocation (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5), the deny action at 100% and the contract registry | A CloudFormation failure (automatic rollback) |
| 9 | **Source-stage dry run** | Disables the inbound transition into the stage after Source and starts one execution. When Source fetches `main`, the transition is re-enabled, so the remaining stages are enabled, and the result is recorded | The dry run cannot fetch the repository. The bootstrap stops with: *extend the GitHub App installation of that connection to the repository, then rerun the dry run*. Deploy stages stay disabled |

Steps 2 to 4 can be run on their own at any time; they are read-only:

```sh
cd contracts/python
uv run --extra aws finplan-conformance bootstrap-precheck check
```

After a successful run, the bootstrap prints the root recommendation again when the caller was
root: *move the human operator to a scoped, MFA-protected role later*. This is a recommendation,
not a blocker (OQ-11).

## Order across repositories

1. FinancialPlanning (tooling, budget, contract registry). Contracts 0.x went to beta vendored;
   the registry was added for 1.0.0 (D16), so the FinancialPlanning bootstrap is re-run once and
   its build stage then publishes 1.0.0.
2. FinanceModel, then FinanceLambdasTool, then FinanceAgent, in the integration order. Each pins
   contracts 1.x and runs its own bootstrap with this runbook and its own `repo` value. Each
   bootstrap also:
   - writes `/finplan/shared/<repo>/config/budget-enforced-role-names` with its account-level
     tooling role names (pipeline and build roles; a bootstrap-only `shared` key, 1.0.0). Rerun the
     FinancialPlanning bootstrap afterwards so the budget action picks them up;
   - names its build roles `finplan-shared-<repo>-*` and attaches
     `finplan_contracts.registry.read_policy()` to them, so they can install the pinned contract
     package from the contract registry (`/finplan/shared/financialplanning/contract/registry-ref`).
3. FinanceAgent's bootstrap creates the per-environment Gateway service role and publishes
   `/finplan/<env>/financeagent/agent/gateway-principal-ref`. Until then, FinanceLambdasTool
   deploys with direct-test grants only. Enabling Bedrock model access for Claude Opus 5
   (`us.anthropic.claude-opus-5`) is also a FinanceAgent bootstrap step (D12).
4. FinanceModel's bootstrap writes `/finplan/shared/financemodel/secret-ref/jev-api-key`, which
   holds the secret **name** only. The secret itself already exists, and its value is set by the
   user.

## Tests (no AWS)

ENV-12 and ENV-13 run with mocked STS, CodeConnections, SSM, CodePipeline and pricing clients.
No test makes an AWS call or a mutation:

```sh
cd contracts/python
uv run pytest -q tests/test_bootstrap.py
```

They cover these cases:

- A root caller proceeds and gets the recommendation.
- An account mismatch stops before anything is created.
- Deploy actions use the scoped roles.
- The stacks and estimate are shown before anything runs, and the pre-run refuses without an
  assembly.
- A successful dry run enables the stages.
- A failed dry run stops with the extend-installation message.

## Never

- Commit the account ID, the connection ARN, role or pipeline names, or the notification address.
- Run the bootstrap during spec work, or before the bootstrap IaC is synthesized.
- Adopt or modify the other project's existing Qwen stack.
- Deploy later releases with the bootstrap identity.
