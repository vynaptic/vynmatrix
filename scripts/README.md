# Operational and Verification Scripts

This migration includes source only. Inherited certification and operational
utilities do not authorize deployment, live orders, or changes to existing runtime
state. Use isolated local targets and keep live execution disabled.

Utilities retained with vynmatrix for local database work, audits, and paper verification.

---

## Core Scripts

Windows equivalents exist for shell scripts (`*.ps1`) in the same locations.

### `build_strategies.sh` / `build_strategies.ps1`

**Purpose**: Build library and strategy wheels, managed venvs, and the
config-declared service images, then verify the consolidated `indicator-runner`
image. The indicator wheel is installed into that image;
per-strategy/category images are retired.

**Usage**:

```bash
./scripts/build_strategies.sh --tag latest
```

```powershell
.\scripts\build_strategies.ps1 -Tag latest
```

---

### `diagnose_environments.sh` / `diagnose_environments.ps1`

**Purpose**: Quick local diagnostics for Python, vmdev, venvs, and wheels

**Usage**:

```bash
./scripts/diagnose_environments.sh
```

```powershell
.\scripts\diagnose_environments.ps1
```

---

## Database & Pipeline Utilities

### `replay_canonical_signals.py`

**Purpose**: Replay persisted `canonical_signals` through the production paper
execution path for one existing, user-owned linked broker account. Fills come
from persisted real one-minute market data; account/P&L currency conversion
comes from persisted point-in-time FX observations. Date ranges are half-open:
`--start-date` is inclusive and `--end-date` is exclusive. This is an explicit
historical paper/accounting path: normal scoring/outbox delivery must reject the
same old commands at the execution freshness boundary and publish the blocked
result without an order.

The account must be connected local paper, carry no broker credential, and have
exactly one active, explicitly instrument-bounded binding for this
user/account/strategy with autopilot, entries, and exits enabled. Bound that
authority to the replay interval and revoke all four authority flags afterward.
Every selected signal must already have one executable v1 decision for the
requested user/account and its exact published `execution.commands` row. Replay
uses the persisted command's stable dedup key, score context, policy/config, and
paper route; CLI sizing values are not independent economic authority.

**Usage**:

```bash
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
  --require-minute-data \
  --no-enable-shorting
```

The reviewed real Coinbase witness emits LONG at `2026-07-14T11:00:00Z` and
CLOSE at `2026-07-14T12:45:00Z`. Every resulting fill must preserve the exact
persisted source price ID/content revision and versioned trigger policy.

### `audit_table_counts.sql`

**Purpose**: Quick row-count audit of every production table grouped by domain
(tenancy/users, brokers, instruments, signals, scoring, execution, feedback,
outbox). Run via `psql $DATABASE_URL -f scripts/audit_table_counts.sql` to
sanity-check post-migration / post-deploy state.

### `write_paper_promotion_manifest.py`

**Purpose**: Build one fail-closed paper authority for an exact single-
instrument strategy or synchronized portfolio. The default config and inferred
scope/broker preserve the single-instrument command surface. A synchronized
portfolio supplies `--config`, `--model-configuration-sha256`, and one reviewed
`--instrument-set-artifact`; the writer validates, embeds, and hashes its exact
instrument-id/canonical-symbol allowlist. Account/binding identity and the
released image tag complete the immutable scope. The script reads and hashes
every required evidence artifact; it does not create, simulate, or mark evidence
as passing.

The synchronized allowlist artifact is sorted by positive `instrument_id` and
uses this exact schema; `model_configuration_sha256` must equal the registered
pre-start model configuration carried by later rebalance events:

```json
{
  "schema_version": "1",
  "strategy_id": "us_quality_compounder_v1",
  "strategy_version": "0.2.0",
  "model_configuration_sha256": "<64 lowercase hex>",
  "data_use_scope": "paper_forward",
  "instruments": [{"instrument_id": 123, "canonical_symbol": "IBM"}]
}
```

Run it from the exact immutable `indicator-runner` image being promoted. Both
the runner and scoring engine re-hash the files and require the same config,
model/instrument scope, evidence run, owner, binding, account, broker and image.
Synchronized authority fixes `data_use_scope=paper_forward`; every manifest
fixes `live_authority=false`.

### `write_sandbox_certification_marker.py`

**Purpose**: Write the `coinbase_sandbox_certified.json` marker file the
execution engine reads before allowing live mode. Run after a successful
14-day paper soak, passing `check_soak_acceptance.py` report, sandbox request
smoke, and clean reconciliation evidence.

**Usage**:

```bash
python scripts/write_sandbox_certification_marker.py \
    --commit "$(git rev-parse HEAD)" --operator "ops-on-call" \
    --symbols BTC-USDC,ETH-USDC,SOL-USDC --paper-window-days 14 \
    --duplicate-submission-count 0 \
    --acceptance-report ".artifacts/coinbase/soak-acceptance.json" \
    --sandbox-smoke-evidence ".artifacts/coinbase/sandbox-smoke.json" \
    --paper-soak-evidence ".artifacts/coinbase/paper-soak-summary.json" \
    --reconciliation-summary ".artifacts/coinbase/reconciliation-summary.json"
```

