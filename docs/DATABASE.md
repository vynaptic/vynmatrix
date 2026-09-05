# Database Reference (PostgreSQL)

This is the canonical database installation, migration, reference-data, and owner
lifecycle guide. The schema source is
[`lib_application.db.models`](../libs/python/lib_application/lib_application/db/models/).
Use PostgreSQL 16 for the supported stack; SQLite is limited to unit tests.

## Installation and privilege stages

Prepare the tooling environment and build the platform image using the applicable
[macOS/Linux](../SETUP_MAC_LINUX.md) or [Windows](../SETUP_WINDOWS.md) setup guide.
Configure the explicit URLs and secrets in [.env.example](../.env.example); keep
`EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`.

| Stage | Identity | Commit boundary |
| --- | --- | --- |
| Database creation/ownership checks | `ADMIN_DATABASE_URL`, database `postgres` | CREATE DATABASE autocommit |
| Alembic and owner initialization | `MIGRATION_DATABASE_URL`, target database | Separate migration and owner transactions |
| Runtime login provisioning | Explicit administrator, target database | All six logins atomically |
| Initial static references | Verified maintenance/schema owner | Whole catalogue transaction |
| Explicit development-canary activation | `MIGRATION_DATABASE_URL`, designated owner required | Exact release and audit atomically |
| Later reference/profile/account changes | `BACKEND_DATABASE_URL`, `vm_backend_login` | One validated request/batch |

