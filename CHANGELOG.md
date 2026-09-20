# Changelog

This file records concise, user-visible changes in vynmatrix. Detailed design,
operational, and verification material belongs to the documents linked from
[README.md](README.md); historical source-snapshot entries remain available in
Git history.

## [Unreleased]

### Added

- `vmdev init`, `vmdev doctor` and `vmdev deploy`: one idempotent command
  installs or upgrades this deployment, and one generates the private `.env`
  and owner profile with all fifteen secrets distinct. `vmdev init --update`
  adds configuration keys that appeared upstream without touching an existing
  value; `vmdev deploy --plan` prints the image, migrations, configuration keys
  and snapshot a run would use; `--start-only` brings a stopped stack back up.
- Stamped images and a deployment record. The platform image carries the git
  commit it was built from as labels and `/app/BUILD_INFO.json`, is tagged
  `sha-<commit12>` (`-dirty` for an uncommitted tree), and resolves its base
  through `VM_SVC_BASE_REF` instead of a moving `latest`. Migration `0108`
  adds the `deployments` table, which records what was installed and how the
  run ended, with column-level `SELECT` for the backend role. `GET
  /api/ui/version` returns it and the owner UI footer shows the running build.
- An upgrade takes a verified `pg_dump` snapshot before it changes anything and
  restores it, re-points Compose at the previous generation and records
  `rolled_back` when a stage after the runtime stops fails. It refuses to
  restore automatically without a verified snapshot. See
  [DEPLOYMENT.md](docs/DEPLOYMENT.md).

- Ten disabled native signal strategies selected from the Backtrader catalogue;
  selection, deviations and validation evidence are tracked in
  [strategy readiness](docs/STRATEGY_READINESS.md#backtrader-migration).
- The owner UI's Strategies page reports whether each strategy is released for
  trading, counts what is registered rather than what ships on disk, and says
  why an unreleased strategy cannot be bound.
- A read-only owner UI served by the backend at `http://127.0.0.1:8081/`:
  Dashboard, Strategies and Profit and loss pages with no build step, new
  container, port or dependency. It unlocks with `BACKEND_ADMIN_API_KEY` and
  reads through `/api/ui/*`; migration `0107` grants the backend role
  column-level, owner-scoped SELECT on exactly the columns those pages read.

### Fixed

- Host-side `vmdev db migrate`, `vmdev db roles`, `vmdev db catalogue` and
  `vmdev user` resolve the container database hostname to the published loopback
  listener themselves. They no longer require a hand-exported, rewritten
  `MIGRATION_DATABASE_URL`, which the setup guide previously asked for and which
  made the documented incremental upgrade path fail.
- Historical scoring observes candle closes only after their interval completes;
  signal redelivery preserves the canonical origin used by durable execution commands.
- Native bar strategies preserve their configured evaluation horizon through scoring
  and feedback instead of falling back to a one-day horizon.
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

- Compose refuses to resolve a moving image tag: `VM_DEPLOY_IMAGE_TAG` has no
  `latest` default and must name the immutable tag `vmdev deploy` writes.
- `.env.example` is a template rather than a file to edit by hand, and gains
  `VM_PAPER_ACCOUNT_INITIAL_EQUITY`. A fresh install creates the local paper
  account with that starting equity through the onboarding service.
- [SETUP.md](SETUP.md) is the single owner of the installation sequence, and
  [DEPLOYMENT.md](docs/DEPLOYMENT.md) owns the upgrade and rollback contract.
  `vmdev build venvs` is documented as a contributor step with its TA-Lib
  prerequisite and is no longer part of installing or upgrading.
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
