# Changelog

This file records concise, user-visible changes in vynmatrix. Detailed design,
operational, and verification material belongs to the documents linked from
[README.md](README.md); historical source-snapshot entries remain available in
Git history.

## [Unreleased]

### Fixed

- Close and flatten transitions now durably cancel obsolete local-paper
  protective orders; a reduce-only row with no remaining position is terminally
  cancelled instead of retried.
- The platform supervisor forwards each child's service key when proxying
  `/metrics/<component>`.
- `FX_RATE_HISTORY_DAYS` permits up to 366 days for Coinbase `USDC/EUR`;
  ECB reference history remains bounded by its official rolling 90-day feed.
- Host-side `vmdev db bootstrap` now loads the checkout's `scripts` package
  reliably.
- The scoring engine and the backend each hold one bounded database pool: the
  scoring store no longer opens SQLAlchemy's default fifteen-connection pool
  beside its session factory, and the backend's db secrets provider reuses the
  backend's engine, so twenty strategies fit the documented 93-connection budget.
- Strategy workers end their delivery loop between passes on stop, report a pass
  that outlives the stop budget instead of disposing the engine silently, and
  the indicator process manager grants the whole fleet one grace deadline that
  covers that budget.
- `vm_scoring_outbox_notify_listener_up` reads 1 only while PostgreSQL has
  acknowledged the `LISTEN`, not while the listener thread is merely alive.
- The supervisor rejects the `postgresql+psycopg2://` URL spelling for every
  service role, and LISTEN connections normalize it when handed one directly.
- The strategy worker's catch-up floor and liveness checks run on a fixed
  one-second tick again; `SIGNAL_RELAY_IDLE_INTERVAL_SEC` no longer delays them.
- Migration `0106` marks the dead-lettered rows of the retired outbox topics
  published so they cannot fail soak acceptance.

### Changed

- Strategy signal delivery runs on a dedicated loop outside the bar-processing
  lock, woken by each committed transition, and the scoring outbox relay wakes on
  the existing `outbox_events` notification (`SCORING_OUTBOX_NOTIFY_ENABLED`).
- Strategy worker subprocesses use a fixed two-connection pool with no overflow;
  `SIGNAL_RELAY_IDLE_INTERVAL_SEC` sets the delivery loop's recovery cadence.
- Retired the consumer-less outbox topics `signals.ingested`, `signals.scored`,
  `execution.results` and `feedback.ready` with their producers; migration `0105`
  marks any undelivered rows published. `execution_logs.execution_details` now
  carries `causation_event_id`. `EVENT_BUS_PUBLISH_TOPICS` is removed.
- Consolidated repository documentation around one owner per topic: shared
  setup, architecture, configuration, database lifecycle, deployment, evidence,
  operations, and strategy readiness now link to one another instead of
  repeating contracts and commands.
- Updated the custom license to require source publication and an upstream pull
  request for every Enhancement, with a conditional redistribution grant.

## 2026-09-05

### Added

- Explicit single-owner bootstrap, inactive reference registration, guarded
  owner/account control-plane operations, and a three-container local runtime
  with a two-container combined alternative.
- The Vynmatrix Personal Noncommercial Reciprocity License and retained
  attribution/provenance notice for publication at `vynaptic/vynmatrix`.

### Changed

- Preserved the canonical signal → scoring → transactional outbox → execution
  → feedback path while keeping paper mode and the live-execution gate disabled.
- Recorded outstanding fixture provenance and independent-authority limits in
  [NOTICE](NOTICE) and [docs/MIGRATION.md](docs/MIGRATION.md).
