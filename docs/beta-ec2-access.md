# Beta access from the EC2 development desktop

The EC2 instance role already has AdministratorAccess. Explicit resource-policy denies still override that identity policy. Beta now configures `dev_desktop_access.role_names` with the exact IAM role name `finplan-devdesktop`; account and partition values come from CloudFormation tokens.

FinancialPlanning admits this role in metadata table policies, all six bucket policies (including upload reads), the plan API resource policy, and handler operator authorization. FinanceModel uses the same beta configuration for its job-control table, research and registry buckets, job API, and operator authorization. Empty or absent configuration grants no exception. Gamma and prod reject a nonempty operator exception.

TLS, KMS requirements, write-once artifacts, immutable registry records, append-only audit/portfolio history/activity, environment isolation, compute budget controls, explicit strategy-change confirmation, and submitter/approver separation remain enforced. The desktop may approve a different principal's beta compute request; it may not approve its own request. The exception does not itself grant identity permissions.

FinanceLambdasTool has no matching resource-policy deny and can be dry-run invoked by the desktop. FinanceAgent uses Cognito JWT authentication for its runtime and MCP gateways; use its `docs/beta-access.md` onboarding flow for those interfaces. Administrator IAM access permits infrastructure inspection but does not issue a Cognito user access token.

Codex on this EC2 is configured for `approval_policy = "never"` and `sandbox_mode = "danger-full-access"` in the user's existing configuration. These local execution settings do not override AWS denies or application authorization. See the [official sandbox and approval documentation](https://learn.chatgpt.com/docs/security).

The one-minute snapshot scheduler and thirty-minute research scheduler are a later step. No new recurring agent job is enabled by this access change.


The exact existing `finplan-beta-financemodel-job-execution-role` may additionally
read `GET /v1/portfolios/{portfolio_id}` and `/state` for isolated synthetic
experiment books. The handler checks original portfolio metadata: synthetic true,
isolated_experiment true, named experiment/approach ownership, the deterministic
internal experiment portfolio ID, and initialized authoritative paper state/revision.
User-editable settings alone cannot grant this access. Ordinary user
portfolios, other managed-role names, descendant history/decision routes, and all
writes remain forbidden. The worker independently validates experiment/approach,
revision, cash and positions before an approved managed Jev invocation. This grants
no direct DynamoDB access, approval authority, recurring vendor authorization or
Gamma/prod exception. The Model repository owns the matching beta-only identity
policy; both layers must be deployed before a real approved worker can use it.

The beta API resource policy factors identical tool read lists into one union deny
and per-tool forbidden-write denies, preserving all existing effective tool
permissions. Its deployed-token size remains below8192bytes with the original
512byte reserved headroom test. Higher-environment policy documents retain their
pre-change contents.
