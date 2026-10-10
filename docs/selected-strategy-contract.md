# Selected strategy contract, 1.3.0

The MCP tool now uses the additive `finance/v1/tools/recommend-portfolio-invocation-request`.
An empty request `{}` loads the saved default paper portfolio and latest approved market snapshot.
An optional `portfolio_id` selects another saved paper book. Snapshot/date overrides must appear
together, and saved portfolio selection cannot be mixed with supplied holdings. This mode requires
producer and consumer contracts 1.3.0; the original explicit request below remains unchanged.
Saved-mode responses include paper-state provenance and proposed fractional share quantities with
reference prices and dates. Recommendations never update the saved book or execute trades.

The strategy-neutral `finance/v1/tools/recommend-portfolio-request` requires an approved
`input_snapshot_id`, completed-session `as_of`, and `holdings` with `weights`, `cash_weight`,
`portfolio_value` and `high_watermark`. Instruments must be distinct and supported; weights plus
cash must sum to one. Numeric validity, data timing and frozen-universe checks also run in the
producer because JSON Schema cannot express every cross-field invariant.

The response requires full target weights, cash, buy/sell/hold deltas and indicative notionals,
turnover and constraint outcome. Provenance includes source experiment, export run, configuration,
approved snapshot and snapshot/artifact checksums. The current mode is `advisory_paper`, and the
forecast status is explicitly `not_available`. An inference result is not an execution record.

The experiment configuration adds optional `policy_source_run_id` and `policy_strategy_id` for
exporting an evaluated configuration without retraining. Supported strategy selectors are PPO,
SAC, cash, buy-and-hold, equal weight, min-variance, mean-variance and scenario-CVaR. The default
selector is PPO for the original policy-export caller. These optional additions preserve existing
1.1 configuration payloads. New tool consumers require a producer serving contracts 1.2.0.

Valid and invalid fixtures are registered in `contracts/conformance/cases.yaml`; producer Python
and TypeScript conformance, compatibility and reproducible wheel checks run before release.
Consumers pin the exact wheel and SHA-256 in `contracts-pin.json`. Release the producer into beta
before releasing beta consumers. No cross-environment reference is implied by a shared wheel.
