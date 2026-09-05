# Runtime configuration

This document owns runtime configuration precedence, ownership, and fail-closed
behavior. The complete variable catalogue is [.env.example](../.env.example).
Keep deployment-specific values outside Git. This configuration does not grant
deployment, broker, paper-promotion, or live authority.

## Precedence

For a running process, values resolve in this order:

1. Explicit arguments accepted by an operational command.
2. Environment injected by the declared runtime.
3. Source-controlled strategy config.json for strategy behavior only.
4. Validated code defaults.

config/build.yaml and config/containers.yaml describe artifacts, not trading
policy. config/deployment/*.yaml supplies runner composition where
ApplicationManager consumes it. Its legacy live_mode controls restart behavior
after a clean strategy exit; it never enables orders. These files do not inject
database credentials or invent a topology.

## Ownership

| Surface | Source of truth |
| --- | --- |
| Local runtime values | Private .env passed to the declared Compose stack |
| Runtime role URLs | docker/docker-compose.stack.yml |
| Child environment allowlists and role-to-DATABASE_URL mapping | scripts/platform_processes.py |
| Owner profile, accounts, bindings, and risk state | Database services used by the backend and vmdev user |
| Account credentials | managed_secrets encrypted by the newest-first SECRETS_MASTER_KEYS ring |
| Reviewed brokers and instruments | Source catalogue plus database registration |
| Strategy behavior | The selected strategy config.json and immutable registered version snapshot |
| Market price, FX, calendar, and provider evidence | Point-in-time database records and their source lineage |
| Build dependencies and images | config/build.yaml, config/containers.yaml, Docker runtime requirement profiles, and docker/constraints.txt |

Dependency authority has three layers: component `setup.py` files declare package
metadata, `docker/requirements-*.txt` selects exact runtime profiles, and
`docker/constraints.txt` pins those profiles. Add a runtime package to its
applicable profile and constraint, then update package metadata when it is an
importable component dependency. `vmdev audit` verifies runtime-profile pins
against constraints and separately checks static, safe setup metadata; it does
not compare the two.

## Process inputs and credentials

The declared launcher maps one least-privilege database URL into each child:

| Process | Role input | Internal port |
| --- | --- | ---: |
| Backend | BACKEND_DATABASE_URL | 8081 |
| Scoring and inline relay | SCORING_DATABASE_URL | 8001 |
| Execution | EXECUTION_DATABASE_URL | 8000 |
| Feedback | FEEDBACK_DATABASE_URL | 8002 |
| Primary market data and selected calendar/FX workers | MARKET_DATA_DATABASE_URL | 8003, 8004–8008 |
| Indicator runner | INDICATOR_DATABASE_URL | 8080 |
| Platform supervisor | No database connection | 8090 |

Only backend and PostgreSQL have declared loopback host listeners. Internal
services retain their Compose-network ports. [DEPLOYMENT.md](DEPLOYMENT.md)
owns container layout and lifecycle.

Runtime groups must not receive generic DATABASE_URL, DB_USER, DB_PASSWORD,
ADMIN_DATABASE_URL, MIGRATION_DATABASE_URL, or PostgreSQL administrator
credentials. Maintenance URLs are for the explicit database lifecycle only.
Container maintenance utilities use postgres:5432; host-side maintenance and
control-plane commands need a host-reachable scoped URL. The role, bootstrap,
and backup details are in [DATABASE.md](DATABASE.md).

Use distinct backend, scoring, execution, feedback, and market-data API keys.
Backend uses its own admin boundary; scoring and execution distinguish service
and admin keys. Backend and execution alone receive the secrets key ring:
SECRETS_BACKEND=db, BACKEND_ALLOW_ANON=false, EXECUTION_MODE=paper, RUN_MODE=paper,
and EXECUTION_ENGINE_ALLOW_LIVE=false are fixed launch inputs. No global broker
trading key or caller-selected owner is accepted as execution authority.

## Snapshots, parsing, and readiness

Long-lived processes validate configuration once at composition time and retain
an immutable snapshot. Changing an environment variable needs the supported
restart lifecycle; it cannot change an established route, binding cache,
deduplication rule, risk gate, child credential, or worker catch-up state.

Use lib_common.env_utils for numeric and boolean values. Malformed values fail
startup, and vmdev audit rejects new hand-rolled parsing. Connection pools and
JSON-file logging are bounded by the values in .env.example; change them only
with a local resource budget. The supervisor gives health for liveness and
ready for selected-component progress. A management endpoint may be healthy
while feeds, owner configuration, or execution authority keep the platform
unready.

Readiness fails on stale selected strategy or outbox work, dead-lettered
execution commands, unknown broker submissions, incomplete account
reconciliation, stale required FX, or exhausted bounded database resources.
The exact metrics and current thresholds are declared in .env.example and
observed through the supervisor; [RUNBOOK.md](RUNBOOK.md) owns incident
response.

## Worker and strategy selection

PLATFORM_WORKERS selects optional market-data work only:
market-data, equity, fx, calendar-ibkr, calendar-saxo, and calendar-zerodha.
An empty value selects none. STRATEGY_LIST independently selects strategy
workers; an empty value starts none. Neither selector activates a strategy,
binding, broker account, or execution route.

The FX worker persists observed ECB EUR reference rates and Coinbase USDC-EUR
traded candles. Conversion resolves an eligible point-in-time direct, inverse,
or EUR-cross observation; it never assumes USD/USDC parity.
`FX_RATE_HISTORY_DAYS` accepts `1..366` and controls Coinbase `USDC/EUR` hourly
backfill; the ECB source retains its rolling 90-day history. Configure only
currencies, age bounds, and source products permitted by .env.example.

Each non-crypto tradable instrument needs current official session coverage.
calendar-ibkr, calendar-saxo, and calendar-zerodha are opt-in writers with
unique internal ports and exact configured canonical symbols. They resolve
catalogued broker identity and replace coverage through the backend under its
admin boundary. Missing, stale, future-dated, incomplete, or conflicting
coverage blocks new exposure; crypto is explicitly continuous and CLOSE remains
eligible for risk reduction. [DATABASE.md](DATABASE.md#authoritative-market-sessions)
defines the persisted session meaning.

## Strategy and promotion configuration

New bindings are inactive with autopilot disabled. Entry and exit authority are
separate; a scoring decision is audit context, not an irrevocable broker
submission grant. Execution resolves the current owner, binding, account,
credential, environment, and exact route immediately before broker I/O.

Paper promotion, where a strategy supports it, requires matching read-only
indicator and scoring manifests, the exact declared image identity, a
strategy-specific recorded-data evidence set, an account/binding/route scope,
and live_authority=false. A manifest never satisfies
EXECUTION_ENGINE_ALLOW_LIVE. The writer and evidence procedure are referenced
by [E2E_VERIFICATION_GUIDE.md](E2E_VERIFICATION_GUIDE.md).

USQualityCompounder remains disabled. It must not enter STRATEGY_LIST or gain a
binding until its provider, panel, catalogue, session, account, and
strategy-specific evidence gates pass. Current status belongs in
[STRATEGY_READINESS.md](STRATEGY_READINESS.md); its research contract is in the
[strategy README](../strategies/indicator/USQualityCompounder/README.md).
