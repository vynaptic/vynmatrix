# Event-driven signal delivery and bounded strategy connections

**Status:** Approved design; implementation has not started. **Date:** 2026-09-05.
**Stage:** Spec A of two. Spec B (execution pulling its own commands from the outbox)
is written only after this spec is deployed and measured.

## 1. Goal and non-goals

Lower signal-to-order latency and remove failure surfaces for a deployment running
about twenty concurrent strategies, without changing the three-container topology,
the process-per-strategy model, any authority gate, or the paper-only defaults.

Four changes, all inside existing processes:

1. Strategy subprocesses get a small fixed database pool.
2. The scoring outbox relay wakes on the existing `outbox_events` notification.
3. The signal worker delivers to scoring through a dedicated loop, never under its
   processing lock.
4. The four outbox topics that have no consumer are retired together with their
   producers.

Non-goals: changing who delivers execution commands, widening or narrowing any
PostgreSQL grant or row-level policy, merging interpreters, adding a connection
pooler, raising `max_connections` by default, or touching scoring, risk, freshness
or authority logic.

## 2. Verified starting point

Read from the working tree on 2026-09-05; code and Compose files are authoritative.

| Fact | Evidence |
| --- | --- |
| Each selected strategy runs as one `indicator_runner.signal_worker` grandchild spawned with a copy of the indicator child's environment, so `DB_POOL_*` values set on the indicator spec reach every worker | [process_manager.py](../../../apps/indicator_runner/indicator_runner/process_manager.py) `_build_signal_worker_command`, `_get_process_env`; [platform_processes.py](../../../scripts/platform_processes.py) `_COMMON` |
| A worker's pool is used by one processing thread; the LISTEN thread holds its own raw psycopg2 connection | [signal_worker.py](../../../apps/indicator_runner/indicator_runner/signal_worker.py) `_processing_lock`, `PgNotifyListener` construction |
| The worker main loop calls `relay_once()` every second while idle and runs `catchup_all()` on a separate floor | [signal_worker.py](../../../apps/indicator_runner/indicator_runner/signal_worker.py) main loop, `stop_event.wait(1.0)` |
| The bar path and the panel path both call the relay while holding `_processing_lock`, so scoring latency stalls bar processing | [signal_worker.py](../../../apps/indicator_runner/indicator_runner/signal_worker.py) `_catchup_symbol_locked`; [panel_runtime.py](../../../apps/indicator_runner/indicator_runner/panel_runtime.py) `_process_prepared` |
| Ordered claiming returns only the oldest undelivered row per `ordering_key`, so one `run_once` delivers at most one signal per partition | [outbox.py](../../../libs/python/lib_application/lib_application/outbox.py) `claim_batch(preserve_ordering=True)`; [runtime_journal.py](../../../apps/indicator_runner/indicator_runner/runtime_journal.py) `DurableSignalRelay.run_once` |
| Migration 0021 installs a trigger that emits `pg_notify('outbox_events', ...)` for every pending row, and the relay already implements `_start_notify_listener`, but neither relay constructor passes `notify_dsn` | [0021_outbox_events_notify.py](../../../scripts/db/alembic/versions/0021_outbox_events_notify.py); [outbox_relay.py](../../../apps/scoring_engine/scoring_engine/outbox_relay.py); [main.py](../../../apps/scoring_engine/scoring_engine/main.py) `_start_inline_relay` and the `relay` command |
| The relay drains immediately while work exists and waits at most `poll_interval_seconds` (default 2.0) when idle; the notify event would shorten that wait | [outbox_relay.py](../../../apps/scoring_engine/scoring_engine/outbox_relay.py) `run_forever` |
| Readiness considers only `execution.commands` and `execution.rebalance.commands` | [main.py](../../../apps/scoring_engine/scoring_engine/main.py) `_REQUIRED_OUTBOX_TOPICS` |
| `signals.ingested`, `signals.scored`, `execution.results` and `feedback.ready` are enqueued unconditionally, claimed by the relay, and handed to `NoOpEventPublisher`; nothing consumes them | [pipeline_events.py](../../../apps/scoring_engine/scoring_engine/pipeline_events.py); [execution_persistence.py](../../../apps/execution_engine/execution_engine/execution_persistence.py); [paper_order_lifecycle.py](../../../apps/execution_engine/execution_engine/paper_order_lifecycle.py); [engine.py](../../../apps/feedback_loop_engine/feedback_loop_engine/engine.py); [main.py](../../../apps/scoring_engine/scoring_engine/main.py) `_build_event_publisher` |
| The command-to-result `causation_id` is carried only in the `execution.results` payload | [execution_persistence.py](../../../apps/execution_engine/execution_engine/execution_persistence.py) `ExecutionResultEvent(causation_id=...)` |
| Measured on the live stack with one strategy: a worker uses about 105 MB proportional memory, of which about 100 MB is private; steady-state connections were 17 of a 100 limit | `docker stats`, `/proc/<pid>/smaps_rollup`, `pg_stat_activity` on 2026-09-05 |

