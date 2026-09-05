# Quick Reference

Command cheat sheet for local development. Activate the `.venv-dev` tooling/test
environment from the [setup guide](../README.md#new-developer-onboarding) first.
GitHub PR and release helpers require separately configured hosting; this local
migration does not authorize publication, pushing, deployment, or live execution.

---

## vmdev CLI

```bash
# Building
vmdev build libs
vmdev build libs --component=lib_indicators
vmdev build strategies
vmdev build venvs
vmdev build venvs --group=indicator

# Docker - Config-driven service images (recommended)
vmdev build docker --from-config --tag latest  # Build images declared by containers.yaml
# The verified indicator strategy wheel is installed into one indicator-runner
# image; per-strategy image builds are not supported.

# Release naming inspection only (does not tag or publish)
vmdev release normalize V1_1p0
# Publication is a separate manual, rights-gated action; see DEPLOYMENT.md.

# Testing
vmdev test all
vmdev test lib --name=lib_common
vmdev test team --team=quant

# Formatting
vmdev format

# Git MR workflow
vmdev git install
vmdev git mr submit

# Running

# Cleanup
vmdev clean --all       # Repository artifacts only; never prunes global Docker state
vmdev clean --docker    # Explicitly remove local vynmatrix/* images
```

---

## Pre-commit Hooks

```bash
# Prepare environments for the tracked .githooks/pre-commit wrapper
vmdev git install
pre-commit install-hooks

# Run manually
pre-commit run --all-files

# Update hooks
pre-commit autoupdate

# Do not bypass hooks; fix failures before committing.
```

---

## Database Management

```bash
# Start/Stop PostgreSQL
vmdev db start                    # Start PostgreSQL container
vmdev db stop                     # Stop PostgreSQL container
vmdev db status                   # Check status and table counts

# Schema Management
vmdev db init                     # Initialize schema only
vmdev db reset                    # Drop/recreate schema + configured instruments

# Connect and Query
vmdev db connect                  # psql shell (via Docker)

# Backup/Restore
vmdev db backup                   # Backup to backups/ directory
vmdev db backup ./my_backup.sql   # Backup to specific file
vmdev db restore ./backup.sql     # Restore from backup

# Admin UI (optional)
vmdev db pgadmin                  # Start pgAdmin at localhost:5050

# User Management
vmdev user add                    # Interactive user creation
vmdev user add --config users.yaml  # Batch from config file
vmdev user list                   # List all users
```

User configuration must declare `base_currency`; each configured broker account
must separately declare `broker.base_currency`. Both are canonical uppercase
currency codes. Paper broker configuration must also declare
`paper_initial_equity` and `paper_initial_cash`; cash must be between zero and
equity. The CLI never assumes a currency or account balance.

---

## Docker

```bash
# Build images
vmdev build docker --from-config --tag latest

# Images (local namespace: vynmatrix/*)
docker images | grep vynmatrix
vmdev clean --docker             # Removes only locally tagged vynmatrix/* images

# Core services (the indicator runner is profile-gated)
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d

# Prime observed ECB + Coinbase USDC-EUR rates before historical replay
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  run --rm --no-deps fx-rate-ingestor \
  python -m apps.market_data_ingestor.market_data_ingestor.main fx-rates-once

# Mode Selection
EXECUTION_MODE=paper docker compose --env-file .env -f docker/docker-compose.stack.yml up -d    # Default safe mode
EXECUTION_MODE=backtest docker compose --env-file .env -f docker/docker-compose.stack.yml up -d # Historical data only
# Live execution is outside the migration scope; keep the live gate disabled.

# Start the benchmark indicator worker with an explicit allowlist
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d

# Strategy runner overrides
RUN_MODE=backtest docker compose --env-file .env -f docker/docker-compose.stack.yml up -d        # Force strategy run mode
```

---

## Git

The local migration has no configured remote. The hosted PR examples below apply
only after the owner establishes the destination and rights; do not run them as
part of local setup. Inspect `git remote -v` before any hosted operation.

```bash
# One-time setup (run once after cloning)
vmdev git install                       # Install git mr alias + hooks
gh auth login -h github.com             # Authenticate GitHub CLI (opens browser, one-time)
gh auth status -h github.com            # Verify: should show logged in

# Main-first PR workflow (daily use)
git switch main
git pull --rebase origin main
git add <reviewed-files>
git commit -m "feat: add my strategy"   # Commit on local main
git mr submit                           # Creates branch + PR, resets local main
# Creates feature/<timestamp>-<slug>, opens PR to main, resets local main

# Optional submit flags
git mr submit --draft
git mr submit --title "feat: add my strategy" --body "Implementation notes"

# If rebase conflicts
git add <resolved-file>
git rebase --continue
# or cancel:
git rebase --abort

# Conventional commits — full convention: CONTRIBUTING.md § Commit messages
git commit -m "feat: add new strategy"

# Stash
git stash
git stash pop
git stash list
```

> **Note**: `git mr submit` requires the GitHub CLI (`gh`). Git SSH keys handle push/pull,
> but `gh` uses a separate API token for creating PRs. Run `gh auth login` once to set it up.
> If `git mr submit` reports invalid auth, run `gh auth status -h github.com` then re-login.
>
> **Common submit blockers**:
> 1) `Working tree is not clean` -> run `git status`, then commit/stash changes.
> 2) `push to main is blocked` -> expected; direct push to `main` is disallowed, use `git mr submit`.

