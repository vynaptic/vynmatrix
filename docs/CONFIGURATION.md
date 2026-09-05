# Runtime Configuration

This source configuration reference does not claim a deployed service or authorize
publishing, deployment, or live execution. See [the release boundary](DEPLOYMENT.md).

This is the single source of truth for runtime configuration ownership and
precedence. [`.env.example`](../.env.example) is the variable catalogue;
deployment-specific secret values belong outside git.

## Precedence

For a running service, configuration is resolved in this order:

1. Explicit command-line or constructor arguments used by an operational
   command.
2. Process environment variables injected by the local Docker Compose runtime
   or a separately reviewed future deployment.
3. Source-controlled strategy `config.json` values for strategy behavior only.
4. Validated code defaults.

`config/build.yaml` and `config/containers.yaml` describe artifacts, not runtime
trading policy. `config/deployment/*.yaml` is consumed only by the
`ApplicationManager` path. Those files contain only the provider-neutral
strategy-runner endpoint, restart policy, and secret names; database URLs and
deployment topology are injected by the actual runtime. This migration includes
only the local Compose topology; no external infrastructure repository is assumed.

## Ownership

| Surface | Authority |
|---|---|
| Local runtime values | `.env` passed to `docker/docker-compose.stack.yml` |
| Local role-specific database URLs | `docker/docker-compose.stack.yml`; future deployment ownership is unconfigured |
| Designated owner profile, broker accounts and bindings | Shared owner services used by `apps/backend` and `vmdev user` |
| Per-account broker ciphertext | `managed_secrets`, encrypted through `SECRETS_MASTER_KEYS` |
| Tradable instrument identity and broker aliases | `config/instruments.yaml` plus database migrations/bootstrap |
| Observed currency-conversion inputs | Selected `fx` worker: official ECB EUR reference rates plus Coinbase USDC-EUR candles persisted in `prices` |
| Strategy behavior | the selected strategy's source-controlled `config.json` |
| Point-in-time equity evidence | Immutable provider lineage, observations, factor/rank snapshots, and registered strategy panel inputs; entitlement and `data_use_scope` are part of the content identity |
| Portfolio rebalance authority | Tenant-neutral model rebalance from the completed panel decision, then one frozen user/binding/broker-account plan evaluated by scoring and leased by execution |
| Exact paper strategy authority | evidence-hashed `paper_strategy_promotion.json`, mounted read-only into indicator and scoring; never live authority |
| Versioned feedback baseline | immutable `strategy_versions.default_params`, registered from that release's validated strategy parameters |
| Build dependencies, component group labels, and images | `config/build.yaml`, `config/containers.yaml`, `docker/constraints.txt` |
| Repository review routing | `.github/CODEOWNERS` routes default pull-request review to `@vynaptic`; GitHub protects `main` separately |

**Dependency version authority is two-tier.** Abstract ranges live in each component's
`setup.py` (`install_requires`); the production lock is `docker/constraints.txt`. `vmdev
audit` enforces agreement between the two, and first-party dependency truth, via the
`runtime-dependency-contract` and `first-party-dependency-contract` rules. Add a dependency
in both places, never only in the lock.

The scoring, execution, and indicator runtimes consume configuration snapshots;
they do not own parallel user-binding or instrument write APIs.

## Process groups and credential inputs

Compose runs `application` plus the `workers` profile by default configuration
(`COMPOSE_PROFILES=workers`, `PLATFORM_APPLICATION_GROUP=application`). For the
two-container layout, clear `COMPOSE_PROFILES` and set the application group to
`all`. PostgreSQL counts toward the limit of three running containers. A separately
approved gateway container requires the combined layout. Use [DATABASE.md](DATABASE.md)
for the lifecycle: bootstrap stops both groups before its maintenance one-shot;
raw wildcard profiles or concurrent `compose run` jobs bypass that bound.

