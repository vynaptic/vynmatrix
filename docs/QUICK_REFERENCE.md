# Quick Reference

Command cheat sheet for local development. Activate the `.venv-dev` tooling/test
environment from the [setup guide](../README.md#new-developer-onboarding) first.
GitHub pull-request helpers target
[`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix). [LICENSE](../LICENSE)
limits use to personal, noncommercial purposes; source publication does not
authorize deployment or live execution.

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
# The verified indicator strategy wheel is installed into the shared platform
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

```text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db catalogue --check
vmdev db catalogue --apply
vmdev db catalogue --apply --changes reviewed-changes.yaml
vmdev user show
vmdev user update --config owner-update.yaml
vmdev user account --config account.yaml --secrets-file protected-credentials.json
vmdev db migrate
vmdev db roles
vmdev db status
vmdev db connect
vmdev db backup backups/pre-upgrade.dump
vmdev db restore backups/pre-upgrade.dump
vmdev db stop
```

The optional `vmdev db activate-canary --strategy-id ID --version SEMVER` is a
maintenance-only transition for an exact, eligible development canary. It does not
create bindings or grant paper-promotion/live authority; see
[Database Reference](DATABASE.md#installation-and-privilege-stages).

See [DATABASE.md](DATABASE.md) for the exact identities, expected-value patch
schema, explicit account adoption, rollback constraints, and secret handling.
`db start` starts only PostgreSQL. Bootstrap runs migrations and initial inactive
references; `db migrate` is schema-only. There is no reset, demo-user seed, or
pgAdmin command.

## Docker

```text
vmdev build docker --from-config --tag latest
docker image ls vynmatrix/platform
docker compose --env-file .env -f docker/docker-compose.stack.yml ps
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 application workers
```

Use `vmdev db bootstrap` for startup/upgrades. With `COMPOSE_PROFILES=workers` and
`PLATFORM_APPLICATION_GROUP=application`, the runtime is three containers including
PostgreSQL. Empty profiles with group `all` use two. Do not use wildcard profiles or
start a fourth maintenance container. Provider selection is `PLATFORM_WORKERS`;
strategy selection is `STRATEGY_LIST`, both explicit and separate from registration.
Keep paper execution and the disabled live gate. The
[E2E guide](E2E_VERIFICATION_GUIDE.md) owns recorded-data and bounded-job commands.

## Git

Verify the intended personal remote before publishing. The hosted PR examples below apply
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
vmdev db bootstrap --owner-config owner.local.yaml
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
