# Operational and Verification Scripts

This migration includes source only. Inherited certification and operational
utilities do not authorize deployment, live orders, or changes to existing runtime
state. Use isolated local targets and keep live execution disabled.

Utilities retained with vynmatrix for local database work, audits, and paper verification.

---

## Core Scripts

Use the PowerShell sibling where one is listed; not every shell utility has one.

### `build_strategies.sh` / `build_strategies.ps1`

**Purpose**: Build library and strategy wheels, managed venvs, and the
config-declared platform image, then verify `vynmatrix/platform`.
The indicator wheel is installed into that shared image;
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

### `run_platform.py` / `platform_processes.py`

**Purpose**: Supervise existing entrypoints with explicit per-process database
roles, scoped API keys, bounded restarts, and graceful group shutdown. The
declared Compose stack runs PostgreSQL plus `application` and `workers`; the
two-container alternative uses the same `application` service in `all` mode.
Feedback always runs, optional producers require `PLATFORM_WORKERS`, and an
empty `STRATEGY_LIST` skips indicator execution. See
[configuration](../docs/CONFIGURATION.md) for selectors and private listener ports.

Backfill and the existing quality-compounder panel cycle are explicit bounded
jobs inside the current group, with no additional container or hidden scheduler:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  exec -T workers python -m scripts.run_platform job backfill --timeout-seconds 3600
```

The alternative job name is `quality-compounder`; its independent provider,
entitlement, session, catalogue, and panel prerequisites must already be satisfied.
Use `application` instead of `workers` in the `all` layout. Timeouts must be
positive and at most 86400 seconds; expiry returns 124, overlapping same-job
execution returns 75, and termination returns 130. The local job lock covers the
single prescribed worker container, not multiple independently provisioned workers.

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
: "${CANARY_OWNER_ID:?Set the actual owner ID from onboarding}"
: "${CANARY_PAPER_ACCOUNT_ID:?Set the actual dedicated paper account ID}"
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  exec -T application sh -c \
  'DATABASE_URL="$EXECUTION_DATABASE_URL" exec python /app/scripts/replay_canonical_signals.py "$@"' replay \
  --user-id "$CANARY_OWNER_ID" \
  --broker-account-id "$CANARY_PAPER_ACCOUNT_ID" \
  --strategy-id swing_high_low_pmo_v1 \
  --symbols BTC-USDC \
  --start-date 2026-07-10 \
  --end-date 2026-07-16 \
  --source coinbase_live \
  --require-minute-data \
  --no-enable-shorting
```

This selects the execution database role for the replay child in the existing
container. It creates no account or execution authority. The reviewed real
Coinbase witness for Swing version `1.0.1` emits LONG at `2026-07-14T11:00:00Z` and
CLOSE at `2026-07-14T12:45:00Z`. Every resulting fill must preserve the exact
persisted source price ID/content revision and versioned trigger policy. Keep
that historical artifact separate from a new release's canary evidence; see the
[complete composite proof](../docs/E2E_VERIFICATION_GUIDE.md).

### `audit_table_counts.sql`

**Purpose**: Read PostgreSQL's estimated live-row counts for public tables,
ordered by count and name. These statistics are a diagnostic, not exact ledger
reconciliation. Use the explicit maintenance target from `vmdev db connect` and
the SQL file through the existing PostgreSQL connection; do not introduce a
global runtime `DATABASE_URL` for installation.

### `write_paper_promotion_manifest.py`

**Purpose**: Build one fail-closed paper authority for an independently eligible
single-instrument strategy or synchronized portfolio. The CLI's default Swing
config is currently an E2E-only development canary, permanently excluded from
paper promotion; passing the local pipeline procedure cannot promote it. Supply
the exact independently eligible candidate configuration. A synchronized
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

Run it from the exact immutable `vynmatrix/platform` image being promoted, using
`exec -T` in the existing container and `--output -` to stream the validated
manifest to a new host-side file; the mounted `/app/.artifacts` evidence is
read-only. Review it before placing it at the configured host artifact path. Both
the runner and scoring engine re-hash the files and require the same config,
model/instrument scope, evidence run, owner, binding, account, broker and image.
Synchronized authority fixes `data_use_scope=paper_forward`; every manifest
fixes `live_authority=false`.

The logical indicator attestation role remains `indicator-runner`, supplied as
`--container-image indicator-runner=vynmatrix/platform:<exact-tag>` to the existing
environment-attestation command. That role does not name another running
container. Retired indicator-image manifests are rejected; the platform image
requires newly matched image/config/evidence, never relabeled old certification.

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

**Purpose**: Retained standalone `pg_dump` helper for an explicitly selected
host database. The supported stack backup/restore lifecycle is
[`vmdev db backup` / `restore`](../docs/DATABASE.md); it scopes the maintenance
connection and retains runtime ordering. The standalone helper requires host
`pg_dump` and an explicitly supplied `DATABASE_URL`; its optional `SPACES_TARGET`
uploads outside the machine and requires separate authorization.

**Usage**:

```bash
vmdev db backup backups/before-maintenance.dump
```

---

## Operational Scripts — Ownership

Per the May 2026 codebase audit, these scripts are operational tooling (not
production runtime). Each has a designated owner team for maintenance + a
documented purpose. **They are not consolidation candidates** unless their
underlying workflow is replaced.

| Script | Owner team | Purpose |
|---|---|---|
| `run_platform.py` / `platform_processes.py` | platform | Scoped process groups and bounded existing jobs |
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
├── run_platform.py                # Group supervisor and bounded job launcher
├── platform_processes.py          # Existing entrypoints and child environment contracts
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
│   ├── pre_migration_backup.sh     # pg_dump pre-migration backup (cloud-agnostic)
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
`.venv-dev` runtime, wheels, platform image, and private `.env`. Installation and
routine database work share the canonical stack lifecycle in
[the database guide](../docs/DATABASE.md). The retired shell database managers,
automatic seed chain, and separate database Compose file are not startup paths.

### 2. Bootstrap with explicit owner configuration

Start with `COMPOSE_PROFILES=workers`,
`PLATFORM_APPLICATION_GROUP=application`, an empty `STRATEGY_LIST`, and only the
required explicit producer selection. Prepare the reviewed private owner document
and role credentials described in the database guide, then run:

```bash
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
```

Bootstrap stops both runtime groups before its declared maintenance job and
starts them only after successful completion, keeping the supported lifecycle
within three running containers. Registration creates inactive strategies and
registered releases, with no account, binding, or trading authority. The backend
has no general release-activation endpoint; setting `STRATEGY_LIST` does not
activate a registered release. The narrow maintenance-only
`vmdev db activate-canary` operation requires exact existing dev-only E2E canary
source and explicit paper/live-false gates. Follow its eligibility boundary and
the recorded-data requirements in [the E2E guide](../docs/E2E_VERIFICATION_GUIDE.md).

### 3. Validate signal flow

For an already eligible, explicitly authorized canary, inspect the exact
application database with `vmdev db connect`. Read its durable outputs:

```sql
SELECT * FROM canonical_signals ORDER BY ts DESC LIMIT 20;
SELECT * FROM asset_scores ORDER BY ts DESC LIMIT 20;
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
