# vynmatrix Setup (macOS/Linux)

This guide prepares an isolated local development and paper-test environment.
For the other platform, use the [Windows guide](SETUP_WINDOWS.md). The source is not yet open-source:
[LICENSE](LICENSE) is unchanged and the license/rights decision is pending.
These steps do not publish the repository or authorize deployment or live orders.

## 1. Prerequisites

- Python 3.11 on PATH. [config/build.yaml](config/build.yaml) records 3.11.13;
  CI and images use the Python 3.11 series. Use the configured patch where available.
- Git for the initialized local repository and hooks.
- Docker Engine/Desktop with Compose v2 for container integration checks only.
- About 15 GB free disk space as a starting allowance for images and environments.
- Access to the package index for dependency installation. Broker credentials and
  GitHub authentication are not required for the default Python checks.

On macOS use your installed Python 3.11 or pyenv; on Linux install Python 3.11 and its venv support through your normal package manager. `make setup` also requires make. All commands below run from the repository root.

## 2. Open the source

For this local migration, open the prepared `vynmatrix` directory. Remote access
is not required for local setup. After maintainers designate and publish a
repository, the configurable clone form is:

```bash
GITHUB_OWNER=YOUR_GITHUB_OWNER
git clone "https://github.com/${GITHUB_OWNER}/vynmatrix.git"
cd vynmatrix
```

`YOUR_GITHUB_OWNER` is a placeholder, not an existing account or organization.
Do not run the clone command until that destination is established. A source
archive can be tested, but Git hook installation requires an initialized checkout.

## 3. Create the tooling and test environment

```bash
python3.11 -m venv .venv-dev
source .venv-dev/bin/activate
python --version
python -m pip install --constraint docker/constraints.txt --requirement docker/requirements-svc-base.txt --requirement docker/requirements-scoring.txt --requirement docker/requirements-indicator-runner.txt --requirement docker/requirements-market-data.txt --editable tools/dev_cli pytest pytest-cov pre-commit mypy ruff types-PyYAML build psutil jsonschema
make setup
pre-commit install-hooks
vmdev --version
```

The dependency command mirrors the Python CI runtime and adds the wheel build
tool. It uses [docker/constraints.txt](docker/constraints.txt), the same pinned
dependency authority as service builds. `make setup`/the Windows setup script
installs `vmdev` and repository-local Git helpers. Both the pre-commit quality
wrapper and pre-push guard live in `.githooks`; `core.hooksPath` points there.
`pre-commit install-hooks` prepares the quality-check environments without
replacing that hook path. Keep `.venv-dev` active for all `vmdev` and commit commands.
Do not install project packages into system Python.

## 4. Run local Python checks

```bash
vmdev test lib --name=lib_common
vmdev test all
vmdev audit --strict
pre-commit run --all-files
```

`vmdev test` starts pytest using the CLI's own Python interpreter. It supports
`all`, `lib`, and `team`; there is no `vmdev test strategy`. For focused work use
`python -m pytest <existing-test-path>` from the same active environment.
PostgreSQL tests require an isolated test database and their documented opt-ins;
credentialed broker checks are separate. See [CONTRIBUTING.md](CONTRIBUTING.md#testing)
and [.github/workflows/ci.yml](.github/workflows/ci.yml).

## 5. Build wheels and isolated runtime environments

```bash
vmdev build libs
vmdev build strategies
ls build/wheels
vmdev build venvs
```

Expect six `lib_*` wheels and the `vynmatrix_indicator` wheel in `build/wheels/`.
Application venvs and the `build/venvs/strategy-validation` environment contain
installed artifacts for runtime/campaign checks; they do not replace the
`.venv-dev` interpreter used by `vmdev test`. Rebuild wheels after changing
package source, then rebuild affected environments or images.

## 6. Configure a private local environment

```bash
test -f .env || cp .env.example .env
```

Edit `.env` locally. Never commit it or copy personal/runtime state from another
checkout. Use fresh local values for `DB_PASSWORD`, `API_KEY`, `ADMIN_API_KEY`,
and `BACKEND_ADMIN_API_KEY`; the backend fails closed without its admin key.
Leave `BACKEND_ALLOW_ANON=false`. Optional pgAdmin needs its own local password.

Keep these execution settings:

```dotenv
ENVIRONMENT=dev
EXECUTION_MODE=paper
EXECUTION_ENGINE_ALLOW_LIVE=false
EXECUTION_USE_LOCAL_PAPER_BROKER=true
```

For Compose, leave `DATABASE_URL` and per-service database URLs unset so the
stack provisions its declared database and least-privilege service logins.
Use the Compose service addresses `http://execution-engine:8000` for
`EXEC_ENGINE_URL` and `http://scoring-engine:8001` for `SIGNAL_API_URL`, or leave
those overrides unset. A container's `localhost` points to itself.

The default crypto/FX ingestors contact public market-data endpoints; paper
mode prevents live execution but does not mean offline operation. Keep optional
broker/calendar/equity profiles disabled until their required credentials and
catalogue data are separately configured. Configuration details are in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## 7. Build and start the local Docker stack

```bash
docker info
vmdev build docker --from-config --tag latest
docker image ls vynmatrix/indicator-runner
docker compose --env-file .env -f docker/docker-compose.stack.yml up -d
docker compose --env-file .env -f docker/docker-compose.stack.yml ps
```

The stack bootstraps PostgreSQL migrations, seeds, and service roles before
starting dependent services. Do not also start the separate `vmdev db start`
database on the same port: that helper is an alternative for host-side database
work, not a prerequisite for this Compose stack.

The default stack runs scoring, execution, feedback, backend, market data, and
observed FX. It starts no indicator worker. To select the development canary:

```bash
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml --profile indicator up -d
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --tail 100 indicator-runner
```

`SwingHighLowPMO` is the enabled development-only strategy in source. Image
presence and a running container do not prove readiness: real historical warmup,
fresh bars, source lineage, bindings, and account authority are separate gates.
No signal or fill is guaranteed at startup. Follow the
[paper E2E guide](docs/E2E_VERIFICATION_GUIDE.md) for the full recorded-data proof.

Stop the local stack without deleting its volumes when finished:

```bash
docker compose --env-file .env -f docker/docker-compose.stack.yml down
```

## 8. Daily workflow and troubleshooting

Reactivate the environment in a new terminal with `source .venv-dev/bin/activate`. Run focused
tests and formatting before broader checks; rebuild libraries/strategies before
rebuilding Docker images. Use `deactivate` to leave the venv.

- `ModuleNotFoundError` during tests: confirm `python` and `vmdev` resolve inside
  `.venv-dev`, then rerun the pinned dependency command.
- Missing/stale wheel: run `vmdev build libs` and `vmdev build strategies` before
  rebuilding images.
- Database authentication failure: check local `.env` and whether an existing
  volume was initialized with different credentials; do not delete it blindly.
- Backend refuses startup: provide `BACKEND_ADMIN_API_KEY`; keep auth enabled.
- No signals: check the `indicator` profile, exact `STRATEGY_LIST`, warmup data,
  and readiness logs. Do not weaken gates to manufacture evidence.

GitHub CLI (`gh`) is needed only for a later hosted PR workflow, after the
repository owner and rights are established. See [CONTRIBUTING.md](CONTRIBUTING.md#workflow),
[the command reference](docs/QUICK_REFERENCE.md), and
[the documentation index](README.md#documentation).