---

## Virtual Environments

```bash
# Tooling and tests
source .venv-dev/bin/activate
# Separately, use build/venvs/strategy-validation/bin/activate for recorded-data campaigns.

# Deactivate
deactivate

# List venvs
ls -1 build/venvs/
```

---

## Testing

```bash
# Activate the tooling/test venv first
source .venv-dev/bin/activate

# Run tests
pytest
python -m pytest libs/python/lib_common/tests -q
# For a single test: python -m pytest <existing-file>::<test-function>
pytest --cov=strategies/indicator --cov-report=html

# Type checking
mypy libs/python/lib_strategy/
mypy libs/

# Linting
ruff check .
ruff check --fix .
```

---

## Python

```bash
# Version check
python3 --version
which python3

# Package management
pip list
pip install -r requirements.txt
pip install build/wheels/lib_indicators-*.whl
pip install --force-reinstall build/wheels/lib_indicators-*.whl

# Freeze dependencies
pip freeze > requirements.txt
```

---

## File Locations

| Path | Description |
|------|-------------|
| `build/wheels/` | Library wheels |
| `build/venvs/` | Virtual environments |
| `strategies/indicator/<Name>/core.py` | PureSignalStrategy implementation |
| `strategies/indicator/<Name>/config.json` | Strategy config (`runner_kind: signal_worker`) |
| `config/containers.yaml` | Deployable service-image inventory |

---

## Common Workflows

### Create New Strategy

```bash
cp -r strategies/indicator/_template strategies/indicator/MyStrategy
cd strategies/indicator/MyStrategy
# Edit core.py (PureSignalStrategy subclass) and config.json
# Add MyStrategy to an explicit STRATEGY_LIST only after its activation gates pass
git switch main
git pull --rebase origin main
git add <reviewed-files>
git commit -m "feat: add MyStrategy"
git mr submit
```

### Modify Library

```bash
# Edit library code
vmdev build libs --component=lib_strategy
vmdev build venvs
vmdev test lib --name=lib_strategy
git add libs/python/lib_strategy
git commit -m "feat: update base strategy"
```

### Validate a Strategy

```bash
# For a frozen historical campaign, build the exact validation environment and
# follow "Reproducible Strategy Validation Environment" in USER_MANUAL.md.
vmdev build venvs --validation

# Runtime pipeline validation remains a separate service-boundary check.
EXECUTION_MODE=paper docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d
vmdev db connect   # inspect canonical_signals / asset_scores for the strategy
```

---

## Troubleshooting

```bash
# Docker not running
docker ps
open -a Docker  # macOS

# Pre-commit fails
pre-commit run --all-files
vmdev format

# Rebuild everything
vmdev clean --all       # Docker images remain unless --docker is explicit
make setup
vmdev build libs
vmdev build strategies
vmdev build docker --from-config --tag latest
```

---

## Environment Variables

```bash
# .env file (private local values; future deployment is unconfigured)
API_KEY=your-api-key
ADMIN_API_KEY=your-admin-api-key
BACKEND_ADMIN_API_KEY=your-backend-admin-api-key
ENV=dev
ENVIRONMENT=dev
LOG_LEVEL=INFO

# Scoring Engine
DATABASE_URL=postgresql://user:pass@host:5432/scores
SCORE_WEIGHTS=strat_a:0.6,strat_b:0.4
HALF_LIFE_BARS=20
EXEC_ENGINE_URL=http://execution-engine:8000

# Execution Engine Mode (CRITICAL for production safety)
EXECUTION_MODE=paper                     # backtest | paper | live (default: paper)
EXECUTION_ENGINE_ALLOW_LIVE=false        # Must be true for live mode
EXECUTION_DEDUP_TTL_SECONDS=3600         # Signal deduplication TTL
```

See [CONFIGURATION.md](CONFIGURATION.md) for precedence, service ownership, and
fail-closed production requirements.

---

For detailed explanations, see:

- [docs/USER_MANUAL.md](USER_MANUAL.md)
- [SETUP_MAC_LINUX.md](../SETUP_MAC_LINUX.md)
- [SETUP_WINDOWS.md](../SETUP_WINDOWS.md)
