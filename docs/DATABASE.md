# Database reference

PostgreSQL is the supported runtime database. This is the canonical guide for
bootstrap, database roles, migrations, catalogue reconciliation, schema
meaning, and backup. SQLite is limited to unit tests. Runtime environment
ownership is in [CONFIGURATION.md](CONFIGURATION.md), and container lifecycle
is in [DEPLOYMENT.md](DEPLOYMENT.md).

## Installation and privilege stages

Prepare the local image and private environment with [SETUP.md](../SETUP.md).
The lifecycle keeps distinct authority at each stage:

| Stage | Required authority | Commit boundary |
| --- | --- | --- |
| Database creation and ownership check | ADMIN_DATABASE_URL to postgres | PostgreSQL CREATE DATABASE autocommit |
| Alembic and owner initialization | MIGRATION_DATABASE_URL to the target database | Separate migration and owner transactions |
| Runtime login provisioning | Explicit maintenance administrator | One runtime-role transaction |
| Static reference registration | Verified maintenance/schema authority | One catalogue transaction |
| Routine owner, account, and catalogue changes | BACKEND_DATABASE_URL as vm_backend_login | One validated request or batch |

Create an untracked owner.local.yaml with actual values:

~~~yaml
profile:
  email: "<your email>"
  base_ccy: "<your accounting currency>"
  tz: "<your IANA timezone>"
~~~

Then run:

~~~text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
~~~

Bootstrap verifies the supplied role and input, stops application groups, runs
the declared maintenance job, and then starts only the selected runtime groups.
It applies Alembic, validates head, creates missing runtime logins, registers
references, and initializes the owner in resumable stages. Database creation
remains outside a transaction; failures keep runtime stopped and never reset,
stamp, or run demonstration SQL.

Repeat with profile: {} to validate an existing designation without reapplying
profile values. Supplied repeat values must match. An existing historical user
may be adopted only through owner.local.yaml with existing_user_id or vmdev user
init --existing-user-id, using maintenance authority. This is the sole
caller-supplied owner identity exception; routine API and CLI operations cannot
choose an owner.

Reference registration creates inactive strategies and registered strategy
versions. It creates no broker account, credential, binding, risk mandate,
runtime selector, or execution authority. The narrow vmdev db activate-canary
operation is development-only, requires paper mode and the live gate false, and
does not establish an account or binding.

## Routine configuration changes

Source-controlled config/instruments.yaml, config/brokers.yaml, and each
strategy config.json describe reviewed non-secret references. The database owns
installed owner settings, stable account keys, encrypted credentials, audit
state, and historical records. Stable identities are strategy ID plus semver,
broker code/environment/region, explicit instrument identity, and account
config_key; numeric IDs remain preserved.

~~~text
vmdev db catalogue --check
vmdev db catalogue --apply
vmdev db catalogue --check --strategy-id <existing-source-strategy-id>
vmdev db catalogue --apply --broker-code <implemented-broker-code>
vmdev db catalogue --apply --changes reviewed-changes.yaml
vmdev user show
vmdev user update --config owner-update.yaml
vmdev user account --config account.yaml --secrets-file protected-credentials.json
~~~

Check validates without writes. Ordinary apply creates only missing rows. A
different installed value is a conflict, not permission to overwrite or
delete. Existing registered, active, deprecated, and pulled strategy-version
states are preserved; a changed release payload needs a new semver and
catalogue reconciliation cannot activate a release.

Explicit patches include a stable key, expected current values, and desired
changes. They are suitable for bounded metadata, not financial contract terms,
tradability, currency, session authority, or an instrument identity. Complete
batches, audit rows, and catalogue locks share one transaction. A bounded retry
re-reads state after serialization, deadlock, connection, or uncertain-commit
failures. Identical acknowledged repeats are no-ops; conflicts leave no partial
write.

Owner and account patches use the same expected-value rule. A protected CLI
credential input is for new-account creation; an expected-value patch cannot
carry credentials. Replace an existing credential document through the
authenticated backend, which commits its pointer, ciphertext, account change,
and audit together. Account currency, external identity, and opening capital
cannot change after relevant activity. Never place a secret in a command line.

## Existing databases, migrations, and rollback

The current linear Alembic head is 0104_saxo_capability_flags. Verify the
revision from scripts/db/alembic/versions rather than copying a transient
command result. Revisions 0099 through 0104 introduce single-owner controls,
safe reference registration, control-plane guards, commercial-tenancy removal,
and a guarded Saxo capability correction.

For an existing database, configure its actual maintenance authority and use:

