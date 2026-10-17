# Horizon evaluation and recursive research (contracts 1.6.0)

`evaluate_portfolio_decision` retains its existing request. Its optional `horizon_evaluation`
separates daily accounting from a strategy evaluation contract, window maturity, preserved-policy
replay and cost-consistent controls. A frozen first-allocation hold is not a sequential policy replay.
Legacy decisions without an issued evaluation contract are labeled retrospective. The producer
retains full replay trajectories in checksum-bound immutable evidence.

`run_recursive_improvement` starts or resumes a persisted research cycle. The default `dry_run`
reviews evidence and estimates an experiment without launching compute. `cycle_id` is a `ca_`
analysis identifier; the response has its own immutable analysis identity and lineage. A paid
request requires `dry_run: false`, `confirmed_by_user: true`, and an idempotency key, in addition
to the producer's verified caller, budget and concurrency checks. At most three iterations are
allowed. `get_classical_analysis` retrieves cycle or iteration evidence; list operations accept
`recursive_cycle` and `recursive_iteration` kinds.

The cycle returns an explicit state and stopping reason. Results remain proposals; this operation
does not accept portfolio trades, activate a serving strategy or bypass experiment approval.
Qwen GPU compute requires its own approved cost estimate. TypeSafe Jev consumption is separately
metered vendor usage. Benchmark availability must reflect actual configured model readiness.

The shared contracts do not assert that a short-term loss proves a modeling defect, that recent
news caused a price move, or that repeated inspection of a test period preserves its holdout status.

Verification: regenerate fixtures with `python contracts/python/scripts/gen_fixtures.py`, run the
Python contract suite and `npm test` in `contracts/typescript`, then verify each consumer's exact
wheel pin with `python scripts/check_contracts_pin.py --env beta`.
