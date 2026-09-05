# Scaling & hardening roadmap (Phase 3+)

Deferred architectural options for vynmatrix, starting from a measured local
paper workload. No cloud footprint, revenue state, capacity result, or deployment
is claimed by this migration. Each expansion requires concrete workload and
recovery evidence plus a separate owner decision.

> Optimise for tomorrow, not today: do the things that are **cheap now and
> irreversible/compounding if deferred** (decision provenance — already landed;
> tenant isolation seam; durability) before the things that are merely a
> **reversible cost-timing choice** (bigger box, managed services, more replicas).

## Scale ladder (when → what)

| Trigger | Action | Why it waits |
|---|---|---|
| **Recovery objectives or real money exceed the self-hosted design** | Select a managed or separately operated PostgreSQL topology on the chosen cloud; verify service-role/RLS controls, PITR, failover, and restore drills before cutover. | No cloud database or verified backup is supplied. Measure recovery objectives and test restore before choosing a topology. |
| **One box saturates** (~25 strategies, or multi-tenant load) | Shard `indicator-runner` onto its own node (it is the highest-envelope, independently-shardable workload — split `STRATEGY_LIST`) or select another reviewed container platform. | Benchmark the actual workload before sizing a node; any hosted topology still requires deployment-specific control-plane/RLS work. |
| **Throughput outgrows Postgres NOTIFY** | Introduce a real broker (NATS/Kafka/Redis Streams) for the fan-out legs. | The transactional outbox + `LISTEN/NOTIFY` already gives durable, idempotent, dead-lettered async handoff at current scale; a broker would only duplicate those guarantees and add a stateful service to an oversubscribed box. See `CLAUDE.md` → Event-Driven Data Plane. |
| **Market-making is funded** | A **separate, co-located, low-latency execution + tick data-plane** — NOT a retrofit of these uvicorn services. | The DB-outbox + HTTP-relay + 60s-poll ingest design is correct for swing/position signals but structurally wrong for sub-millisecond MM order paths. Keep the `ExecutionCommandEvent` contract clean (it already is) so an MM engine can consume the same command stream as a parallel, independently-scaled path. |

## Multi-tenant hardening: PostgreSQL RLS + service roles (H-8)

Migration `0052_service_role_rls` defines the runtime authorization boundary:

- `vm_backend`, `vm_scoring`, `vm_execution`, `vm_feedback`, `vm_market_data`,
  and `vm_indicator` are `NOLOGIN`, `NOBYPASSRLS`, non-owner group roles.
- Every service authenticates through a distinct `*_login` role inheriting
  exactly one group. Combining memberships is prohibited because PostgreSQL
  grants and permissive RLS policies union.
- Alembic alone uses the owner credential. Runtime containers never receive it.
- Privileges are command- and table-specific, with no default grants for future
  objects. Market data exclusively writes prices; indicator exclusively writes
  watermarks; scoring cannot mutate tenant configuration; execution cannot
  create broker accounts; feedback is suggestion-only and cannot write prices.
- Backend transactions set the transaction-local `app.current_tenant` GUC.
  Backend RLS policies scope direct and account-owned rows, including
  `managed_secrets` and `mode_performance`. Cross-tenant pipeline services use
  their narrowly scoped service role rather than a mutable bypass GUC.

The local stack provisions the six LOGIN roles after migration and starts each
service with its own URL. Any future deployment must provision the same logins and verify
exact membership before switching containers. A PostgreSQL integration gate
must prove allowed operations and denied cross-tenant/service operations with
the real runtime credentials; SQLite tests cannot validate RLS.

Keep `EXECUTION_ENGINE_ALLOW_LIVE=false` until that PostgreSQL role test, the
paper end-to-end pipeline, backup restore, and broker-specific certification all
pass in the target environment.

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