| Process | Parent input mapped to child `DATABASE_URL` | Internal port |
|---|---|---:|
| Backend | `BACKEND_DATABASE_URL` / `vm_backend_login` | 8081 |
| Scoring, inline relay | `SCORING_DATABASE_URL` / `vm_scoring_login` | 8001 |
| Execution | `EXECUTION_DATABASE_URL` / `vm_execution_login` | 8000 |
| Feedback daemon | `FEEDBACK_DATABASE_URL` / `vm_feedback_login` | 8002 |
| Primary market feed | `MARKET_DATA_DATABASE_URL` / `vm_market_data_login` | 8003 |
| FX | Same market-data role | 8004 |
| IBKR / Saxo / NSE calendars | Same market-data role | 8005 / 8006 / 8008 |
| Equity feed | Same market-data role | 8007 |
| Indicator supervisor | `INDICATOR_DATABASE_URL` / `vm_indicator_login` | 8080 |
| Platform supervisor | No database connection | 8090 |

Only backend and PostgreSQL publish host ports, both on `127.0.0.1`. Child
interpreters receive explicit allowlists. API service inputs are
`BACKEND_ADMIN_API_KEY`, `SCORING_API_KEY`, `EXECUTION_API_KEY`,
`SCORING_ADMIN_API_KEY` and `EXECUTION_ADMIN_API_KEY`; all five must differ
from each other and from any supplied worker service key.
Scoring/execution receive only their own admin key as `ADMIN_API_KEY`, while
service keys map to `API_KEY`. Backend uses `X-Admin-Key`; scoring/execution
admin endpoints additionally require `X-Admin-API-Key`. Feedback and selected
market workers require `FEEDBACK_API_KEY` and `MARKET_DATA_API_KEY` respectively.
Use distinct keys for all boundaries. The inline relay receives the execution
service key; indicator receives only the scoring service key.

Application children require full role URLs; generic `DATABASE_URL`,
`DB_USER`/`DB_PASSWORD`, administrator/migration URLs and PostgreSQL credential
variables are forbidden in runtime groups. Compose keeps maintenance inputs
out of their environment. `ADMIN_DATABASE_URL` targets the `postgres` database;
`MIGRATION_DATABASE_URL` names an explicit non-`vm_` maintenance login and target
database on the same server. Fresh historical migrations require a PostgreSQL
superuser because they alter role attributes. The default uses the same `trader`
login/password for both maintenance stages; a different login must already have
that authority. Nothing grants elevated rights automatically. Six
`VM_<ROLE>_DB_PASSWORD` inputs provision the
six runtime logins during maintenance only. Container URLs use `postgres:5432`;
see the database guide for explicit host-command URL overrides.

Backend and execution alone receive `SECRETS_MASTER_KEYS`; the launcher fixes
`SECRETS_BACKEND=db`, `BACKEND_ALLOW_ANON=false`, `EXECUTION_MODE=paper`,
`RUN_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`. Application startup
requires the key ring even before owner account onboarding. Other children do
not inherit it. No global broker trading keys or caller-selected owner are
accepted as execution authority.

Feedback always runs in `workers`/`all`. `PLATFORM_WORKERS` selects only
`market-data,equity,fx,calendar-ibkr,calendar-saxo,calendar-zerodha`; empty means
none of those. `STRATEGY_LIST` is independently explicit and empty skips the
indicator runner. Selection does not replace strategy/version/binding gates.

## Startup snapshots and validation

Execution, scoring, the indicator supervisor, and each
indicator worker call their validated configuration loader once at the process
composition root. The resulting immutable startup configuration is injected
into long-lived collaborators. Changing an environment variable after
construction does not alter routing, gating, deduplication, circuit-breaker,
scoring, binding-cache, relay, strategy selection, child-process credentials,
or worker catch-up behavior; restart through the supported lifecycle to apply
a configuration change.

Optional empty Compose settings are omitted from child environments so dynamic
defaults apply. Present malformed numeric and boolean values fail startup. The canonical
parsers in `lib_common.env_utils` own bounds and accepted boolean spellings;
services must not reinterpret values locally. `vmdev audit` checks direct
environment calls as well as values reached through mapping aliases and local
variables. Ordinary string reads remain valid at a composition root for URLs,
credentials, paths, and process host/logging options.