## 3. Strategy connection budget

Set on the indicator child spec in [platform_processes.py](../../../scripts/platform_processes.py),
so the indicator supervisor and every strategy grandchild inherit it:

| Variable | Value |
| --- | --- |
| `DB_POOL_SIZE` | 2 |
| `DB_MAX_OVERFLOW` | 0 |
| `DB_POOL_CONNECTION_BUDGET` | 2 |

Two pooled connections are sufficient because the only pool users in a worker are
the processing thread and the delivery loop introduced in section 5; the LISTEN
thread does not use the pool. Pool exhaustion still fails closed through the existing
`pool_timeout`. No other child changes its pool. The values are fixed in the launcher
and documented; there is no per-strategy override.

Configured allowance for twenty strategies against PostgreSQL's default
`max_connections` of 100 with 3 reserved:

| Consumer | Allowance |
| --- | ---: |
| 20 strategy workers, 2 pooled + 1 LISTEN each | 60 |
| indicator supervisor child | 2 |
| backend, scoring, execution, feedback, market-data, fx at 5 each | 30 |
| scoring outbox LISTEN (section 4) | 1 |
| total | 93 |

Each additional strategy adds 3, each optional equity or calendar worker adds 5.
CONFIGURATION.md documents this formula and states that exceeding it requires raising
`max_connections` through the PostgreSQL service `command` in
`docker/docker-compose.stack.yml`. This spec adds no automatic checker; the existing
`vm_database_pool_*` metrics remain the runtime signal.

## 4. Scoring relay wake-up

Both relay constructors in [main.py](../../../apps/scoring_engine/scoring_engine/main.py)
pass `notify_dsn` when `SCORING_OUTBOX_NOTIFY_ENABLED` is true (the default). The DSN is
the scoring child's own `SCORING_DATABASE_URL`; the listener therefore runs as
`vm_scoring_login`, which needs no new privilege to `LISTEN`. The channel is the
existing `outbox_events`.

Behaviour:

- A notification only sets the asyncio event that ends the idle wait. Claiming,
  leases, backoff, dead letters, redrive, ordering and readiness are unchanged.
- The `poll_interval_seconds` wait remains as the recovery path, so a missed or
  dropped notification delays delivery by at most the existing two seconds.
- `PgNotifyListener` reconnects on connection loss; while it is down, polling continues
  and a new gauge `vm_scoring_outbox_notify_listener_up` reads 0. The gauge sits next
  to `vm_scoring_outbox_relay_up` and is exposed through the supervisor's
  `/metrics/scoring` proxy.
- The listener is stopped before the relay task ends, using the existing
  `_stop_notify_listener`.

## 5. Worker delivery loop

