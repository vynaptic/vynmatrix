# Capacity and service boundaries

The supported single-owner runtime uses at most three running containers,
including PostgreSQL. [DEPLOYMENT.md](DEPLOYMENT.md) defines the split and combined
layouts. No hosted capacity, recovery result or broker certification is claimed.

## Capacity changes within the budget

| Trigger | Next step | Boundary |
|---|---|---|
| Worker CPU or memory pressure | Measure selected strategy/feed demand, reduce selection or resize the host; use split application/workers groups | Keep one worker group and explicit `STRATEGY_LIST` |
| Process connection pressure | Sum pools across every API, feed and strategy child; measure before changing limits | Defaults cap each process at five connections |
| A containerized IBKR gateway is required | Use the combined `all` group so the gateway can occupy slot three | Gateway image, authentication and operation require separate review |
| Owner UI is added | Serve its static assets through backend | No additional frontend container |
| Recovery objectives exceed one host | Review database backups, restore time and a separately operated PostgreSQL option | No failover or managed service is supplied |

PostgreSQL persistence, transactional outbox and idempotent consumers remain the
handoff mechanism. Redis, extra brokers, replicas and per-strategy containers are
outside the present topology. A change to that budget requires an explicit new
design and measured need.

## PostgreSQL owner and service boundaries

`vm_backend`, `vm_scoring`, `vm_execution`, `vm_feedback`, `vm_market_data` and
`vm_indicator` remain separate `NOLOGIN`, non-owner, `NOBYPASSRLS` group roles.
Each interpreter uses its corresponding login and reviewed table/command grants.
Sharing a container does not combine database memberships. Administrative and
migration credentials are excluded from runtime groups.

Backend resolves the one explicitly designated deployment owner. It sets the
transaction-local owner scope, and PostgreSQL policies reject foreign-owner
configuration even if a caller forges the former tenant selector. Historical
user/account IDs and ledger attribution remain intact. Commercial organizations,
plans, roles and subscriptions are retired only when empty; existing data blocks
migration pending explicit disposition.

The bootstrap lifecycle stops runtime before its one-shot maintenance stage and
provisions the six exact login memberships after migrations. See
[DATABASE.md](DATABASE.md) for commands and rollback limits. SQLite tests cannot
establish PostgreSQL grants, RLS, locking or restore behavior; those require
isolated PostgreSQL acceptance with the actual roles. Keep paper mode and
`EXECUTION_ENGINE_ALLOW_LIVE=false` throughout the current scope.

## Decision provenance (already landed — the irreversible-now piece)

The `decision_contexts` table (see `CHANGELOG.md`) snapshots the regime / volatility /
model-version / per-factor contributions / stale-input flags / abstain outcome of
**every** scoring decision, atomically with the signal + scores. This is the one
piece that cannot be backfilled, so the implementation records it when a paper decision occurs. Those records provide the foundation for the
hedge-fund-grade explainability and false-positive analysis that the deeper scoring
work below feeds into.

## Decisioning evolution backlog (scoped follow-ons)

Landed: `PriceBasedMarketContextProvider` is the production default. It reads an
explicit source/timeframe from persisted `prices`, excludes future bars during
historical replay, and derives regime plus realized volatility only from fresh,
valid observations. Missing, insufficient, invalid, or stale history fails closed
with no actionable meta output. `ObservedMarketContextProvider` remains available
for explicitly observed unit/backtest contexts; it has no fallback values.
The bounded `DeterministicMetaScorer` is startup-blocked in `EXECUTION_MODE=live`;
live promotion requires a calibrated observed-outcome scorer.
Remaining items change scoring behaviour and so each warrants a **focused,
baseline-validated** change (run the e2e before/after), not a bundled session-tail
edit:

- **Feedback → strategy-weight loop:** persist a per-strategy performance multiplier
  from `FeedbackLoopEngine` and have scoring load it into `StrategyWeightManager`
  (`update_performance_multiplier` exists but has no caller today — the loop is not
  closed end-to-end).
- **Abstain / no-trade gates** for false-positive reduction: confidence-floor,
  source-health, cross-strategy conflict/quorum, regime filters, cooldowns —
  recorded in the `decision_contexts.abstained`/`abstain_reason` columns already
  provisioned. Soft gates in scoring; execution keeps the hard fail-closed risk
  limits.
- **Calibrated meta-scorer:** replace `DeterministicMetaScorer` (an explicitly
  uncalibrated, bounded paper-validation rule set) with a trained, calibrated model
  via the existing `register_scorer` seam, fed by real paper/backtest outcomes
  (no fabricated models).
- **Greeks / slippage / portfolio context:** these have no slot today — adding them
  is a real schema change (domain types + persistence + event contracts), needed for
  the options + market-making paths.
