# Changelog

This file records concise, user-visible changes in vynmatrix. Detailed design,
operational, and verification material belongs to the documents linked from
[README.md](README.md); historical source-snapshot entries remain available in
Git history.

## [Unreleased]

### Added

- A standalone frontend for the owner UI, with currency-preserving monetary
  displays, explicit unavailable data, authenticated access, and frontend
  tests, lint, and build checks enforced by the required CI gate.
- Adverse-news gate storage and classification for USQualityCompounder (inert;
  migration 0111). The gate is not wired into the panel resolver and changes no
  strategy behaviour.
- SEC filing gate evidence for USQualityCompounder (inert; migration 0112). The
  quarter-end producer now records 8-K item codes and NT late-filing notices
  after the 0.2.0 panel commits; a failure there never fails the panel, and the
  collection stops issuing EDGAR requests 6 hours after its cutoff or 15
  minutes before the execution open, whichever is earlier. The resolver is not
  wired into the panel and excludes no name. Run the acceptance diagnostic with
  `python -m market_data_ingestor.sec_filing_event_history --submissions-dir <dir>`;
  a repeatable `--cutoff <ISO-8601 aware>` replays exact cutoffs instead of
  quarter ends. Members without recorded submissions are reported as missing,
  not clean, and the report lists every input file with its SHA-256.
- `vmdev db release-strategy` releases one exact registered strategy version for
  paper, so a strategy other than the Swing canary can be bound and shown as
  Released. The evidence is a reviewed commit: the strategy's `config.json` must
  say `READY_FOR_PAPER_TRADING` (or `LIVE_CANDIDATE`) with a keep decision, the
  registered row must equal the packaged release, no other version may be
  released, and the latest successful `vmdev deploy` must come from a clean
  checkout whose commit has the same strategy directory. `--check` previews and
  writes nothing; `--apply --reason` writes the two release columns and one
  `api_audit_logs` row. `vmdev db withdraw-strategy` undoes it, and is refused
  while a binding is switched on, while the strategy's own fills leave a
  position open, or while any of its orders, executions, scoring decisions,
  rebalance plans or outbox events is unfinished. Both refuse unless
  `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`, and neither
  touches an account, binding, credential or live gate. No shipped strategy is
  labelled paper-ready yet, so none is releasable until a reviewed change
  relabels it.
- More than one strategy can trade the same account from the owner UI. A new
  binding is scoped to the instruments its strategy's release declares,
  resolved through the instrument catalogue, instead of to every instrument, so
  strategies that share no instrument can all be in **Trading** on one account.
  Two active strategies still never share an instrument on one account; the
  refusal now names the strategy in the way and what the two share. The
  Strategies page shows each binding's scope. A membership universe such as the
  point-in-time S&P 500 has no list (see the next entry). A binding made before
  this change keeps its old scope; `POST /bindings` can re-scope it.
- The owner UI can change a binding's instrument scope, so strategies whose
  declared universes overlap can still share one account: narrow one of them
  (for example SwingHighLowPMO to ETHUSDC and SOLUSDC beside a BTCUSDC
  strategy) and both can be in **Trading**. The new scope is a non-empty list of
  catalogue instruments. The overlap rule runs against it whenever the binding
  is or becomes active, and a change that drops an instrument the strategy still
  holds on that account is refused, because that position's close signals would
  stop reaching it.
- An equity strategy with a membership universe, such as USQualityCompounder,
  can be in **Trading** beside crypto strategies on one account. A new binding
  takes its strategy's declared asset class as `asset_classes_allowed`, taken
  as declared: an ETF strategy is `etf`, never `equity`. A binding without an
  instrument list now covers every instrument in its asset classes rather than
  every instrument, in the backend's overlap rule and in the
  `vm_validate_strategy_binding` trigger (migration `0110`) alike, so a second
  active strategy is refused only when the two can trade a common instrument.
  The downgrade of `0110` refuses while two active bindings on one account
  rely on that separation.