### `db/pre_migration_backup.sh`

**Purpose**: Take a `pg_dump` backup before production migrations. Cloud-agnostic
for a locally operated PostgreSQL database or a future separately approved
PostgreSQL topology.

**Usage**:

```bash
DATABASE_URL="$DATABASE_URL" SPACES_TARGET=s3://vm-backups/pg scripts/db/pre_migration_backup.sh
```

---

## Operational Scripts — Ownership

Per the May 2026 codebase audit, these scripts are operational tooling (not
production runtime). Each has a designated owner team for maintenance + a
documented purpose. **They are not consolidation candidates** unless their
underlying workflow is replaced.

| Script | Owner team | Purpose |
|---|---|---|
| `manage_broker_secret.py` | platform security | Account-scoped credential onboarding, verification, and atomic MultiFernet rotation; runbook: `docs/BROKER_CREDENTIALS.md` §4 |
| `replay_canonical_signals.py` | platform | Account-scoped historical paper-execution replay from persisted real market/FX data |
| `audit_table_counts.sql` | platform | Per-domain DB row-count audit |
| `write_paper_promotion_manifest.py` | platform | Exact evidence-backed single-instrument or synchronized-portfolio paper authority; never live authority |
| `write_sandbox_certification_marker.py` | platform | Coinbase sandbox certification marker |
| `db/pre_migration_backup.sh` | platform | pg_dump pre-migration backup guard (cloud-agnostic) |
| `build_strategies.{sh,ps1}` + `diagnose_environments.{sh,ps1}` | platform | Build + diagnostic helpers |

---

## Directory Structure

```
scripts/
├── manage_broker_secret.py       # Account-scoped encrypted credential operations
├── replay_canonical_signals.py    # Account-scoped historical paper-execution replay
├── write_paper_promotion_manifest.py  # Exact strategy/model paper authority
├── write_sandbox_certification_marker.py  # Coinbase sandbox certification
├── audit_table_counts.sql         # Per-domain DB row-count audit
├── build_strategies.sh             # Build consolidated strategy runtime (macOS/Linux)
├── build_strategies.ps1            # Build consolidated strategy runtime (Windows)
├── diagnose_environments.sh        # Local environment diagnostics (macOS/Linux)
├── diagnose_environments.ps1       # Local environment diagnostics (Windows)
├── setup_windows.ps1               # Windows setup helper
├── db/                             # Database management helpers
│   ├── bootstrap_scoring.py        # Seed source-controlled instruments after Alembic
│   ├── pre_migration_backup.sh     # pg_dump pre-migration backup (cloud-agnostic)
│   ├── manage_db.sh                # Postgres lifecycle (macOS/Linux)
│   ├── manage_db.ps1               # Postgres lifecycle (Windows)
│   └── alembic/                    # Alembic migrations
└── venv/                           # Virtual environment management
    ├── create_dev_venv.sh          # Development venv setup (macOS/Linux)
    └── create_dev_venv.ps1         # Development venv setup (Windows)
```

---

## Local Development Workflow

Indicator strategies run via the SignalWorker (DB-fed): each
`strategies/indicator/<Name>/core.py` (`PureSignalStrategy`) plus a
`config.json` with `"runner_kind": "signal_worker"` is fed from the `prices`
table — `market_data_ingestor` → Coinbase → `NOTIFY` → SignalWorker →
`core.on_data()` → scoring engine. LEAN backtesting is retired; there is no
second research strategy implementation. Registered historical campaigns run
the same packaged core through `vmdev strategy validate`; build and attest the
dedicated `strategy-validation` venv first as documented in
[the user manual](../docs/USER_MANUAL.md#reproducible-strategy-validation-environment).

### 1. Prepare the local environment

Follow the [OS setup guide](../README.md#new-developer-onboarding) for the
`.venv-dev` runtime, wheels, images, and private `.env`. The full Compose stack
bootstraps its own PostgreSQL database. `vmdev db start` is an alternative for
host-side DB work; do not also start it on the same port as this stack.

### 2. Bring up the local stack

```bash
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile indicator up -d
```

The profile adds `indicator-runner` to the default core services. No catalogue
candidate is staged by default; the explicit benchmark selection prevents a
developer's local `.env` from changing which worker is exercised.

### 3. Validate signal flow

Inspect Postgres to confirm the pipeline is producing output:

```bash
psql "$DATABASE_URL" -c "SELECT * FROM canonical_signals ORDER BY ts DESC LIMIT 20;"
psql "$DATABASE_URL" -c "SELECT * FROM asset_scores ORDER BY ts DESC LIMIT 20;"
```

Replay already-ingested signals through account-scoped paper execution with
`replay_canonical_signals.py`. End-to-end scoring → execution verification uses
the local Docker pipeline and persisted real market and observed-FX data.

---

## Related Documentation

- [CLAUDE.md](../CLAUDE.md) - Production guidelines
- [docs/DATABASE.md](../docs/DATABASE.md) - Database setup, schema, and operations
- [docs/USER_MANUAL.md](../docs/USER_MANUAL.md) - Architecture + code walkthrough

---

**Scope**: Script presence is not verification or operational authority. Review the exact target and documented prerequisites before running a utility.
