# Database Reference (PostgreSQL)

Use only isolated local databases for migration verification. Inherited operational
utilities below are references, not authority to modify any existing live system or
write a live-certification marker.

Single source of truth for platform database setup, schema, migrations, and
deployment. PostgreSQL is used in every runtime environment.

## Environments & Engines

- **Local dev:** Docker PostgreSQL (`vmdev db start`).
- **Local pipeline:** the declared Compose stack bootstraps PostgreSQL 16,
  migrations, seeds, and distinct service logins. Do not start a second helper
  database on the same port.
- **Cloud/runtime migration:** no host, database, backup, or restore evidence is
  included. A future topology requires separate owner review and verification.
- **Tests:** SQLite is used in unit tests only; all runtime environments use PostgreSQL.
- **One engine everywhere** to avoid surprises and keep JSONB/ACID features consistent.

## Local Workflow (5 minutes)

**macOS/Linux:**

```bash
# Start DB in Docker
vmdev db start

# Create/upgrade schema
vmdev db init             # runs alembic upgrade head

# Status / connect
vmdev db status
vmdev db connect

# Or use manual script
./scripts/db/manage_db.sh start
./scripts/db/manage_db.sh status
```

**Windows (PowerShell):**

```powershell
# Start DB in Docker
vmdev db start

# Create/upgrade schema
vmdev db init             # runs alembic upgrade head

# Status / connect
vmdev db status
vmdev db connect

# Or use manual script
.\scripts\db\manage_db.ps1 start
.\scripts\db\manage_db.ps1 status
```

> **💡 Windows Users**: See [SETUP_WINDOWS.md](../SETUP_WINDOWS.md) for PowerShell setup and DB command parity.

### Migrations (Alembic)

- Location: `scripts/db/alembic`
- Create: `alembic revision -m "message"`
- Apply: `alembic upgrade head`
- Current linear head: `0096_model_prior_identity`
- Auto-run via `vmdev db init` or CI.

Migrations `0086_equity_factor_evidence`,
`0087_synchronized_panel_runtime`, `0088_portfolio_rebalances`,
`0089_account_execution_fence`, `0090_optional_factor_contracts`,
`0091_dated_security_symbols`, `0092_panel_owner_fence`,
`0093_factor_risk_exposure`, `0094_backend_control_plane`,
`0095_binding_total_exposure`, and `0096_model_prior_identity` form one
fail-closed equity-portfolio chain.
`0086` adds immutable observation, factor/evidence, and rank ledgers; `0087`
adds permanent security identity, registered panel-input revisions, and the
atomic panel decision/state/outbox journal; `0088` adds immutable model
rebalances and frozen tenant/account plans whose mutable lease, phase, leg
status, and audit fields support replay-safe paper execution, plus append-only
operator resolutions for terminal failed plans. Forward-only `0089` preserves
that deployed `0088` contract while adding the account execution-generation
ledger, one-way leg targets, terminal account-generation/digest seal, and
strengthened PostgreSQL guards. Forward-only `0090` adds the six disabled
optional observation kinds, provider-specific owner-bound personal
entitlements, and a frozen source-contract registry digest without rewriting
deployed evidence revisions. Forward-only `0091` adds a validity-dated
canonical symbol to permanent security identities and rejects overlapping
identity intervals, allowing ticker renames to retain one catalogue instrument
while historical panels keep the symbol effective at their cutoff. Migrations
`0092`–`0095` add owner-fenced panel runtime, factor-risk evidence, backend
activation controls, and binding-owned gross-exposure ceilings. Convergent
repair migration `0096` upgrades databases that reached the original `0088`
shape with a two-column prior-leg reference to the exact four-column
rebalance/leg/instrument/factor identity; fresh databases already have that
contract and are unchanged.
The migrations create least-privilege service
grants, row-level policies, relational ownership constraints, and
mutation-protection triggers; they do not activate a strategy or a binding.

## Schema Overview (tables in `libs/python/lib_application/lib_application/db/models/`)

### Active Pipeline Tables (used in live Strategy → Scoring → Execution → Feedback flow)

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

- **Tenancy & Users:** orgs, users, user_roles, plans, user_plan_subscriptions, user_consents, suitability_questionnaires, user_suitability_responses, feature_flags, user_feature_flags.
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
  A verified one-shot importer in the existing market-data-ingestor image can
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

`vmdev db init` applies migrations only; it does not create fabricated users,
broker accounts, credentials, strategies, or signals. `vmdev db reset` also
bootstraps the source-controlled instrument catalogue from
`config/instruments.yaml`. The full Compose stack has a separate `db-seed`
one-shot service for its source-controlled SQL configuration.

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

After migrations and catalogue seeding, prime the ledger from the real public
sources before a historical replay:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  run --rm --no-deps fx-rate-ingestor \
  python -m apps.market_data_ingestor.market_data_ingestor.main fx-rates-once
```

The continuous `fx-rate-ingestor` process then refreshes these observations on
its configured interval and exposes internal readiness on port `8004`. Its
source failure is isolated from the primary crypto-candle scheduler.

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

Enable exactly one writer for a given instrument:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile calendar-ibkr up -d market-calendar-ibkr
```

