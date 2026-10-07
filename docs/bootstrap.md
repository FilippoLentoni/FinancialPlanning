# Bootstrap runbook (FinancialPlanning)

Task 10.5 of `add-platform-foundation`. The run itself is task 10.6. Design: P9, P10 and the
Migration Plan. Contracts: D6, D11, D12 and the shared
[bootstrap runbook](../contracts/docs/bootstrap-runbook.md), which this page applies to this
repository. Spec: platform-pipeline, "Pipeline bootstrap prerequisites" (PIPE-07), and
platform-cost-guardrails (COST-01, COST-02, COST-06).

> **Do not run the bootstrap during spec or implementation work.** It runs once, by a human,
> as task 10.6.

## Approval status

- The user approved the one-time bootstrap **in principle on 2026-10-07**.
- It runs only after the bootstrap IaC is implemented and synthesized. The entry point refuses
  to start without a synthesized cloud assembly.
- Before anything is deployed, the entry point prints the **exact stacks** with their resource
  types and a **monthly cost estimate**. The estimate uses prices fetched from the AWS Price List
  API at run time; no price is written in this repository. The operator then confirms by typing
  `deploy`. Anything else stops the bootstrap, and nothing is deployed.

## What the bootstrap deploys

Exactly two account-level stacks (environment `shared`), and never an environment stack:

