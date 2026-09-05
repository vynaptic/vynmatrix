# Local setup

Use this shared workflow after completing the platform prerequisites in
[SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) or
[SETUP_WINDOWS.md](SETUP_WINDOWS.md). It creates a local paper-development
environment; it does not provision a cloud deployment or broker authority.

## 1. Prepare repository tooling

The platform guide creates `.venv-dev` and installs `vmdev` plus the local Git
workflow. From that active environment at the repository root, install the
same constrained tooling and test dependencies as CI:

```text
python -m pip install --constraint docker/constraints.txt --requirement docker/requirements-svc-base.txt --requirement docker/requirements-platform.txt --editable tools/dev_cli pytest pytest-cov pre-commit mypy ruff types-PyYAML build psutil jsonschema
pre-commit install-hooks
vmdev test lib --name=lib_common
```

The constrained install is the dependency authority for local checks. The
platform guide's `vmdev git install` keeps the repository's pre-commit and
pre-push hooks under `.githooks`; `pre-commit install-hooks` prepares their
environments. See [CONTRIBUTING.md](CONTRIBUTING.md) for the pull-request
workflow and required checks.

## 2. Build local artifacts

```text
vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag latest
```

The final command builds the declared `vynmatrix/platform` image. Do not create
ad-hoc images or Compose profiles; [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
defines the supported topology.

## 3. Supply private configuration

Create a private `.env` from [.env.example](.env.example). Replace each required
placeholder or blank with distinct private values, including maintenance and
runtime database passwords, required service/admin keys, and SECRETS_MASTER_KEYS.
Leave optional provider and strategy selectors unset. Keep the file untracked
and out of commands, logs, and commits. The exact precedence, scoped database
URLs, credential boundaries, and paper defaults are canonical in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

Keep the .env URLs addressed to postgres:5432 for declared containers and the
bootstrap job. Later host-side catalogue, role, migration, canary, and owner
commands need their applicable scoped URL overridden privately to the loopback
listener (127.0.0.1:${DB_PORT}); do not copy the container hostname into those
commands or expose PostgreSQL publicly. [docs/DATABASE.md](docs/DATABASE.md)
maps each operation to its role and transport.

Create an untracked `owner.local.yaml` with your actual profile:

```yaml
profile:
  email: "<your email>"
  base_ccy: "<your accounting currency>"
  tz: "<your IANA timezone>"
```

Do not invent broker credentials, account mappings, or trading authority.

## 4. Bootstrap PostgreSQL and the owner

```text
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
```

Bootstrap creates or validates the explicit deployment owner, migrates the
schema, provisions runtime roles, and registers inactive reference data. It
does not create a broker account, binding, credential, execution selector, or
live authority. It stops application runtime while its declared maintenance job
runs; the detailed lifecycle, repeats, upgrades, and recovery rules are in
[docs/DATABASE.md](docs/DATABASE.md).

The normal split layout has PostgreSQL, `application`, and `workers`: three
running containers. The combined layout has PostgreSQL and one application
group. Both retain `EXECUTION_MODE=paper` and
`EXECUTION_ENGINE_ALLOW_LIVE=false`.

## 5. Work locally

Use [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) for supported commands,
[docs/STRATEGY_READINESS.md](docs/STRATEGY_READINESS.md) before selecting a
strategy, and [docs/E2E_VERIFICATION_GUIDE.md](docs/E2E_VERIFICATION_GUIDE.md)
for a recorded-data paper proof. A green unit test or running container is not
broker, strategy, or live-trading evidence.