Use `calendar-saxo` / `market-calendar-saxo` or
`calendar-zerodha` / `market-calendar-zerodha` for the other sources. Refresh
must remain inside `EXECUTION_MAX_MARKET_SESSION_AGE_SECONDS`; readiness is
served internally on port `8005`.

For native-strategy pipeline validation, populate the `prices` table
with Coinbase candles. Deep historical warmup for daily/swing strategies runs
as a separate 150-day one-shot (`python -m market_data_ingestor.main backfill`,
or the `market-data-backfill` Compose service under `--profile backfill`) so it
cannot stall the live poll loop. The live `market_data_ingestor` then owns only
its short startup request and 60-second polling. This fixture is not evidence
that SwingHighLowPMO—or any `READY_FOR_BACKTEST` strategy—is paper/live ready;
see [Strategy Readiness](STRATEGY_READINESS.md).

The one-shot retries an empty Coinbase request four times. With the aggregate
coverage gate enabled, at most one unique retried-empty request window per
symbol may defer to that final gate; a distinct second empty window aborts the
run. The gate also requires a recently closed tail candle, because a stale
several-hour suffix can otherwise be hidden inside 150 days of 95%-complete
history; that suffix is repaired with a bounded recent pass. Malformed or
partially rejected candle batches fail closed rather than being classified as a
venue gap.

```bash
# Populate 150 days in an isolated one-shot, then start continuous polling.
INGESTOR_SYMBOLS=BTC-USDC \
INGESTOR_GRANULARITY=ONE_MINUTE INGESTOR_BACKFILL_DAYS=150 \
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile backfill run --rm --no-deps market-data-backfill
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  up -d market-data-ingestor

docker compose --env-file .env -f docker/docker-compose.stack.yml \
  run --rm --no-deps execution-engine \
  python /app/scripts/replay_canonical_signals.py \
  --user-id demo_user \
  --broker-account-id 1 \
  --strategy-id swing_high_low_pmo_v1 \
  --symbols BTC-USDC \
  --start-date 2026-07-10 \
  --end-date 2026-07-16 \
  --source coinbase_live \
  --timeframe 15m \
  --require-minute-data \
  --no-enable-shorting
```

This fixed real-history witness contains the Swing LONG at
`2026-07-14T11:00:00Z` and CLOSE at `2026-07-14T12:45:00Z`. Normal
scoring/outbox delivery of those old commands must publish a blocked/stale
result with no order at the execution freshness gate. The explicit canonical
replay is a separate, local-paper-only accounting proof. It requires one exact
v1 decision and published execution-command event for the user/account/signal,
uses the persisted command's policy/score/route as economic authority, requires
one bounded exact binding, and retains each fill's source price ID/content
revision. It is not evidence of current-time transport.

Coinbase environment guidance:
- `sandbox smoke`: use the Advanced Trade sandbox only for auth/request-shape verification
- `paper soak`: use the local paper broker with live Coinbase market data
- `live`: use Coinbase Advanced Trade production endpoints only after certification

Feedback reads only completed, provenance-matched candles written by
`market_data_ingestor`; it does not run a parallel market-data client.

After a successful 14-day paper soak, write the certification marker consumed by the execution-engine live gate:

```bash
python scripts/write_sandbox_certification_marker.py \
  --commit "$(git rev-parse HEAD)" \
  --symbols BTC-USDC,ETH-USDC,SOL-USDC \
  --paper-window-days 14 \
  --duplicate-submission-count 0 \
  --operator your.name \
  --acceptance-report .artifacts/coinbase/soak-acceptance.json \
  --sandbox-smoke-evidence .artifacts/coinbase/sandbox-smoke.json \
  --paper-soak-evidence .artifacts/coinbase/paper-soak-summary.json \
  --reconciliation-summary .artifacts/coinbase/reconciliation-summary.json
```

## Cloud Deployment

No cloud database or infrastructure is configured for this migration. The local
Compose stack is the available runtime reference. Future deployment requires
separately reviewed ownership, role provisioning, secret delivery, backups,
restore checks, and rollback. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Data Residency & Security

- One individual `user_id` is one tenant. Broker accounts, credentials,
  bindings, risk state, orders, positions, P&L, and feedback remain attributable
  to that user; organization rows are identity metadata, not a shared-desk
  account-ownership model.
- Each runtime uses one least-privilege PostgreSQL login plus RLS/service-role
  policies. Schema-owner credentials are restricted to migration and controlled
  catalogue jobs.
- Per-account secrets may be stored only as ciphertext in `managed_secrets`
  using the externally supplied `SECRETS_MASTER_KEYS` ring; plaintext is never
  returned by the control plane.
- Canonical signals, decisions, order/fill lineage, outbox redrive audit, and
  API audit rows provide the retained operational history for the current
  personal-use scope.

## Operational Checklist

- `vmdev db start` → `vmdev db init` on fresh clones.
- Run `alembic upgrade head` on deploy; keep migrations reviewed.
- Daily off-host `pg_dump` backups enabled for the self-hosted database; perform
  an isolated restore drill monthly.
- Keep `DATABASE_URL` set per environment; prefer IAM auth in prod.
