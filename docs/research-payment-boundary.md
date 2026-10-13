# Isolated research-payment boundary

The `add-agentcore-research-payments` change is the separately authorized payment
subsystem anticipated by the original cross-repository financial-action separation.
The existing environment, research and shared boundaries are unchanged, as are all
four paper-planning pipelines' financial permissions.

`infra/research_payments.py` renders a dedicated beta-only permission boundary.
`scripts/research_payment_boundary.py` deploys it in an independent CloudFormation
stack through the operator identity, outside the four application release pipelines.
Default invocation renders a template; `--apply` explicitly deploys that owned stack.
Use a private output path outside the repositories for all account-specific state.

The boundary permits reviewed session/instrument/payment actions only on the owned
beta manager. It permits the service identity's credential operations; exact secret
access is supplied by AgentCore's per-connector role grants, not by the boundary
alone. Management and execution roles contain no direct secret grants. The boundary
does not allow IAM mutation, role passing, manager creation or retail trading.

Create-time workload-identity authorization requires a namespace grant because the
new ARN is not available yet. The exception is constrained to the same account and
region and fixed project/repository/beta request tags. The service role that uses
it trusts only AgentCore for the owned manager.

Offline permission tests and live IAM simulation verify that management cannot
process payments, execution cannot create sessions, and the deployed planning
runtime retains its explicit payment denial. No original policy is broadened.

The current setup checks USD 5 headroom within the authorized USD 100 project
ceiling. No wallet funds have been spent. AWS billing is delayed; external merchant
spend must remain explicit in future accounting. FinanceAgent's operator adapter
accepts only bounded Base Sepolia test USDC challenges, with a USD 1 session limit
and at most ten signing attempts per research-round ledger.

The dedicated boundary and role stacks are deployed, and the manager is READY.
Coinbase connector creation returned SubscriptionRequiredException; Marketplace
subscription, user consent, wallet activation and authenticated testnet verification
remain pending. See FinanceAgent's `docs/research-payments.md` for the continuation.