Fresh history includes `0039` and `0052` role DDL. PostgreSQL requires a superuser
even for their `ALTER ROLE ... NOSUPERUSER` statements. The supported fresh path
therefore uses the explicitly supplied maintenance administrator as schema owner:
`ADMIN_DATABASE_URL` and `MIGRATION_DATABASE_URL` normally use the same login and
password, targeting `postgres` and the application database respectively. A distinct
maintenance login must already have the required administrator authority; bootstrap
never elevates or invents it. This requirement comes from
[PostgreSQL's role implementation](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/commands/user.c#L718).
Maintenance credentials are absent from application and worker environments.

The six runtime logins each inherit only their matching `vm_*` service group.
`vm_backend_login` owns the routine CLI/control-plane writes; it cannot designate
an owner, change identity columns, or update ledger history. PostgreSQL roles are
cluster-wide: this fixed role inventory is for one deployment per cluster.

Create a private `owner.local.yaml` with your actual profile. This schema example
contains placeholders that must be replaced:

```yaml
profile:
  email: "<your email>"
  base_ccy: "<your accounting currency>"
  tz: "<your IANA timezone>"
```

Then run the same command on macOS, Linux, or PowerShell:

```text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
```

Bootstrap validates input and source references before stopping runtime groups. It
waits for application/workers to stop, verifies that only PostgreSQL remains,
starts PostgreSQL if necessary, runs the declared `bootstrap` job, removes that
job, and then starts the chosen runtime groups. Use `--no-start-runtime` to finish
maintenance without starting the platform. Neither `--profile '*'` nor arbitrary
Compose jobs are supported lifecycle entry points.

The job verifies database ownership/settings, applies the complete Alembic history,
verifies head, creates missing runtime logins, registers static references, and
initializes the explicit owner in separate resumable stages. Database creation is
outside a transaction as required by
[PostgreSQL](https://www.postgresql.org/docs/16/sql-createdatabase.html).
A migration/catalogue/onboarding failure leaves runtime stopped and retains the
completed stages. It never drops/reset-stamps the database or runs demonstration SQL.

Repeat bootstrap with `profile: {}` to verify the existing designation without
reapplying old profile values. Supplied values on a repeated initialization must
match; updates are a separate operation. Existing users require explicit
`existing_user_id` in the owner document (or `vmdev user init --existing-user-id`).
This is the sole owner-identity selection exception, under maintenance authority.
No oldest-user, email-domain, or first-account heuristic is used.

Registration creates inactive strategies and `registered` versions. It creates no
broker accounts, credentials, bindings, execution selection, risk mandate, or market
observations. Source presence is never execution authority.

The optional `vmdev db activate-canary --strategy-id ID --version SEMVER` command
requires a host-reachable maintenance URL and explicit `ENVIRONMENT=dev`,
`EXECUTION_MODE=paper`, and `EXECUTION_ENGINE_ALLOW_LIVE=false`. The exact registered
source release must be enabled, restricted to `dev`, and carry the existing
`E2E_PIPELINE_CANARY_ONLY` governance decision. It transitions that release and its
parent strategy to active in one audited transaction; identical repeats are no-ops.
It refuses `deprecated`/`pulled` releases and disabled parents of already-active
releases. It creates no account, binding, runtime selection, or attestation.
Swing's permanent exclusion from paper promotion/live remains intact. Follow the
[recorded-data verification guide](E2E_VERIFICATION_GUIDE.md) for the separate
account, data and execution authority requirements.

## Routine configuration changes

The database is authoritative for installed owner settings. Source-controlled
`config/instruments.yaml`, `config/brokers.yaml`, and each existing strategy's
`config.json` describe reviewed non-secret references. Adapter capabilities still
come from the implemented broker specifications. Stable keys are strategy ID and
semver, broker code/environment/region, explicit instrument identity, and the
owner's unique account `config_key`; numeric IDs are preserved.

Routine host CLI writes require an explicitly supplied `BACKEND_DATABASE_URL` reachable
from the host (normally the published loopback PostgreSQL port). Container URLs
use `postgres`; do not copy a container hostname into a host connection or expose
PostgreSQL publicly. Supply secrets through your environment mechanism, never as
command arguments. The platform image also includes `python -m dev_cli.main` for
`exec` use in an existing container.

```text
vmdev db catalogue --check
vmdev db catalogue --apply
vmdev db catalogue --check --strategy-id <existing-source-strategy-id>
vmdev db catalogue --apply --broker-code <implemented-broker-code>
vmdev db catalogue --apply --changes reviewed-changes.yaml
vmdev user show
vmdev user update --config owner-update.yaml
vmdev user account --config account.yaml --secrets-file protected-credentials.json
```

`--check` reads/validates without sequence allocation, audits, or writes. Normal
`--apply` creates missing rows only. Different installed fields are conflicts;
absent fields/records never mean overwrite/delete. Existing `registered`, `active`,
`deprecated`, and `pulled` release states are preserved. Release payload changes
require a new semver; catalogue updates cannot activate a strategy/version.

Explicit patches use a list of `{kind, key, expected, changes}` objects. Each changed
field needs its expected current value. Allowed edits are broker display/capability
metadata (bounded by implemented capabilities), broker environment URLs/rate limits,
and sector name/description. Instrument identity, financial contract terms,
tradability, currency and session authority require explicit recataloguing; they
are not routine metadata patches. URLs cannot embed credentials or query secrets.
Identical acknowledged patches are no-ops; conflicting updates fail without partial
writes. A complete batch, its fixed SQL function calls, and audits share one
transaction and catalogue advisory lock. Bounded retries re-read stable keys/desired
state after serialization/deadlock/connection failures, including uncertain commits.

Owner profile patches and stable-key account patches also use `expected` and
`changes`. Account adoption explicitly names an existing account ID belonging to
the designated owner. Account currency, external identity and opening capital
cannot change after relevant activity; database guards use the execution account
exclusion lock. Credentials use a protected file/stdin and existing encrypted
`set_secret(..., session=...)`; pointer, ciphertext, account and audit changes
commit together. Rotation remains an explicit operation through the owner API.
On platforms without POSIX owner-only file checks, including Windows, use
`--secrets-file -` with input redirected from a protected source; the CLI refuses
to assume that POSIX mode bits establish Windows file protection.

Repeat `vmdev db roles` verifies supplied existing passwords without replacing them.
`vmdev db roles --rotate` is the explicit six-password rotation transaction; stop
runtime and update all six connection URLs before restarting. Back up the external
secret-key ring separately; a database dump cannot recreate it.

## Existing databases, migrations and rollback

The current linear head is `0104_saxo_capability_flags`; verify it from
`scripts/db/alembic/versions/`, not a copied command result. Revisions `0099`–`0104`
add owner designation, safe registrations/reference functions, control-plane guards,
and guarded commercial-table removal. Historical IDs and migrations remain intact.
`0104` completes only the exact historical Saxo capability document with two
explicit false flags. Customized documents remain unchanged and require reviewed
reconciliation; neither flag grants execution or live certification authority.

For a database whose current schema owner differs from the fresh-install convention,
configure that exact maintenance identity and use `vmdev db migrate` for schema-only
upgrade. It stops runtime, verifies ownership, acquires the migration lock, and runs
Alembic; it neither provisions a new owner nor seeds data. Earlier role migrations
still require the explicit administrator authority described above. Do not silently
transfer object ownership or change role attributes to make preflight pass.

Take a protected archive and validate a restore before an existing-data upgrade.
Inventory pending outbox commands, nonterminal orders/rebalance plans and canonical
ledger exposure for all preserved users. Owner adoption blocks unresolved foreign
work and exposure. Non-owner non-SPOT history requires explicit reconciliation/
disposition; no speculative derivative accounting or position-projection shortcut
is used. Historical records of the selected owner remain recoverable.

`0103` refuses **before DDL** if `orgs`, `plans`, `user_roles`,
`user_plan_subscriptions` contain rows or `users.org_id` is non-null. Resolve and
record disposition/export deliberately; the migration never deletes populated
commercial data. It preserves all user/account identifiers and trading history.
Its downgrade recreates empty commercial structures. Earlier downgrades refuse
loss of a configured owner, stable account keys, or registered versions rather than
rewriting active state. Do not assume downgrade is a data rollback: restore the
matched code/database snapshot when a guarded downgrade cannot preserve semantics.

```text
vmdev db backup backups/pre-upgrade.dump
vmdev db migrate
vmdev db restore backups/pre-upgrade.dump
```

Backup streams a new mode-0600 PostgreSQL custom archive through the existing
PostgreSQL container; it refuses to overwrite a file. Restore confirms the explicit
target, stops runtime, restores transactionally with the supplied maintenance owner
and preserved grants, and leaves runtime stopped for verification. The target and
required cluster roles must already exist. Keep archives off-host according to your
recovery needs. Neither operation creates a fourth container.

`vmdev db backup`, `restore`, and `connect` execute PostgreSQL utilities inside
the existing PostgreSQL container. Their `MIGRATION_DATABASE_URL` must therefore
target `postgres:5432`, even when the host publishes a different loopback port.
In contrast, `vmdev db migrate` and `activate-canary` connect from the host and
require a host-reachable `MIGRATION_DATABASE_URL`; routine owner/catalogue writes
use a host-reachable `BACKEND_DATABASE_URL`. Scope the appropriate URL through
your secret environment mechanism for each operation; keep the same intended
database and role, and never put credentials in command arguments.

## Schema Overview (tables in `libs/python/lib_application/lib_application/db/models/`)

### Active Pipeline Tables (used in the Strategy → Scoring → Execution → Feedback flow)

| Table | Pipeline Stage | Description |
|-------|---------------|-------------|
| `canonical_signals` | Scoring | Normalized indicator signals with run_id correlation |
| `asset_scores` | Scoring | Per-instrument aggregated scores |
| `market_scores` | Scoring | Market-level aggregated scores |
| `sector_scores` | Scoring | Sector-level scores (when sectors_allowed configured) |
| `user_strategy_bindings` | Scoring | User binding config: thresholds, mode, autopilot, strategy scoping |
| `strategy_runtime_states` | Market data→Strategy | Latest versioned shared-model snapshot and exact source watermark for one indicator worker partition; never a tenant broker position |
| `strategy_decisions` | Strategy | Append-only per-bar decision journal with exact price revision and zero-or-more stable signal envelopes |
| `strategy_panel_input_revisions` | Market data→Strategy | Immutable, provider-authorized synchronized panel payload registered before strategy evaluation |
| `strategy_panel_decisions` | Strategy | Prepared/completed synchronized-panel journal binding immutable inputs, state generation, rank, audit, signal envelopes, and correction/replay disposition |
| `equity_rank_snapshots`, `equity_rank_snapshot_rows` | Strategy | Append-only complete cross-sectional ranking and per-member decision/evidence ledger |
| `model_rebalances`, `model_rebalance_legs` | Strategy→Scoring | Immutable tenant-neutral portfolio decision and ordered exit/target legs derived from one completed panel/rank |
| `account_rebalance_plans`, `account_rebalance_plan_legs` | Scoring→Execution | Frozen user/binding/broker-account paper plan, one-way terminal targets, and phased execution/restart state sealed with its account writer generation |
| `account_execution_generations` | Execution | Monotonic user/account writer generation and active owner audit; PostgreSQL advisory locking is the crash-released serialization authority |
| `account_rebalance_plan_resolutions` | Execution operations | Immutable, operator-attributed acknowledgement/reconciliation/remediation lineage for terminal failed plans; execution may append but never update or delete |
| `execution_decision_logs` | Scoring→Execution | Audit trail of binding decisions with exact canonical-signal and broker-account lineage for v1 decisions |
| `outbox_events` | Cross-service delivery | Transactional internal event queue with stable event/ordering keys, leases, retry classification, dead-letter state, and generation-fenced redrive audit |
| `pending_orders` | Execution recovery | Recoverable order state including client identity, submission uncertainty, trigger/limit, reduce-only, parent/OCO, cumulative fill, real-market-data watermark, and the exact canonical execution awaiting projection |
| `order_intents`, `orders`, `executions` | Execution ledger | Canonical request, broker-order, and exact-fill chain used for accounting and feedback attribution |
| `execution_logs` | Execution | Trade execution outcomes with full order details (JSON) |
| `execution_metrics` | Execution | Per-execution observability/performance snapshots, including exact FIFO realized-contribution lineage for mode ranking; never paper restart accounting truth |
| `positions` | Execution | Current open-position projection, repaired from canonical fills for local-paper restart |
| `daily_nav` | Execution | Daily account-native NAV snapshots, unique per owned broker account and date |
| `risk_breaches` | Execution/Risk | User-owned circuit-breaker, reconciliation, and risk-guard audit trail; linked-account identity is required when the execution context has one |
| `signal_performance` | Feedback | Effectively-once prediction evaluation, unique per canonical signal and declared horizon |
| `strategy_consecutive_wrong_tracker` | Feedback | Consecutive wrong prediction counter per strategy/instrument |
| `strategy_parameter_feedback` | Feedback | Auditable optimization suggestions and review decisions |

#### Signal action values

`canonical_signals.action` stores the lowercase form, which is **not** the enum name. Convert
with `normalize_scoring_action` from
`libs/python/lib_strategy/lib_strategy/signals/normalization.py` rather than lowercasing by
hand — `CLOSE` maps to `flat`, not `close`, and a hand-rolled conversion violates the CHECK
constraint.

| `SignalAction` | Stored value |
|---|---|
| `LONG` | `long` |
| `SHORT` | `short` |
| `CLOSE` | `flat` |
| `HOLD` | `hold` |

The scoring vocabulary additionally admits `open_spread` and `close_spread`.

#### Binding mode selection

`user_strategy_bindings.mode_selection_policy` is constrained to `fixed`, `best_return`,
`lowest_risk`, `highest_sharpe`, or `user_rotating`. **The column default is `fixed`.**

Ranking applies only when the resolved execution mode token is `best`/`auto`; otherwise the
binding's explicit mode is used. Candidate `mode_performance` rows are filtered to the
binding's `execution_modes_allowed` first, then ranked:

- `best_return` → highest historical `total_return`
- `lowest_risk` → lowest historical `max_drawdown`
- `highest_sharpe` → highest Sharpe, tie-broken by `total_return`; this is also the in-code
  fallback for any unrecognised policy, including `user_rotating`

When no permitted mode has performance data it falls back to `preferred_mode`, or to the
first permitted mode when `preferred_mode` is unset. Ranking lives in the pure
`_rank_mode_performance` helper — there is no separate `ModeSelector`/`ModeOptimizer` service.
Note that `execution_mode` is not an ORM column; it exists only on the scoring DTO
`ScoringUserBinding`. Entry authority is gated by `entries_enabled`/`exits_enabled`.
| `prices` | Market data / Feedback / Accounting | Persisted OHLCV with content revisions, including point-in-time ECB and Coinbase FX observations used for currency conversion |
| `watermarks` | Market data | Source/instrument ingestion checkpoints plus generation-fenced historical-rebuild requests |

### Supporting Tables (config/reference data)

- **Owner and retained historical identities:** users, user_consents, suitability_questionnaires, user_suitability_responses, feature_flags, user_feature_flags.
- **Broker Connectivity:** brokers, broker_environments, linked_broker_accounts, broker_credentials, managed_secrets.
- **Instruments & Mapping:** instruments, instrument_aliases,
  instrument_broker_symbols, instrument_sectors, sectors, market_calendars,
  market_sessions, corporate_actions, index_membership, earnings_events, and
  equity_security_identities. Broker mappings keep display symbols separate from opaque
  venue IDs and venue types. Untyped IDs are unique within one broker; typed
  IDs are unique by broker/ID/type so no venue series can map to two canonical
  instruments. Persisted `asset_class` uses exactly `crypto`, `equity`, `etf`,
  `index`, `futures`, `options`, `fx`, or `commodities`. `Instrument.is_tradable`
  is an explicit execution boundary: cash indices are always reference-only,
  while their executable futures and options are separate canonical
  instruments with separate venue identities.
- **Point-in-time Equity Evidence:** equity_source_lineages,
  equity_observations, equity_observation_values, equity_factor_snapshots,
  equity_factor_snapshot_details, and equity_factor_evidence. These tables are
  append-only and bind provider/product/version/retrieval/timestamp,
  adjustment/missing-data, entitlement, revision, availability, and content
  identity. Historical-validation authority cannot satisfy a paper- or
  live-forward panel.
  A verified one-shot importer in the platform image can
  translate a content-addressed symbolic research bundle into this DB graph,
  deriving identities from local instrument IDs and accepting exact immutable
  replays. Missing source-backed membership availability fails before writes;
  effective dates and retrieval time are never substituted.
- **Strategies:** strategies, strategy_versions (immutable release image,
  source revision, parameter schema, and version-specific `default_params`
  snapshot used by feedback), user_strategy_configs, scoring_rules,
  sizing_profiles.
- **User Control Plane:** user_trading_policies, user_budget_buckets.
- **Options & OMS:** option_strategy_templates, option_strategy_template_legs,
  user_option_presets, order_intents, orders, child_orders, option_order_legs,
  executions, positions, daily_nav. Order intents own relational
  user/account/strategy/signal/side/mode attribution; append-only exact venue
  fills are the FIFO P&L and strategy-metrics source. Each execution requires
  the venue trade ID, actual fill timestamp, quantity, price, fee amount, and
  fee currency; cumulative order status never synthesizes a row.
  `pending_orders` is the durable lifecycle/reconciliation projection, not a
  parallel accounting ledger. Local-paper delayed fills retain exact source
  price ID, revision, timestamp, source, timeframe, and trigger-policy
  provenance. A fill also retains `pending_projection_exec_id` until its
  idempotent execution metric, position, and NAV projections complete; the
  lifecycle drains this checkpoint before consuming another candle. Option legs
  and fills remain canonical order data; there is no parallel fixed-leg
  position tracker.
- **Risk & Audit:** risk_mandates, risk_breaches, api_audit_logs, user_notifications, outbound_webhooks. Risk mandates are either global (`user_id IS NULL`) or attached by foreign key to one real user. Every breach has a real user foreign key and, when account-attributable, a composite-validated `broker_account_id`; no hashed or polymorphic owner identifier remains.

For the full E2E pipeline verification with real data, see [E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md).

### Table lifecycle: pending expand/contract drops (owner decision 2026-07-29)

Thirteen tables have no application read/write path and are being removed via
**expand/contract**: application and seed references were removed first
(2026-07-30); the ORM models and schema stay until a deploy + soak confirms no
unexpected rows exist in production (`SELECT COUNT(*)` each, export anything
non-empty), and only then does a later release delete the models together with
the drop migration and RLS policies. Dropping:
`user_consents`, `suitability_questionnaires`, `user_suitability_responses`,
`user_notifications`, `outbound_webhooks`, `feature_flags`,
`user_feature_flags`, `user_budget_buckets`, and the options-order family
`child_orders`, `option_order_legs`, `option_strategy_templates`,
`option_strategy_template_legs`, `user_option_presets` — the owner-reviewed
verdict is that these OMS shapes are unsuitable for real venue multi-leg
execution (no per-leg venue contract identity/conid, ratio, or fill lineage;
`child_orders` models slicing, not legs; presets overlap versioned strategy
configuration). Replacement shapes are designed inside the options-strategy
change against real IBKR combo semantics. **Retained deliberately:**
`index_membership` — the active SP500 rotation change is its named consumer.

## Reference and Market Data

### Observed currency rates

Currency conversion uses the same revision-tracked `prices` store rather than a
constant or a stablecoin-parity assumption. The independently supervised
`fx-rate-ingestor` writes:

- ECB `EUR/USD`, `EUR/GBP`, and `EUR/INR` business-day reference observations
  at `1d` with source `ecb_reference`; and
- Coinbase `USDC/EUR` traded candles at `1h` with source `coinbase_live`.

Execution reads the newest eligible observation at or before the accounting
timestamp. Direct or inverse pairs are preferred; one cross through EUR is
allowed only when both observed legs satisfy the configured freshness window.
Missing or stale conversions block the affected balance, position, P&L, or NAV
calculation.

Select `fx` in `PLATFORM_WORKERS` to refresh these observations in the workers
container (or the combined application). The process exposes port `8004` internally.
Provider failure remains separate from crypto-candle ingestion. Bounded historical
replay and provenance checks are described once in
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md).

### Authoritative market sessions

Every instrument persists one execution policy:

- `crypto` instruments are explicitly `continuous` and cannot reference a
  calendar; and
- every non-crypto instrument is `scheduled` and remains fail-closed until it
  references a synchronized `market_calendars` row.

`market_calendars` records the official broker/exchange provider, HTTPS source
reference, observation time, and a complete UTC coverage interval.
`market_sessions` records only the open regular-session intervals within that
coverage. A covered instant outside every interval is authoritatively closed;
missing, stale, future-dated, or out-of-coverage data is unavailable and blocks
new exposure. The admin-authenticated backend
`PUT /market-calendars/{code}` replaces the coverage, intervals, and exact
instrument assignment in one transaction. Instruments omitted from a
replacement are detached and fail closed. `CLOSE` signals remain eligible for
risk reduction.

The market-data image provides three independently supervised, opt-in writers:
`calendar-ibkr` consumes Client Portal regular `liquid_hours`,
`calendar-saxo` consumes live OpenAPI `AutomatedTrading` states, and
`calendar-zerodha` consumes NSE's exact current market state. The Zerodha path
uses a 30–300 second official-state lease because Kite exposes no authoritative
future schedule; it never manufactures a weekday template. Each writer resolves
the configured canonical symbols through `instrument_broker_symbols`, validates
every provider response before its first write, and calls the backend API above.
An empty selector, incomplete typed mapping, expired credential, stale NSE open
date, or source/API failure fails closed and lets prior coverage age out.

Enable exactly one calendar writer for a given instrument through `PLATFORM_WORKERS`:
`calendar-ibkr`, `calendar-saxo`, or `calendar-zerodha`. Their internal ports are
`8005`, `8006`, and `8008`. Configure the corresponding explicit symbol selectors,
provider credentials, and typed mappings. A gateway dependency is described in
[DEPLOYMENT.md](DEPLOYMENT.md); it does not disappear from the container budget.

The `backfill` job uses the existing market-data implementation and real Coinbase
history. Invoke it through `scripts.run_platform job backfill` in a running workers
(or combined) container, with a timeout. It has a local exclusion lock and no
separate scheduler/container. See the E2E guide for coverage, stale-tail, duplicate
submission, ledger/FX, and historical-versus-current delivery acceptance.
Feedback reads provenance-matched persisted candles, never a second synthetic feed.

## Deployment and accounting boundaries

One active designated owner may use multiple explicitly owned broker accounts,
environments, regions, strategies and currencies. Retained non-owner IDs represent
historical attribution, never a caller-selectable new execution tenant. Row-level
policies and the transaction-local scope remain where they protect ownership.

Market sessions, provider entitlements, instrument mappings, contract multipliers,
fill-time FX and canonical execution-ledger replay remain mandatory. Positions and
metrics are projections. Normal installation cannot manufacture those authorities.

Local/cloud topology, persistence, health checks and upgrade process are canonical in
[DEPLOYMENT.md](DEPLOYMENT.md). Recorded-data acceptance is canonical in the
[E2E guide](E2E_VERIFICATION_GUIDE.md). No cloud infrastructure or release is implied
by successfully running a local bootstrap.
