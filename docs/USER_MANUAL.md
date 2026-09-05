# Architecture and source guide

This is the architectural map for vynmatrix. It explains how the runtime is
assembled and where the canonical contracts live. Use the documents in
[README.md](../README.md#documentation) for setup, commands, configuration,
database operation, deployment, and paper evidence; this guide deliberately
does not duplicate those procedures.

## Runtime at a glance

~~~mermaid
flowchart LR
    S[Signal-only strategy] --> I[Indicator runner]
    I --> N[Canonical Signal]
    N --> SC[Scoring]
    SC --> DB[(PostgreSQL)]
    DB --> O[Transactional outbox]
    O --> EX[Execution]
    EX --> L[Canonical order and fill ledger]
    L --> FB[Feedback]
~~~

PostgreSQL owns durable handoffs and state. The scoring-to-execution hop is an
outbox row persisted with the scoring decision, then relayed by scoring over
HTTP; it is not an in-memory queue. Consumers are at-least-once and use stable
idempotency identities.

The deployed system has one explicitly designated owner, while historical user
and account IDs remain on every authoritative record. A decision needs an exact
owner, broker account, environment, instrument, currency/FX path, applicable
market session, and risk authority. Missing, stale, mismatched, or ambiguous
authority fails closed. `positions` and execution metrics are projections; the
canonical execution ledger is the restart-accounting source.

## Component boundaries

| Boundary | Owns | Primary source |
| --- | --- | --- |
| Strategy domain | `Signal`, signal actions, normalization, strategy ports, option-spread calculations | `libs/python/lib_strategy/lib_strategy/` |
| Shared runtime | configuration parsing, logging, health, internal events | `libs/python/lib_common/lib_common/` |
| Application | ORM models, database services, outbox, owner/account/catalogue policy | `libs/python/lib_application/lib_application/` |
| Infrastructure | broker and market-data adapters | `libs/python/lib_infrastructure/lib_infrastructure/` |
| Indicator runner | durable state, source watermarks, signal-only strategy evaluation | `apps/indicator_runner/` |
| Scoring | normalization, policy evaluation, decision persistence, outbox relay | `apps/scoring_engine/` |
| Execution | authority/risk gates, broker submission, reconciliation, ledger and paper lifecycle | `apps/execution_engine/` |
| Feedback | outcome evaluation and auditable suggestions | `apps/feedback_loop_engine/` |
| Market data | price, observed FX, and official-session writers | `apps/market_data_ingestor/` |
| Backend | single-owner configuration API | `apps/backend/` |
| Process composition | supervised application and worker children | `scripts/run_platform.py` and `scripts/platform_processes.py` |

`lib_strategy` does not import application code or broker adapters.
`lib_application` owns persistence and depends on strategy contracts;
infrastructure implements those contracts; applications coordinate the runtime.

## Canonical symbol index

Search these before creating another type or service with a similar name.

| Need | Canonical owner |
| --- | --- |
| Signal contract | `lib_strategy.signals.signal.Signal` and `SignalAction` |
| Action conversion | `lib_strategy.signals.normalization.normalize_signal_action` and `normalize_scoring_action` |
| Indicator strategy base | `lib_strategy.signals.PureSignalStrategy` |
| Signal emission | `emit_long()`, `emit_short()`, and `emit_close()` on the strategy base |
| Option-spread calculation | `lib_strategy.spreads.option_spreads` with a broker-observed multiplier |
| Database sessions | `lib_application.db.session.get_session_factory()` |
| Transactional outbox | `lib_application.outbox.OutboxStore` |
| Runtime order intent | `execution_engine.models.OrderIntent` |
| Broker-wire order intent | `lib_strategy.types.BrokerOrderIntent` |
| Persisted order intent | `lib_application.db.models.oms.OrderIntent` |
| Asset-class vocabulary | `lib_common.asset_classes` constants |

The three `OrderIntent` types are intentional boundary-specific representations.
Keep their explicit conversions. `SignalAction.CLOSE` persists as `flat`; use
the normalizers rather than lowercasing an enum value. The execution threshold
for a binding is magnitude-based: `abs(score) >= threshold`, with direction
taken from the score sign.

## Strategy contract

An indicator strategy has a `core.py` subclass of `PureSignalStrategy` and a
`config.json` whose `runner_kind` is `signal_worker`. Its code may emit signals
with the source bar timestamp, but it may not call a broker order API. The
deterministic signal identity depends on that timestamp.

Source shipping in `vynmatrix/platform` is separate from authorization. Runtime
selection (`STRATEGY_LIST`), database strategy/version state, designated owner,
broker-account binding, instrument routing, session coverage, data entitlement,
and execution authority all remain independent gates. Current strategy status
and the narrow Swing development-canary restriction are in
[STRATEGY_READINESS.md](STRATEGY_READINESS.md).

## Reproducible strategy validation environment

Build the declared validation environment before a recorded-data campaign:

~~~text
vmdev build venvs
~~~

On macOS/Linux, activate
`build/venvs/strategy-validation/bin/activate`; on Windows, use the equivalent
environment under `build\\venvs\\strategy-validation`. The `vmdev strategy`
commands freeze and attest bounded historical campaigns. They are separate from
`vmdev test`, which has only `all`, `lib`, and `team` subcommands.

Deterministic fixtures are suitable for unit boundaries. Backtests, soaks, and
end-to-end claims require recorded real historical data and their exact source,
window, configuration, and result identity. Strategy validation does not grant
broker, paper-promotion, deployment, or live authority.

## Change impact

Trace every contract change through the actual producer and consumer:

1. Strategy signal producer and canonical `Signal` contract.
2. Normalization, scoring persistence, and outbox event payload.
3. Execution idempotency, authority/risk checks, order and fill ledger.
4. Position/NAV/feedback projections and their account/currency provenance.
5. ORM models, Alembic migration, downstream tests, configuration, and the
   document that owns the changed operator contract.

Use [DATABASE.md](DATABASE.md) for schema and migration rules,
[CONFIGURATION.md](CONFIGURATION.md) for environment ownership, and
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) for evidence. Do not
introduce a compatibility alias or parallel path without an identified consumer
and explicit approval.

## Where to go next

| Question | Document |
| --- | --- |
| Which commands are supported? | [QUICK_REFERENCE.md](QUICK_REFERENCE.md) |
| How is runtime configuration resolved? | [CONFIGURATION.md](CONFIGURATION.md) |
| How do I bootstrap, migrate, or back up PostgreSQL? | [DATABASE.md](DATABASE.md) |
| Which containers and processes run? | [DEPLOYMENT.md](DEPLOYMENT.md) |
| How do I set up an account or credentials? | [BROKER_CREDENTIALS.md](BROKER_CREDENTIALS.md) |
| How do I prove the paper pipeline? | [E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) |
| How do I handle a runtime incident? | [RUNBOOK.md](RUNBOOK.md) |
