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
| `finplan-shared-financialplanning-tooling` | Permission boundaries (`finplan-<env>-permission-boundary`, `finplan-<env>-research-permission-boundary` for beta, gamma and prod, and `finplan-shared-permission-boundary`); the project budget, its alerts and the SNS topic; the deny policy and the budget action; the budget-state writer Lambda; the contract registry (CodeArtifact domain `finplan`, repository `contracts`, both retained on stack deletion, and `/finplan/shared/financialplanning/contract/registry-ref`); the pipeline with its scoped roles and CodeBuild projects. The writer and the four CodeBuild projects (build, and one stage project per environment) log to explicit log groups (`/aws/lambda/<function>`, `/aws/codebuild/<project>`) with 30-day retention, deleted with the stack |

Scoped roles created. The account-level roles carry `environment=shared` and
`finplan-shared-permission-boundary`; the per-environment deploy, execution and stage roles carry
`environment=<env>` and `finplan-<env>-permission-boundary` (contracts D13, ENV-21):

| Role | Used by |
|---|---|
| `finplan-shared-financialplanning-pipeline-role` | CodePipeline |
| `finplan-shared-financialplanning-pipeline-build-project-role` | The Build stage, including the contract publish step (publish to the `contracts` repository only; no delete) |
| `finplan-shared-financialplanning-deploy-role-<env>` | Deploy actions (the role CodePipeline assumes) |
| `finplan-shared-financialplanning-deploy-role-<env>-exec` | CloudFormation execution. It is limited to `finplan-<env>-financialplanning-*` resources, may create only roles that carry `finplan-<env>-permission-boundary`, and writes SSM only under `/finplan/<env>/financialplanning/` |
| `finplan-<env>-financialplanning-operator-pipeline-stage` | Manifest publishing and environment tests. It matches the environment's operator principal, so the plan API admits it |
| `finplan-shared-financialplanning-budget-action-role` | AWS Budgets. It may attach and detach only the deny policy; its boundary lets it detach that policy so a reset or reversal works (contracts 0.2.2) |
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
  role name published at the time of the bootstrap, for each repository: the account-level list
  `/finplan/shared/<repo>/config/budget-enforced-role-names` (its pipeline and build roles, written
  by that repository's bootstrap; contracts 1.0.0) and the beta, gamma and prod lists
  `/finplan/<env>/<repo>/config/budget-enforced-role-names` (written by its pipeline). The platform
  publishes its environment lists after each deploy (ingestion and plan-API roles); its own tooling
  roles are in the stack's list already. Rerun the bootstrap after other repositories first publish
  theirs, so the action picks them up. The bootstrap/admin identity is never on the list.
- **When the cap is reached,** the action attaches `finplan-budget-enforcement-deny` and the
  budget-state writer sets `/finplan/shared/financialplanning/config/budget-state` to `enforced`.
  On-demand ingestion then returns `BUDGET_EXCEEDED`, while plan reads keep working.

  **Lifting the cap is a human decision.** First raise the ceiling parameter (the budget limit
  picks it up at the next bootstrap), otherwise the action fires again. Then remove the deny in one
  of two ways, from your own authenticated CLI session:

  1. Reverse the action, which lets AWS Budgets detach the policy from every role it applied it to
     (the action ID comes from `describe-budget-actions-for-budget`):

     ```bash
     aws budgets describe-budget-actions-for-budget --account-id <account-id> \
       --budget-name finplan-shared-financialplanning-project-budget
     aws budgets execute-budget-action --account-id <account-id> \
       --budget-name finplan-shared-financialplanning-project-budget \
       --action-id <action-id> --execution-type REVERSE_BUDGET_ACTION
     ```

  2. Or detach the policy by hand from each role that holds it
     (`aws iam list-entities-for-policy --policy-arn arn:aws:iam::<account-id>:policy/finplan-budget-enforcement-deny`,
     then `aws iam detach-role-policy` for each role).

  Finally set the budget-state parameter to `{"state": "normal"}`. Ingestion and deployments then
  resume without any redeploy.

  **Automation still cannot lift the cap.** Every permission boundary denies detaching the deny
  policy and executing a budget action. The single exemption is the action's own execution role
  (`finplan-shared-*-budget-action-role`, shared boundary only, contracts 0.2.2), so that AWS
  Budgets can carry out a reset or a reversal; that role may attach and detach the deny policy and
  nothing else, and no other principal under the shared boundary may create, re-trust,
  re-permission or pass a role with that name.

  **Incident, 2026-10-07.** The action fired and applied the deny policy to 11 pipeline roles. Its
  reset then failed (`RESET_FAILURE`, "explicit deny in a permissions boundary"): the shared
  boundary, which also bounds the action's execution role, denied that role the detach. Contracts
  0.2.2 adds the exemption above, and the action's logical ID changed to
  `BudgetEnforcementActionV2`. **Expect the next bootstrap to replace the action:** CloudFormation
  creates a fresh action in `STANDBY` and then deletes the old one. Deleting the old action does
  not detach the policy, so remove it from the roles by hand (option 2 above) and reset the
  budget-state parameter. If the old action cannot be deleted during the stack's cleanup phase,
  the update still completes; delete it with `aws budgets delete-budget-action`. If actual spend
  is still above the (raised) ceiling, the fresh action fires again by design.
- **Pipeline changes** (the tooling stack itself) are deployed only by rerunning the bootstrap.
  The pipeline never updates itself.

### Re-run for the contract registry (contracts 1.0.0, 2026-10-08)

Contracts 1.0.0 adds the contract registry and its publish step to the tooling stack (design P10;
contracts D16, task 7.3; platform task 15.6). The running pipeline keeps its old build spec until a
human re-runs the bootstrap with the usual two steps (synthesize, then `scripts/bootstrap.py`). The
change set of `finplan-shared-financialplanning-tooling` is, compared with the deployed template:

| Change | Logical ID | Type | What |
|---|---|---|---|
| Add | `ContractRegistryDomain` | `AWS::CodeArtifact::Domain` | Domain `finplan` (AWS-managed encryption key), resource policy: `GetAuthorizationToken` for `finplan-shared-<repo>-*` roles of the other three repositories, this account only. `DeletionPolicy: Retain` |
| Add | `ContractRegistry` | `AWS::CodeArtifact::Repository` | Repository `contracts`, no upstream or external connection, resource policy: `GetRepositoryEndpoint` and `ReadFromRepository` for the same roles. `DeletionPolicy: Retain` |
| Add | `ContractRegistryRef...` | `AWS::SSM::Parameter` | `/finplan/shared/financialplanning/contract/registry-ref` (JSON: domain, repository, region, formats) |
| Modify | `BuildProject...` | `AWS::CodeBuild::Project` | Build spec only: runs `scripts/publish_contracts.py` after `scripts/build_stage.py` |
| Modify | `BuildRoleDefaultPolicy...` | `AWS::IAM::Policy` | Build role gains the registry grant: token for the domain, `sts:GetServiceBearerToken` for CodeArtifact, read and publish in `contracts` and its packages, read of the reference |

Nothing is replaced or removed; no parameter changes; the store stack is unchanged. The cost
estimate lists the repository (storage and requests; the packages are a few hundred KB); the domain
has no standing charge. After the re-run, the next pipeline build publishes contracts 1.0.0 (wheel
and npm package) and logs `contract registry: published ...`; later builds log `already published`
until `contracts/VERSION` changes. A build fails if a published version would change.

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

Deleting the tooling stack also deletes its log groups (`RemovalPolicy.DESTROY`). The contract
registry domain and repository are **retained** (published contract versions are the release
record of every consumer pin): delete them by hand only when no repository pins a contract
version any more (`aws codeartifact delete-repository --domain finplan --repository contracts`,
then `aws codeartifact delete-domain --domain finplan`), and delete the
`/finplan/shared/financialplanning/contract/registry-ref` parameter if it remains. The pipeline
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