~~~text
vmdev db backup backups/pre-upgrade.dump
vmdev db migrate
vmdev db restore backups/pre-upgrade.dump
~~~

Migrate is schema-only: it stops runtime, verifies authority, obtains the
migration lock, and never seeds, initializes another owner, or transfers
ownership. Back up and validate a restore before upgrading data that matters.
Restore requires an explicit target and leaves runtime stopped for verification.
Keep the separate encryption-key ring with the archive.

Migration 0103 refuses to remove populated commercial-tenancy structures. A
guarded downgrade is not a data rollback: it refuses loss of configured owner,
stable account key, or registered-version semantics. Restore a matched
code/database snapshot when a downgrade cannot preserve records. Do not delete
data or volumes to obtain a clean startup.

The .env URLs use postgres:5432 inside Compose. Bootstrap, backup, restore, and
connect run through declared container slots and retain that hostname. Host-side
roles, migrate, canary, catalogue, and vmdev user operations instead need a
private per-operation scoped URL using the loopback listener
(127.0.0.1:${DB_PORT}). Preserve the same target database and intended role;
never copy the container hostname into a host command or expose PostgreSQL
publicly.

## Schema overview

Application models live under
libs/python/lib_application/lib_application/db/models. These domain groups are
the useful schema map; inspect models and Alembic for column-level truth.

| Domain | Primary records | Meaning |
| --- | --- | --- |
| Owner and control plane | users, linked_broker_accounts, broker_credentials, managed_secrets, user_strategy_bindings, risk mandates | Owner/account identity, encrypted credentials, and current authority |
| Catalogues | brokers, broker_environments, instruments, aliases, broker symbols, sectors, calendars, sessions | Stable broker and tradable-contract reference data |
| Signal and scoring | canonical_signals, asset/market/sector_scores, strategy decisions, execution_decision_logs | Durable signal and account-scoped scoring lineage |
| Delivery | outbox_events | Transactional event identity, lease, retry, dead-letter, and redrive audit |
| Execution ledger | order_intents, orders, executions, pending_orders, account execution generations | Canonical request/order/fill chain and recoverable submission state |
| Accounting projections | positions, daily_nav, execution_metrics | Rebuildable views derived from canonical fills |
| Feedback | signal_performance, mode_performance, strategy feedback and trackers | Attributed evaluation and audit decisions |
| Market evidence | prices, watermarks, panel/factor/rank/rebalance records | Revisioned source data and strategy evidence |

Signal storage uses long, short, flat, and hold. SignalAction CLOSE maps to flat;
use normalize_scoring_action rather than a manual lowercase conversion.

StrategyVersion status is constrained by ck_version_status to registered,
active, deprecated, or pulled. Registration never promotes a version. Binding
entry and exit flags remain separate. The default binding mode policy is fixed;
the other supported policies are best_return, lowest_risk, highest_sharpe, and
user_rotating. Their ranking applies only when the resolved execution mode is
best or auto and still respects the binding's allowed modes.

## Reference and market data

Observed FX is persisted in prices, not inferred from a constant. The selected
FX process writes ECB EUR reference observations and Coinbase USDC-EUR traded
candles. Accounting resolves direct, inverse, or one EUR-cross observation at
the relevant timestamp only when all required legs are fresh. Missing FX blocks
the affected balance, position, P&L, or NAV calculation.

### Authoritative market sessions

Crypto instruments are explicitly continuous. Every non-crypto tradable
instrument is scheduled and needs a current market_calendars record plus
complete market_sessions coverage. A covered instant outside an interval is
authoritatively closed; missing, stale, future-dated, or uncovered data is
unavailable and blocks new exposure. CLOSE remains eligible for risk reduction.

The backend replaces a calendar's coverage, intervals, and exact instrument
assignments in one transaction. Omitted instruments detach and fail closed.
One selected calendar writer owns each instrument; it must resolve a catalogued
broker identity and validate the provider response before writing. Empty
selectors, incomplete mappings, expired credentials, or source failures do not
produce a guessed weekday schedule.

## Accounting and deployment boundaries

One owner may have multiple accounts, broker environments, regions, strategies,
and currencies. Historical non-owner identities are attribution, not a new
tenant selector. Every executable decision must preserve account, instrument,
session, contract, and fill-time FX provenance through the ledger and feedback.

Positions and metrics are projections, never restart-accounting inputs. The
transactional outbox remains the scoring-to-execution delivery boundary. Use
[E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md) to prove those contracts
with recorded real data, and [DEPLOYMENT.md](DEPLOYMENT.md) for container
operation.