The scoring outbox relay receives its execution API key explicitly from the
composition root and retains that snapshot for every delivery. The fixed
Coinbase market-fill polling timeout is a broker safety invariant, not a
runtime override.

Every PostgreSQL engine, including one created from a service-specific explicit
URL, uses the same bounded process-local pool policy. `DB_POOL_SIZE`,
`DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, and
`DB_POOL_RECYCLE_SECONDS` default to `3`, `2`, `10`, and `1800`; startup rejects
their combined capacity above `DB_POOL_CONNECTION_BUDGET` (default `5`).
Pre-ping and LIFO reuse are always enabled. Pool capacity, checked-out
connections, overflow, and pressure ratio are exported as
`vm_database_pool_*` metrics without exposing a connection URL.

Compose uses bounded `json-file` logging for every declared service.
`DOCKER_LOG_MAX_SIZE` and `DOCKER_LOG_MAX_FILES` default to `10m` and `3`;
increasing them requires an explicit local disk budget review.

Compose uses a fixed 60-second application/worker stop grace period. The platform
supervisor forwards termination, reaps process groups, retries required children
at most three times with bounded backoff, and bounds whole-group shutdown to
55 seconds. Each component still uses its own drain and cleanup coordinator.
A required process that exhausts retries ends its group; partial failure is not
silently reported as a healthy runtime.

The feedback optimizer never reads a mutable filesystem configuration. It uses
the exact active `strat_ver_id` attached to the signal and its persisted
`default_params` release snapshot. Strategy registration must therefore update
the source-controlled config and the matching version snapshot together; a
missing, mismatched, invalid, or non-adjustable snapshot suppresses feedback.

## Runtime fail-closed requirements

- `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false` remain set
  throughout local development and migration verification. No live authority is supplied.
- The scoped service/admin keys above protect different boundaries. Backend
  resolves the designated owner and rejects caller-supplied user identity.
- Each service receives its own least-privilege database login. Pipeline
  services must not connect as the schema owner or PostgreSQL superuser.
- Every selected child receives its explicit role URL from the launcher. Runtime
  groups do not assemble credentials from administrator variables.
- Alembic is the unconditional PostgreSQL schema authority; application
  `create_all` is restricted to isolated SQLite unit stores.
- `SECRETS_BACKEND=db` requires a newest-first comma-separated
  `SECRETS_MASTER_KEYS` ring. Key rotation is an explicit account-scoped
  operation; repository history rewriting does not rotate exposed credentials.
  Backend and execution child processes receive the same backend and ring because
  backend writes account credentials and execution resolves their `secret_ref`.
- A binding names one concrete `broker_account_id`. That identity is preserved
  through decision, outbox command, order, fill, position, P&L, and feedback.
- A live broker route must implement both exact order-scoped fill retrieval and
  a broker-specific authenticated certification workflow. Order-status
  cumulative quantities never create `executions`; missing venue trade ID,
  actual timestamp, fee amount, or fee currency leaves the route blocked.
- New binding rows default to `is_active=false` and `autopilot=false`. Enabling
  execution is a separate, explicit operator decision; omitted request fields
  never authorize trading.
- `entries_enabled` and `exits_enabled` are separate authority. A close-only
  binding has `is_active=true`, `entries_enabled=false`, and
  `exits_enabled=true`; entry authority additionally requires `autopilot=true`.
  The API and database reject authority on an inactive binding and reject more
  than one active strategy binding for the same broker account and canonical
  instrument until allocation accounting exists.
- Execution re-resolves the current user, binding, broker account, credential,
  environment, and route immediately before broker I/O. An accepted scoring
  snapshot is audit context, not an irrevocable submission grant.
- `users.base_ccy` and `linked_broker_accounts.base_ccy` are required. Account
  balances, position value, P&L, and NAV are normalized to that account/user
  currency only from fresh, point-in-time observations. Missing conversion
  legs fail closed; USD/USDC parity is never assumed.
- Invalid numeric and boolean environment values are handled only through
  `lib_common.env_utils`; `vmdev audit` rejects new hand-rolled parsers.
- `SCORING_OUTBOX_TOPICS` must retain both `execution.commands` and
  `execution.rebalance.commands`. Removing either route fails scoring startup;
  the portfolio topic does not grant a strategy or binding execution authority.

## Progress readiness and runtime SLOs

The platform supervisor serves `/health` (required processes/listeners alive),
`/ready` (all selected component progress checks), `/status` and `/metrics` on
port 8090. `/metrics/<component>` proxies that component's metrics separately.
Initial owner setup or unavailable selected feeds can keep trading unready while
management stays healthy. Feedback readiness checks its durable successful
heartbeat: `EVALUATION_INTERVAL` defaults to 300 seconds;
`FEEDBACK_HEARTBEAT_MAX_AGE_SECONDS` defaults to interval plus the larger of
60 seconds and half the interval. An absent, stale, failed or future heartbeat
is not ready.

Process liveness is necessary but is not trading readiness. The current
low-volume defaults are deliberately loose five-minute bounds:

| Variable | Default | Readiness contract |
|---|---:|---|
| `SCORING_OUTBOX_MAX_AGE_SECONDS` | `300` | Scoring and its inline relay are unready when an `execution.commands` or `execution.rebalance.commands` event is older than this or any such event is dead-lettered. |
| `INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS` | `300` | The indicator parent is unready when a selected child's durable signal envelope is older than this or its partition contains a dead letter. |
| `INDICATOR_MAX_STRATEGY_LAG_SECONDS` | `300` | The indicator parent is unready when a selected strategy watermark trails the latest required complete source bar beyond this bound. |
| `INDICATOR_PANEL_DATA_USE_SCOPE` | unset | Required for a synchronized panel worker and must be exactly `paper_forward`; historical-validation revisions are never runtime inputs. |
| `INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID` | unset | Canonical user owning the personal data entitlement. It may fall back to `SP500_RESEARCH_OWNER_USER_ID` inside the worker, but the resolved owner is persisted and matched exactly. |
| `INDICATOR_PANEL_ACTIVATION_CUTOFF` | unset | Inclusive, offset-aware decision-cutoff watermark. Earlier exact-owner inputs are terminally `skipped`; inputs whose official execution session has closed are terminally `expired`. |
| `EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS` | `300` | Execution is unready when an eligible durable local-paper order has not consumed committed market time within this bound. |

Execution readiness also requires zero `submission_unknown` orders, successful
database and local-paper rehydration checks, and completion of initial
reconciliation for every account partition discovered from active bindings,
recoverable orders, and non-flat canonical positions. Indicator health exposes
per-strategy lag and outbox counts; scoring exposes outbox counts, oldest age,
and progress; execution exposes pending-order state/age, paper processing lag,
unknown submissions, and reconciliation partitions. Configure alerts from the
corresponding `/metrics` series; do not raise these bounds merely to hide a
stalled worker.

## Exact paper-strategy promotion configuration

`INDICATOR_PAPER_PROMOTION_MANIFEST` and
`SCORING_PAPER_PROMOTION_MANIFEST` must point to the same read-only manifest,
and `VM_DEPLOY_IMAGE_TAG` must match its immutable image tag. In
staging/production paper mode, a missing, stale, or mismatched manifest fails
closed. `scripts/write_paper_promotion_manifest.py` hashes the packaged strategy
config, released indicator image tag, exact evidence set, user, binding,
dedicated account, broker, and instrument authority. Every manifest records
`live_authority=false`; it can never satisfy `EXECUTION_ENGINE_ALLOW_LIVE`.
Only synchronized portfolio authority fixes `data_use_scope=paper_forward`;
the ordinary single-instrument contract keeps its existing data-use semantics.

`model_scope=single_instrument` binds one configured canonical instrument.
`model_scope=synchronized_portfolio` instead requires the pre-start model
configuration digest and one reviewed, hash-pinned positive instrument-id/
canonical-symbol allowlist artifact. The manifest embeds that exact allowlist
and its canonical digest. Its entitlement owner must equal the indicator panel runtime owner.
Scoring rejects independent per-symbol dispatch under portfolio authority and
accepts only the matching atomic model rebalance, exact active binding, account,
broker, asset class, configuration digest, and allowlist digest. Every event leg
must be a member of the allowlist, including risk-reducing exits for retained
holdings, while a zero-leg all-cash decision remains valid. A binding must list
exactly that broker and full allowlist; wildcard or subset bindings do not
inherit promotion authority.

No manifest is created from unit-test output. The referenced evidence files must
belong to one run and pass the real-data, restart, order lifecycle, exact-fill,
reconciliation, scoring-input, transport, authorization, account-binding, and
soak contracts before the writer will emit a manifest.

## Disabled synchronized equity portfolio route

`USQualityCompounder` is packaged for static validation only. Its
source configuration remains `enabled=false`, `environments=["dev"]`, and
`market_data.source="eodhd"`; do not add it to
`STRATEGY_LIST` or create a binding until its provider and evidence gates in
[the strategy README](../strategies/indicator/USQualityCompounder/README.md) pass.

The synchronized runtime is not configured from a ticker list. An authorized
provider must first persist and register one complete revision-pinned
point-in-time panel, including official decision/execution sessions, membership,
permanent security/share-class identity, observations, factor snapshots,
entitlement, and provider-policy hashes. Historical-validation evidence cannot
be selected for a `paper_forward` panel. Scoring accepts operational rebalance
batches only for `paper_forward`, and execution remains paper-only with
`EXECUTION_ENGINE_ALLOW_LIVE=false`.

The market-data entrypoint in the platform image owns the transactional research import
boundary; no additional service or image is declared. The factor materializer
emits exactly one provider-neutral symbolic `database_evidence_bundle` in its
content-addressed manifest. `sp500-research-import` requires
`SP500_RESEARCH_ARTIFACT_ROOT`, `SP500_RESEARCH_FACTOR_MANIFEST`, and
`SP500_RESEARCH_OWNER_USER_ID`. Research instrument numbers are never copied
into the catalogue: the importer resolves
permanent security keys locally, derives DB identities and hashes, admits only
exact immutable replays, and rolls back the whole graph on any failure.

The strategy process runs within the selected worker group. Promotion requires
aggregate CPU/memory, database-pool, child-health and service-SLO qualification.
Select `equity` in `PLATFORM_WORKERS` for a separate equity interpreter in the
same container; each ingestor process owns one configured source.

Do not represent this command as a successful import for the current EODHD
six-year reconstruction. Its component-history rows contain effective dates
but no source publication timestamps, so the importer intentionally rejects
them before writing. The emitted bundle records membership availability as
missing and is therefore a deterministic negative promotion artifact, not an
E2E success artifact. A
future source-qualified bundle may be imported through the declared container
with the artifact root mounted read-only; changing a scope label or using
retrieval time as historical availability is not permitted.

The explicitly selected equity ingestor can additionally persist EODHD US
extended delayed quotes for paper execution. Set
`EODHD_DELAYED_QUOTES_ENABLED=true` only with the exact active personal owner in
`SP500_RESEARCH_OWNER_USER_ID`; the delayed batch is restricted to the
catalogue-resolved `EQUITY_INGESTOR_SYMBOLS`. Every batch requires a complete,
positive, non-crossed timestamped BBO and is stored in the immutable equity
evidence graph. The ingestion adapter retains the EODHD-specific response and
source-digest semantics while publishing the normalized owner-scoped
delayed-BBO contract. The execution engine reads only that generic contract for
the exact owner in paper mode, using
`EXECUTION_MAX_DELAYED_PAPER_MARKET_DATA_AGE_SECONDS`; it has no EODHD import or
credential, and live execution never falls back to delayed evidence.

The EODHD live ingestor treats HTTP 402 as daily-call exhaustion, distinct
from HTTP 401 authentication and HTTP 403 plan-entitlement failures. It opens
a provider circuit through the next documented midnight-UTC subscription reset
and for at least `EODHD_DAILY_QUOTA_MIN_COOLDOWN_SEC` (default 900; strict range
60–86400), marks readiness false, and exposes a counter plus open-circuit
gauge. Poll ticks continue without issuing EODHD requests while the circuit is
open. This containment applies only to the long-running live ingestor; the
one-shot historical backfill and validation commands still fail closed.

Delayed quotes and daily bars have independent request cadences inside this
same ingestor process. `EQUITY_INGESTOR_POLL_INTERVAL_SEC` controls the delayed
quote loop (default 300 seconds), while
`EQUITY_INGESTOR_CANDLE_POLL_INTERVAL_SEC` controls symbol-by-symbol daily-bar
requests (default 3,600 seconds and never lower than the quote cadence). This
prevents a 500-name delayed-quote universe from refetching unchanged daily bars
on every quote tick; both paths still share the credential-safe quota circuit,
catalogue identity, and readiness boundary.

The platform image provides the existing `quality-compounder-once` entrypoint.
Invoke it through `python -m scripts.run_platform job quality-compounder` with
`compose exec` in the existing worker group. The launcher bounds time and
prevents same-job overlap; no scheduler or panel container is declared. An
operator selects the run after the final official XNYS quarter session. The
command rechecks the consecutive-session boundary and requires
`QUALITY_COMPOUNDER_PANELS_ENABLED=true`.

An enabled run requires `EODHD_API_TOKEN`, `EDGAR_USER_AGENT`, the exact personal
data owner in `QUALITY_COMPOUNDER_ENTITLEMENT_OWNER_USER_ID`, and a reviewed
`QUALITY_COMPOUNDER_ROUND_TRIP_COMMISSION_BPS`. All provider requests finish
outside database transactions. Membership, identity, EOD prices, splits,
dividends, SEC facts, derived market/fundamental factors, the evidence manifest,
and the synchronized panel then commit atomically before the next official
open. Any missing member, catalogue/conid gap, stale or mixed-scope evidence,
corporate-action ambiguity, quota failure, or deadline breach rolls back the
attempt and fails closed.

Panel registration requires factor-complete coverage of at least 80% across
all effective members and at least 70% in every sector with 10 or more
effective members. Deterministic diagnostics report exact complete/total
counts; incomplete and ineligible members do not count as complete.

The command targets only the registered, active model version
`us_quality_compounder_v1` / `0.2.0`, point-in-time S&P 500 membership, and the
catalogued SPY ETF benchmark. The strategy catalogue row may remain inactive
while this evidence is produced; the command does not activate the strategy,
create an account binding, infer settled USD or NAV, or grant broker/order
authority.

Before the first panel run, build the validation runtime and compile the pinned
ICE/NYSE artifact with
`build/venvs/strategy-validation/bin/python -m dev_cli.validation.backtest.equity_run
compile-official-sessions`. Record its printed SHA-256, mount the artifact read-only, and invoke
`quality-compounder-calendar-import`. The import requires exact
`QUALITY_COMPOUNDER_OFFICIAL_SESSION_ARTIFACT` and
`QUALITY_COMPOUNDER_OFFICIAL_SESSION_SHA256` values and accepts only canonical
compiler bytes; an incompatible existing XNYS calendar fails closed.

Provision equities through a separately reviewed maintenance invocation of the generic
`equity-catalogue-import` command. Its content-pinned canonical JSON must be
sorted by canonical symbol and provide each exact USD scheduled equity,
reviewed exchange and tick size, reviewed positive whole-share `lot_size`, and
positive IBKR `STK` conId. It performs no provider symbol search and never
creates a calendar: the official `XNYS` calendar and seeded `ibkr` broker must
already exist. It creates missing rows or fills only null catalogue fields;
any conflicting non-null value or conId assignment fails the transaction.

Set `EQUITY_CATALOGUE_ARTIFACT` and `EQUITY_CATALOGUE_SHA256` for the existing
market-data entrypoint. This is a maintenance import, not a Compose service or
normal bootstrap seed. Use the reviewed maintenance role/stage described in
[DATABASE.md](DATABASE.md); runtime market-data credentials do not grant catalogue
DDL or owner designation. `EQUITY_CATALOGUE_DRY_RUN` defaults to `true`; review
the deterministic plan before an explicit atomic apply. No import command here
constitutes provider-data or PostgreSQL acceptance evidence.

## Venue candle feeds

The live scheduler and execution engine keep canonical instrument identity
separate from each venue's request identifier. `INGESTOR_SYMBOLS` contains
canonical instrument names or configured aliases. At startup, each selector
resolves through `instruments` and the exact broker row in
`instrument_broker_symbols`; only the canonical symbol is emitted in
`new_market_data` notifications.

Text-symbol venues use `broker_symbol`. IBKR, Zerodha, and Saxo require the
separate `broker_instrument_id`; Saxo also requires
`broker_instrument_type`. These typed fields hold conids, instrument tokens,
and UIC/AssetType without encoding them into a display symbol or an environment
variable. Unknown or duplicate selectors, missing broker rows, incomplete
typed identities, and non-positive numeric ids fail startup before any venue
request. Catalogue changes are therefore reviewed and shared with execution
routing instead of maintained in a parallel ingestor configuration.
Untyped opaque ids such as IBKR conids and Zerodha instrument tokens are unique
per broker. Typed ids remain unique by broker/id/type because Saxo may reuse a
UIC across AssetTypes.

| `INGESTOR_SOURCE` | Venue identifier and credentials |
|---|---|
| `coinbase_live` | Coinbase product id; public candles or optional key/secret |
| `deribit`, `deribit_testnet` | Exact instrument name; public chart endpoint |
| `delta`, `delta_india` | Exact regional product symbol; public candles |
| `ibkr` | Positive numeric conid; an authenticated Client Portal Gateway session |
| `zerodha` | Positive numeric instrument token; API key plus current daily access token |
| `saxo_live` | Positive numeric UIC plus exact typed AssetType; OAuth token, RFC3339 expiry, and optional AccountKey |
| `saxo_simulation` | Same explicit Saxo mapping and credentials, persisted only as simulation provenance |

Authenticated venue feeds use dedicated platform market-data credentials
(`ZERODHA_MARKET_DATA_*` or `SAXO_MARKET_DATA_*`). They must not reuse the owner's
account-scoped execution secret.

The live poller and the explicit bounded `run_platform job backfill` share this
exact source/catalogue/provider boundary. `INGESTOR_SYMBOLS` always selects
canonical instruments; the job resolves the same broker symbol, conid,
instrument token, or UIC/AssetType and persists only the provider's intrinsic
source tag. It never fetches Coinbase data on behalf of another source or
rewrites provenance. Public Deribit and Delta feeds need no credential;
Coinbase credentials are optional, IBKR requires an authenticated gateway,
and Zerodha/Saxo use the dedicated feed credentials above. Missing identity,
credential, coverage, or recent venue data fails the job.

`INGESTOR_BACKFILL_MINUTES` belongs to the live poller's bounded startup
request. `INGESTOR_BACKFILL_DAYS` belongs to the explicit historical job run with
`python -m scripts.run_platform job backfill --timeout-seconds 3600` through
`compose exec -T workers` (`application` in the `all` layout). It has its own
process and connection pool within the existing group; host CPU, memory and
database capacity are still shared with live polling. There is no backfill
container profile or hidden scheduler. Continuous crypto history requires 95% total
coverage and a current tail. Session-based assets use a conservative 15%
wall-clock floor plus a candle within the last seven days so nights, weekends,
and holidays are not misclassified as venue failures; the execution session
gate remains authoritative for tradability. Network/HTTP failures are retried,
but a successful empty scheduled-market response defers directly to those
aggregate gates instead of retrying every overnight/weekend window.

## Official market-session writers

IBKR, Saxo, and Zerodha non-crypto routes remain blocked until one supervised
writer owns each canonical instrument. Select these processes through
`PLATFORM_WORKERS` within the existing worker group:

| Worker selection | Official source | Required configuration |
|---|---|---|
| `calendar-ibkr` | IBKR `GET /contract/trading-schedule` regular `liquid_hours` | `IBKR_MARKET_CALENDAR_SYMBOLS`, authenticated `IBKR_GATEWAY_URL`, and a pinned `IBKR_CA_CERT` for non-loopback gateways |
| `calendar-saxo` | Saxo live trading schedule; `AutomatedTrading` only | `SAXO_MARKET_CALENDAR_SYMBOLS`, current `SAXO_MARKET_DATA_ACCESS_TOKEN`, and its RFC3339 expiry |
| `calendar-zerodha` | NSE `/api/marketStatus` exact segment state | `ZERODHA_MARKET_CALENDAR_SYMBOLS`, exact `ZERODHA_MARKET_CALENDAR_NSE_MARKET`, 30–300 second lease |

Selector variables contain canonical symbols only. Exact conids, UIC/AssetType
pairs, and Kite tokens come from the shared broker catalogue. The processes use
the market-data database role to read those identities and
`MARKET_CALENDAR_ADMIN_API_KEY` (in Compose, the existing
`BACKEND_ADMIN_API_KEY`) only for the backend PUT. Never enable two writers for
the same instrument. Empty selectors, incomplete mappings, expired credentials,
malformed responses, and source outages fail closed; no fallback schedule is
generated.

There are no provider aliases or first-search-result fallbacks. IBKR requests
use the exact UTC start and forward direction. A non-loopback Client Portal
Gateway must use HTTPS and set `IBKR_CA_CERT`; TLS verification is disabled
only for the gateway's self-signed loopback default. Zerodha request bounds
are converted explicitly to Indian-exchange wall time and returned offset
timestamps are persisted as UTC. Saxo live and simulation use different API
hosts and immutable source labels; Saxo documents that simulation market data
may be unavailable or synthetic, so it is not an acceptable real-data
validation source. A `saxo_live` response is accepted only when Saxo reports
`DelayedByMinutes=0`. Bid/ask chart shapes (including Saxo FX) are rejected
because the canonical candle schema represents last-trade OHLC and does not
silently manufacture midpoint candles.

## Observed FX process

The selected `fx` worker runs in its own interpreter, independently from the
crypto-candle scheduler. It stores daily `EUR/USD`, `EUR/GBP`, and `EUR/INR`
reference observations from the ECB and hourly traded `USDC/EUR` candles from
Coinbase. Crosses are derived at read time through EUR with both source legs
fresh at the requested timestamp.

| Variable | Default | Owner / constraint |
|---|---:|---|
| `FX_RATE_CURRENCIES` | `USD,GBP,INR` | FX ingestor; non-EUR ECB quote currencies required by the owner accounts |
| `FX_RATE_HISTORY_DAYS` | `90` | FX ingestor; accepted range `1..90` |
| `FX_RATE_POLL_INTERVAL_SEC` | `21600` | FX ingestor; accepted range `60..86400` |
| `FX_RATE_COINBASE_PRODUCT` | `USDC-EUR` | FX ingestor; intentionally rejects any other product |
| `EXECUTION_FX_MAX_AGE_SECONDS` | `259200` | execution; accepted range `60..604800` |

The FX process exposes internal `/health`, `/ready`, and `/live` endpoints on
port `8004`. The platform supervisor tracks it separately so FX-source failure
is visible independently of primary market-data ingestion.

No hosting or public authentication provider is provisioned. The owner API is
bound to loopback; use an owner-controlled SSH tunnel for a private remote host.