| Stack | Contents |
|---|---|
| `finplan-shared-financialplanning-pipeline-store` | The pipeline store bucket `finplan-shared-financialplanning-pipeline-store-<account-id>` (SSE-S3, TLS only, Block Public Access, versioned). It holds the pipeline artifacts, the content-addressed CDK file assets (`assets/`), the release ledger (`releases/`) and the staged tooling template (`bootstrap/`). Lifecycle: pipeline artifacts, `assets/` and `bootstrap/` expire after 30 days, the build cache (`cache/`) after 14, noncurrent versions after 7, and incomplete multipart uploads are aborted after 7; `releases/` never expires. The bucket is **retained** when the stack is deleted (see [Teardown](#teardown)) |
| `finplan-shared-financialplanning-tooling` | Permission boundaries (`finplan-<env>-permission-boundary`, `finplan-<env>-research-permission-boundary` for beta, gamma and prod, and `finplan-shared-permission-boundary`); the project budget, its alerts and the SNS topic; the deny policy and the budget action; the budget-state writer Lambda; the pipeline with its scoped roles and CodeBuild projects. The writer and the four CodeBuild projects (build, and one stage project per environment) log to explicit log groups (`/aws/lambda/<function>`, `/aws/codebuild/<project>`) with 30-day retention, deleted with the stack |

Scoped roles created. The account-level roles carry `environment=shared` and
`finplan-shared-permission-boundary`; the per-environment deploy, execution and stage roles carry
`environment=<env>` and `finplan-<env>-permission-boundary` (contracts D13, ENV-21):

| Role | Used by |
|---|---|
| `finplan-shared-financialplanning-pipeline-role` | CodePipeline |
| `finplan-shared-financialplanning-pipeline-build-project-role` | The Build stage |
| `finplan-shared-financialplanning-deploy-role-<env>` | Deploy actions (the role CodePipeline assumes) |
| `finplan-shared-financialplanning-deploy-role-<env>-exec` | CloudFormation execution. It is limited to `finplan-<env>-financialplanning-*` resources, may create only roles that carry `finplan-<env>-permission-boundary`, and writes SSM only under `/finplan/<env>/financialplanning/` |
| `finplan-<env>-financialplanning-operator-pipeline-stage` | Manifest publishing and environment tests. It matches the environment's operator principal, so the plan API admits it |
| `finplan-shared-financialplanning-budget-action-role` | AWS Budgets. It may attach only the deny policy |
| `finplan-shared-financialplanning-budget-state-writer-role` | The budget-state writer. It may write only `/finplan/shared/financialplanning/config/budget-state` |

After the bootstrap, **only these scoped roles deploy**. The identity that ran the bootstrap is not
used again for deployments.

Neither stack needs the CDK bootstrap stack (`CDKToolkit`), whose roles would carry no finplan
permission boundary. The store stack is small enough to deploy inline. The tooling template
exceeds the 51,200-byte inline limit, so the CDK CLI stages it in the store under `bootstrap/`
using the operator's own credentials.

## Human prerequisites

1. **An authenticated AWS CLI session.** The user's existing credentials are acceptable (OQ-11
   resolved). A root caller is **not refused**. The bootstrap prints a recommendation to move the
   human operator to a scoped, MFA-protected role later; this is a recommendation, not a blocker.
2. **Local untracked configuration** at `~/.finplan/bootstrap.json`, outside the repository (the
   bootstrap refuses a file inside the repository tree):

   ```json
   {
     "account_id": "<account-id>",
     "primary_region": "us-east-2",
     "repo": "financialplanning",
     "codeconnection_arn": "<codeconnection-arn>",
     "github_repository": "FilippoLentoni/FinancialPlanning",
     "pipeline_name": "finplan-shared-financialplanning-pipeline",
     "budget_notification_email": "<notification-address>"
   }
   ```

   The connection is one of the existing AVAILABLE GitHub CodeConnections in us-east-2 (OQ-2).
   The notification address is the human-chosen target for the budget alerts. It is passed to
   CloudFormation as the `NotificationEmail` parameter, is never echoed in logs and is never
   committed. AWS sends a confirmation e-mail that the recipient must accept.
3. **Toolchain:** `uv` (Python 3.12) and Node.js for `npx aws-cdk@2`.

## Steps

```sh
# 1. synthesize the cloud assembly (offline)
uv run python scripts/synth.py --out cdk.out

# 2. bootstrap (interactive; this is task 10.6)
AWS_REGION=us-east-2 uv run --with "botocore[crt]" python scripts/bootstrap.py --assembly cdk.out   # botocore[crt] reads `aws login` sessions
```

`scripts/bootstrap.py` drives the contract sequence
`finplan_contracts.bootstrap.run_bootstrap`. It stops at the first failing step, and nothing after
that step runs.

| # | Step | What happens | Stops when |
|---|---|---|---|
| 1 | Assembly | Copies only the two tooling stacks from `cdk.out` into `cdk.out.bootstrap/` | No synthesized assembly exists, or it lacks the tooling stacks |
| 2 | Pre-run plan | Prints the exact stacks with their resource types and the cost estimate (Price List API) | n/a (read-only) |
| 3 | Caller | `sts get-caller-identity`: the account must equal `account_id`. A root caller proceeds and the recommendation is printed | The account differs. Nothing has been created |
| 4 | Region | The session region must equal `primary_region` | The region differs |
| 5 | Connection | The configured CodeConnection must be `AVAILABLE` (read-only) | Any other status |
| 6 | Scoped roles | Every deploy action of the synthesized pipeline must use a scoped deploy role carrying a boundary | A deploy action without a scoped role |
| 7 | Confirmation | The operator types `deploy` | Anything else. Nothing has been deployed |
| 8 | Connection reference | Writes `/finplan/shared/financialplanning/config/codeconnection-ref` | n/a |
| 9 | Budget parameters | Writes `/finplan/shared/financialplanning/config/cost-ceiling-usd` (the default from `config/shared.json`) and the default allocation `/finplan/shared/financialplanning/config/budget-allocation` (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5), each **only when absent**. A user-set value is preserved. An allocation that sums to more than the ceiling stops the bootstrap with a validation error, and paid-work pre-flight checks refuse with `BUDGET_EXCEEDED` until it is fixed | The allocation is invalid |
| 10 | Deploy | `npx aws-cdk@2 deploy --all` of `cdk.out.bootstrap/`, with `NotificationEmail`, `AdditionalEnforcedRoleNames` (every published `/finplan/<env>/<repo>/config/budget-enforced-role-names`) and `SourceDryRunPassed` | A CloudFormation failure (automatic rollback) |
| 11 | Source-stage dry run | The pipeline is created with the inbound transition into Build disabled. One execution is started; when Source fetches `main`, the transition is enabled and the result is recorded in `~/.finplan/financialplanning-source-dry-run.json` | Source cannot fetch the repository. The bootstrap stops with: *extend the GitHub App installation of that connection to the repository, then rerun the dry run*. The deploy stages stay disabled |

After step 11 the waiting execution continues into Build and then Beta. Task 10.6 is verified when
the dry run has fetched `main`, the pipeline exists and its first run reaches beta.

## After the bootstrap

- **Budget scope.** Until a human activates the `project` cost-allocation tag in the Billing
  console, the budget covers the whole account (design P9). After activation, rerun the bootstrap
  by setting `"scope_budget_to_project_tag": true` in `~/.finplan/bootstrap.json` (the script then passes
  `ScopeBudgetToProjectTag=true`). The tag was activated on 2026-10-07 after the first bootstrap.
- **Budget action role names.** The action denies the roles the tooling stack creates, plus every
  role name published at `/finplan/<env>/<repo>/config/budget-enforced-role-names` at the time of
  the bootstrap. The platform publishes its own after each deploy (ingestion and plan-API roles).
  Rerun the bootstrap after other repositories first publish theirs, so the action picks them up.
  The bootstrap/admin identity is never on the list.
- **When the cap is reached,** the action attaches `finplan-budget-enforcement-deny` and the
  budget-state writer sets `/finplan/shared/financialplanning/config/budget-state` to `enforced`.
  On-demand ingestion then returns `BUDGET_EXCEEDED`, while plan reads keep working. **Only a
  human lifts it:** raise the ceiling parameter, detach the deny policy from the roles, and set the
  budget-state parameter to `{"state": "normal"}`. Ingestion and deployments then resume without
  any redeploy. Automation cannot detach the policy, because every permission boundary denies it.
- **Pipeline changes** (the tooling stack itself) are deployed only by rerunning the bootstrap.
  The pipeline never updates itself.

## Teardown

Only when the whole project is retired. Delete the environment stacks first (beta and gamma
through CloudFormation; prod buckets and tables are retained by design), then the two
account-level stacks, newest first. Both carry termination protection. Run these from an
authenticated CLI session; `<account-id>` and `<region>` come from your local untracked
configuration and are never committed.

```bash
for stack in finplan-shared-financialplanning-tooling finplan-shared-financialplanning-pipeline-store; do
  aws cloudformation update-termination-protection --region <region> --stack-name "$stack" --no-enable-termination-protection
  aws cloudformation delete-stack --region <region> --stack-name "$stack"
  aws cloudformation wait stack-delete-complete --region <region> --stack-name "$stack"
done
```

Deleting the tooling stack also deletes its log groups (`RemovalPolicy.DESTROY`). The pipeline
store bucket has `DeletionPolicy: Retain`: deleting its stack leaves the bucket and every object
version in place, because it holds the release ledger (`releases/`). To remove it, first keep
any release records you still need, then delete **every object version and delete marker** (the
bucket is versioned, so `aws s3 rm --recursive` alone leaves noncurrent versions behind) and
finally the bucket:

```bash
BUCKET=finplan-shared-financialplanning-pipeline-store-<account-id>
uv run python -c "import boto3, sys; boto3.resource('s3').Bucket(sys.argv[1]).object_versions.delete()" "$BUCKET"
aws s3api list-object-versions --bucket "$BUCKET" --max-items 1   # expect no Versions and no DeleteMarkers
aws s3api delete-bucket --region <region> --bucket "$BUCKET"
```

If the store is deleted while the tooling stack still exists, the pipeline has nowhere to write
artifacts and the next bootstrap cannot stage the tooling template; rerun the bootstrap, which
recreates the store stack first.

## Tests (no AWS)

`tests/unit/ops/test_bootstrap.py` runs the whole sequence with mocked STS, CodeConnections,
CodePipeline and pricing clients, moto SSM and a fake deploy runner. No test makes an AWS call or
a mutation. The cases are:

- A root caller proceeds and is shown the recommendation.
- The exact stacks and the estimate are printed before the deploy, and only the two tooling stacks
  are deployed.
- The pre-run refuses without an assembly.
- An account mismatch stops before anything is created.
- A declined confirmation deploys nothing.
- A wrong region or an unavailable connection stops the bootstrap.
- A failed dry run stops with the extend-installation message.
- The default allocation is written, a user value is preserved, and a sum of 60 is rejected.

## Never

- Commit the account ID, the connection ARN or the notification address, or paste them into
  issues or docs.
- Run the bootstrap during spec or implementation work, or before `scripts/synth.py` has produced
  the assembly.
- Deploy environment stacks with the bootstrap identity. The pipeline deploys them with the scoped
  deploy roles.
