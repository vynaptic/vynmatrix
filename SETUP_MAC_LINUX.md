# vynmatrix Setup (macOS/Linux)

This guide prepares an isolated local development and paper-test environment.
For the other platform, use the [Windows guide](SETUP_WINDOWS.md). The source is
available under [LICENSE](LICENSE) for personal, noncommercial use; see
[NOTICE](NOTICE) for attribution and third-party-material boundaries. These steps
do not authorize deployment or live orders.

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

Clone the canonical repository:

```bash
git clone https://github.com/vynaptic/vynmatrix.git
cd vynmatrix
```

A source archive can be tested, but Git hook installation requires an initialized
checkout.

## 3. Create the tooling and test environment

```bash
python3.11 -m venv .venv-dev
source .venv-dev/bin/activate
python --version
python -m pip install --constraint docker/constraints.txt --requirement docker/requirements-svc-base.txt --requirement docker/requirements-platform.txt --editable tools/dev_cli pytest pytest-cov pre-commit mypy ruff types-PyYAML build psutil jsonschema
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

Edit `.env` locally using [.env.example](.env.example). Supply actual private
maintenance/runtime passwords, role-specific URLs, distinct API/admin keys, and
the encryption key ring. Keep `EXECUTION_MODE=paper`,
`EXECUTION_ENGINE_ALLOW_LIVE=false`, `COMPOSE_PROFILES=workers`, and
`PLATFORM_APPLICATION_GROUP=application`. Optional provider and strategy selectors
start empty; no broker connection or execution authority is invented.

The administrator URL targets `postgres`; the migration URL targets your application
database on the same server. Complete historical migrations require explicit
maintenance administrator authority. Runtime uses only the six service-role logins.
Use `postgres` as the hostname inside Compose and an explicit loopback URL for
host-side database commands. See [docs/DATABASE.md](docs/DATABASE.md) for the
canonical input/role contracts and [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
for process settings.

## 7. Build and bootstrap the local Docker stack

Prepare `owner.local.yaml` using your actual email, accounting currency and timezone
as specified in the [database guide](docs/DATABASE.md#installation-and-privilege-stages).
Then run:

```bash
docker info
vmdev build docker --from-config --tag latest
docker image ls vynmatrix/platform
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
```

The supported lifecycle runs PostgreSQL plus one maintenance job, removes the job,
then starts application/workers: **three running containers including PostgreSQL**.
The alternative `PLATFORM_APPLICATION_GROUP=all` with empty `COMPOSE_PROFILES`
uses two. The bootstrap validates input before side effects, preserves existing
settings and passwords, and stops on ownership, migration or reference conflicts.
`vmdev db start` starts this same PostgreSQL service only; there is no second stack.

Strategies register inactive with non-executable `registered` releases. Follow the
[readiness inventory](docs/STRATEGY_READINESS.md) and existing eligibility/activation
controls before adding an explicit `STRATEGY_LIST`. Configured provider workers
contact real external data sources; paper mode is not an offline mode. No signal or
fill is guaranteed by a successful startup. The
[paper E2E guide](docs/E2E_VERIFICATION_GUIDE.md) defines the recorded-data proof.

Stop the platform gracefully while preserving its volume:

```bash
vmdev db stop
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
