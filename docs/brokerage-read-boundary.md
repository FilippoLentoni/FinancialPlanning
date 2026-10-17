# Beta brokerage read boundary

The deployed beta environment boundary overlays the conservative, immutable
contracts 1.9 boundary because its financial-secret denial also blocks the
separately authenticated Coinbase connection check.

The exception permits only `secretsmanager:GetSecretValue` for principal
`finplan-beta-financeagent-brokerage-read-role` and secret
`finplan/beta/financeagent/coinbase-brokerage-readonly` in the same AWS account
and region. The resource ARN requires the six-character AWS-generated suffix.
The request identifier must match that exact name or ARN. Other principals,
secrets, secret mutations, batch reads by this role, and financial actions remain
blocked. Other roles retain their existing nonfinancial secret-read behavior.
This does not grant trading permission.

`infra/stacks/brokerage_boundary.py` implements the deployment overlay; gamma,
production, shared, and research boundaries retain their canonical documents.
Optional statement identifiers are removed only in beta to keep the resolved
managed policy at 5,877 characters, below IAM's 6,144-character limit. Regression
tests simulate a permissive identity policy to verify that the boundary itself
maintains these restrictions. The canonical package and wheel are unchanged.

The deployment build gate recognizes the entire exact beta policy document and
managed-policy name before auditing four DENY-only secret ARN reference leaves
that the canonical generic scanner reports. All Allow findings and all other
references remain scanned. Any change to the role, secret, statements, actions,
or boundary name invalidates this recognition; regression tests cover those
mutations and an extra resource. Canonical ENV-05 logic remains unchanged.