Add `SignalDeliveryLoop` to [signal_worker.py](../../../apps/indicator_runner/indicator_runner/signal_worker.py)
(or a sibling module if the file's size cap requires it). It is a daemon thread that
owns every call to `DurableSignalRelay.run_once` for the worker.

- **Wake, do not deliver, from the processing paths.** The bar path in
  `_catchup_symbol_locked` and the panel path in `_process_prepared` replace their
  `relay_once()` call with `wake()`, which sets a `threading.Event`. No HTTP call runs
  while `_processing_lock` is held. The `relay_once` callback passed into
  `SynchronizedPanelRuntime` becomes the wake callback.
- **Drain on wake.** After a wake the loop calls `run_once` repeatedly while the
  previous pass delivered at least one record, because ordered claiming yields one row
  per partition. Draining stops when a pass delivers nothing, so failed records keep
  their backoff. A cap of 200 passes per wake bounds one drain; the loop re-arms itself
  if the cap is hit.
- **Idle recovery.** Between wakes the loop waits `SIGNAL_RELAY_IDLE_INTERVAL_SEC`
  (default 5, range 1 to 60) and then runs one pass, which picks up records whose
  backoff has expired and any wake lost to a race. The main loop's one-second
  `relay_once()` call is removed; `catchup_all()` keeps its existing floor, and the
  readiness snapshot moves from every second to the idle interval.
- **Lock discipline.** The delivery loop never acquires `_processing_lock`. It uses
  its own sessions from the shared pool, which is why the pool holds two connections.
- **Shutdown order.** `SignalWorker.stop()` stops the LISTEN thread, then signals the
  delivery loop to stop and joins it with a timeout no shorter than one HTTP attempt
  budget, then returns. `main()` disposes the engine only after `stop()` returns, so a
  pass never runs against a disposed pool. An in-flight lease that cannot be marked
  before exit expires normally and is redelivered by the next process; the existing
  external-signal identity makes that redelivery a no-op at scoring.
- **Failure of the loop itself.** If the delivery thread dies, the main loop detects
  it the same way it detects a dead listener thread today and exits non-zero, so the
  indicator supervisor restarts the worker.

## 6. Retiring the observational topics

Remove the producers and the publisher plumbing together:

| Topic | Producer removed | Record that remains |
| --- | --- | --- |
| `signals.ingested`, `signals.scored` | [pipeline_events.py](../../../apps/scoring_engine/scoring_engine/pipeline_events.py) `CanonicalSignalEvent`, `ScoredSignalEvent` | `canonical_signals`, `asset_scores`, `decision_contexts`, `execution_decision_logs` |
| `execution.results` | [execution_persistence.py](../../../apps/execution_engine/execution_engine/execution_persistence.py) and [paper_order_lifecycle.py](../../../apps/execution_engine/execution_engine/paper_order_lifecycle.py) `ExecutionResultEvent` | `execution_logs` plus the canonical `orders` and `executions` ledger |
| `feedback.ready` | [engine.py](../../../apps/feedback_loop_engine/feedback_loop_engine/engine.py) `FeedbackEvaluationEvent` | `signal_performance` |

Required preservation step: before the result event is removed, the inbound command's
`event_id` (today `user_strategy_config["_causation_event_id"]`) is written into the
`execution_logs.execution_details` JSON under `causation_event_id`; the paper
lifecycle writes no `execution_logs` row, so its trace remains the canonical ledger
lineage from `orders` through `order_intents.canonical_signal_id`. This keeps the
command-to-result trace without a schema change.

Follow-on removals in the same change: the four topics leave `ScoringRelayConfig.topics`
and `publish_topics` defaults, `SCORING_OUTBOX_TOPICS` in `.env.example`, and the
documentation; `EVENT_BUS_PUBLISH_TOPICS` is removed; `NoOpEventPublisher`,
`EventPublisher` and the `publish_topics` branch in the relay are removed once nothing
references them; the four event classes in `internal_events.py` are removed.
`ExecutionResultEvent` fields that other code imports are checked first, and any
remaining consumer is migrated to `execution_logs` rather than kept alive by the event.

Existing rows: a guarded, idempotent Alembic data migration updates rows of the four
topics whose status is `pending`, `failed` or `in_progress` to `published`, with
`delivery_metadata` set to `{"publisher": "retired"}`. Already published rows are left
as history. Readiness is unaffected because `_REQUIRED_OUTBOX_TOPICS` never included
these topics; the backlog metric's topic label list shrinks accordingly.

## 7. Configuration and documentation

| Change | Where |
| --- | --- |
| New `SCORING_OUTBOX_NOTIFY_ENABLED`, boolean, default `true` | scoring child environment allowlist, `ScoringRelayConfig`, `.env.example`, CONFIGURATION.md |
| New `SIGNAL_RELAY_IDLE_INTERVAL_SEC`, integer 1..60, default `5` | indicator child environment allowlist, worker startup config, `.env.example`, CONFIGURATION.md |
| Indicator child pool values fixed at 2 / 0 / 2 | `platform_processes.py`; documented with the connection formula in CONFIGURATION.md |
| `SCORING_OUTBOX_TOPICS` default reduced to the two execution topics; `EVENT_BUS_PUBLISH_TOPICS` removed | `config_validation.py`, `.env.example`, CONFIGURATION.md |
| E2E guide metric list gains `vm_scoring_outbox_notify_listener_up` | E2E_VERIFICATION_GUIDE.md |
| One Unreleased entry | CHANGELOG.md |

`AGENTS.md` and `CLAUDE.md` are not affected.

## 8. Testing and acceptance

Unit and component tests, SQLite where the outbox store already runs on SQLite:

- Delivery loop: a wake drains multiple ordered rows in one wake; a pass that only
  fails stops the drain; the idle pass runs at the configured interval; `stop()` joins
  the loop before the engine is disposed; a dead loop thread is detected by the main
  loop.
- Panel runtime: the injected callback is a wake and no HTTP client is invoked under
  the processing lock (assert by a lock-held probe in the stub relay).
- Relay: with a stubbed listener, a notification ends the idle wait early; with the
  listener disabled, behaviour equals today's; listener death sets the gauge to 0.
- Launcher: the indicator spec carries the fixed pool values and every other spec is
  unchanged; the scoring spec carries the notify flag.
- Retirement: producers no longer enqueue; the data migration is idempotent and leaves
  published rows untouched; `execution_logs.execution_details` carries
  `causation_event_id` on the immediate path; lifecycle fills are traced through the
  ledger.

One new opt-in PostgreSQL integration module, patterned on
[test_postgres_notify_integration.py](../../../apps/indicator_runner/tests/test_postgres_notify_integration.py)
and using the recorded June 2026 Coinbase fixture, runs twenty `SignalWorker`
instances in one test process, each with its own engine, delivery loop and LISTEN
connection, against a probe strategy:

1. Twenty workers start against an isolated database and a stub scoring HTTP server.
   `pg_stat_activity` for the test database never exceeds its pre-start baseline by
   more than 3 per worker.
2. One committed batch of bars followed by one `NOTIFY` produces one journaled
   transition per worker; every `signals.submit` row is published; the test records
   the distribution of `published_at - created_at`.
3. A repeated `NOTIFY` and a forced lease expiry produce no duplicate canonical
   signal at the stub, and per-partition order is preserved.
4. Stopping the stub for longer than the idle interval grows the backlog; restarting
   it drains the backlog through the recovery pass without a NOTIFY.
5. Stopping every worker during a drain returns within the stop timeout, leaves no
   row `in_progress` past its lease, and disposes the engine after the loop has
   stopped.

Manual acceptance on the isolated paper stack: rebuild the image, recreate both
runtime groups, and read `vm_outbox_oldest_undelivered_age_seconds`,
`vm_scoring_outbox_notify_listener_up`, `vm_indicator_signal_backlog_oldest_seconds`
and `pg_stat_activity` counts before and after. Latency is reported as measured, not
promised.

## 9. Rollout, rollback and risks

Rollout is one pull request following the change discipline in CLAUDE.md, validated
with the affected suites, then `vmdev audit --strict` and `vmdev test all`, then the
paper stack acceptance above.

Rollback: every runtime change is behind an existing process boundary or a flag.
`SCORING_OUTBOX_NOTIFY_ENABLED=false` restores pure polling. The delivery loop has no
flag; reverting the commit restores in-lock delivery. The retirement migration's
downgrade is a no-op that leaves the `retired` rows published, because re-creating
producers for topics nobody consumes has no value; the downgrade docstring says so.

Risks and their mitigations:

- A worker holding two connections could still queue behind `pool_timeout` if a third
  pool user is introduced later. The unit test that asserts only two pool users exists
  to catch that.
- Notify storms under twenty strategies waking together produce at most one relay
  wake per notification; the drain already batches, so this is bounded.
- Removing `execution.results` loses nothing durable once `causation_event_id` is in
  `execution_logs`; the PostgreSQL pipeline gate test is extended to assert that field.

## 10. Staging of Spec B

Spec B, in which the execution engine claims `execution.commands` and
`execution.rebalance.commands` directly from the outbox, is deferred. It needs an
UPDATE row-level policy for `vm_execution` limited to those topics, a consumer-side
re-expression of the endpoint's retry classification, causation and authority
snapshot handling, and migration of the relay tests. It is written only if the
measurements from section 8 show delivery latency or relay failure surface still
mattering after this spec has landed.