- A readiness checklist for each binding on the owner UI's Strategies page. It
  says why a bound strategy is or is not trading, in pipeline order: released,
  mode, account connected, a signal worker processing market data, the last
  scoring decision with its reason, and the last blocking risk breach on the
  account. A failed condition reads **Blocked**; recent history is a warning.
  It reads recorded state only and changes nothing. Migration `0109` grants the
  backend role column-level `SELECT` on exactly the columns it reads, with
  owner-scoped row policies.
- The owner UI can switch a strategy on. A released strategy binds to one of the
  owner's connected accounts in one of three modes — **Off**, **Close only** or
  **Trading** — which are the combinations of the four authority flags that
  satisfy every CHECK on `user_strategy_bindings`, so the control cannot build a
  row the database rejects. Changes are partial, fenced by `{expected, changes}`,
  confirmed before they are sent, and audited; refusals are audited too. No
  migration was needed: the backend role already held the grants.
- A Settings page for the owner profile and the non-secret configuration of each
  broker account, with the recent change log. Credentials are never entered in
  the browser; the account card shows only whether one exists, its status and its
  expiry. `GET /api/ui/control` serves it and returns `config_key` and
  `external_ref`, which both PATCH routes require the caller to echo back.
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

- A fresh install works on Windows. The constrained tooling install skips
  `uvloop`, which has no Windows build; the Linux image still installs it, and
  `vmdev audit` accepts only that host marker on a runtime pin. `vmdev init`
  makes `.env` and `owner.local.yaml` owner-only with an access-control list
  for the owner, SYSTEM and Administrators, instead of checking POSIX mode bits
  that Windows always reports as `0666`. A failed `vmdev init` now removes the
  files it wrote, so it can run again.
- A fresh install on Linux, including a cloud VM, no longer breaks its first
  upgrade. Docker Engine created the bind-mounted `.artifacts` as root, and the
  next pre-upgrade snapshot could not be written; `vmdev` now creates it as the
  deploying user before Compose creates a container.
- `vmdev doctor` and the `vmdev deploy` preflight check that the Docker daemon
  answers (`docker compose version` passed with it stopped), that Buildx is
  installed and `DOCKER_BUILDKIT` does not switch BuildKit off, and that
  `.artifacts` is writable, naming the `chown` that repairs a root-owned copy.
- The daily trade limit fails closed. When the execution log is wired but
  today's trade count cannot be read, execution is now blocked as
  `account_state_unavailable` instead of counting from the profile cache, whose
  `daily_trades` defaults to zero and so disabled the limit. Paper may still
  fall back to that cache when the broker is unreachable, but not past the count.
- The Dashboard's "released" count now uses the bind gate's condition (an active
  strategy row *and* an active version), matching the Strategies page.
- `vmdev deploy` reports how many strategies are released, bound and switched on,
  read from the database after the deployment succeeds, instead of always
  printing "No strategy is released or bound" — which was false on an upgrade of
  an installation with a released, bound strategy. If the read fails it says so
  rather than guessing. `--json` carries the same counts under `trading`.
- Binding a strategy for trading now requires the release the UI always claimed
  it required. `POST /bindings` never checked it: `_require_strategy_release`
  was reachable only from `PUT /strategy-configs`, so an unreleased strategy
  could be bound and switched on. It was inert — the scoring engine refuses to
  persist its signals — but it looked armed and said nothing. Four pieces of UI
  copy that described the missing gate are corrected with it.
- The Strategies page reported a strategy as released when `strategies.is_active`
  was true even if no version was active, which is weaker than the gate. It now
  reports exactly what the gate accepts.
- A binding without an instrument list now trades. Scoring resolved its asset
  classes only against instruments that some binding listed by id, and active
  lists hold symbols, so such a binding matched no signal at all; it now
  matches every catalogued instrument of its classes. Execution checks the same
  bound before each order, where it used to accept any instrument. A binding
  made earlier with no list takes the model default, `crypto`: check its scope
  before switching it on.
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

- Binding authority rules moved from the `POST /bindings` route body into
  `lib_application.services.binding_control`, so the HTTP surface and the owner
  UI share one implementation of the account lock, the broker check, the
  instrument canonicalization, the overlap rule and the release gate. The
  refusal message for an unreleased strategy is now "strategy is not released
  for trading" on both surfaces.
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
