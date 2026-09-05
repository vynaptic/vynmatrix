# vynmatrix

A Python monorepo for multi-strategy signal generation, scoring, durable paper
execution, and feedback. This is an independent local migration with fresh Git
history; it does not carry an existing deployment, broker account, or strategy
certification.

**License status:** vynmatrix is not yet open-source. The existing [LICENSE](LICENSE)
is preserved unchanged. A license/rights decision is pending before publication,
redistribution, or accepting external contributions. No public GitHub owner or
registry is assumed by these instructions.

## Overview

Indicator strategies emit canonical signals without calling brokers. Scoring
persists decisions and an execution command in a transactional outbox. The
execution engine applies tenant, account, instrument, session, and risk gates,
then records orders and fills in the canonical ledger. Feedback evaluates those
persisted outcomes. PostgreSQL provides the runtime state and durable handoffs;
Docker Compose provides the local integration environment.

Keep `EXECUTION_MODE=paper`, `EXECUTION_ENGINE_ALLOW_LIVE=false`, and
`EXECUTION_USE_LOCAL_PAPER_BROKER=true` for local work. Selecting a strategy or
building an image does not authorize a broker order or a deployment.

## New Developer Onboarding

Use [SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) or
[SETUP_WINDOWS.md](SETUP_WINDOWS.md). Both cover:

1. Python 3.11, Git, and an isolated `.venv-dev` tooling/test environment.
2. Constraints-pinned dependencies, the retained `vmdev` CLI, and Git hooks.
3. Local tests and library/strategy wheel builds.
4. A private `.env` with local-only configuration.
5. Config-declared Docker images and explicit indicator selection for paper checks.

A source archive can be inspected and tested without GitHub authentication.
Repository Git helpers and release checks need an initialized Git checkout;
PR submission additionally needs a maintainer-designated remote and `gh` login.
The initial migration remains local: do not push, publish, release, or deploy it.

## Repository Structure

```text
vynmatrix/
├── apps/
│   ├── scoring_engine/         # Signal scoring and transactional outbox
│   ├── execution_engine/       # Risk gates, paper execution, canonical ledger
│   ├── feedback_loop_engine/   # Outcome evaluation and suggestions
│   ├── market_data_ingestor/   # Venue candles, observed FX, official sessions
│   ├── indicator_runner/       # DB-fed signal-only strategy workers
│   └── backend/                # Tenant/account/binding control plane
├── strategies/indicator/       # Strategy cores, configuration, and tests
├── libs/python/                # Six lib_* packages in domain/application/adapter layers
├── tools/dev_cli/              # vmdev builds, tests, audit, and validation campaigns
├── config/                     # Build inventory and reviewed runtime configuration
├── docker/                     # Dockerfiles, pinned constraints, Compose, seeds
├── scripts/                    # CI, database, and paper verification utilities
├── tests/                      # Cross-component and contract tests
├── docs/                       # Technical documentation
└── pyproject.toml              # vynmatrix package metadata and Python tooling
```

The root distribution is `vynmatrix`; shared imports remain `lib_common`,
`lib_data`, `lib_indicators`, `lib_strategy`, `lib_application`, and
`lib_infrastructure`. Indicator strategies are packaged in `vynmatrix_indicator`.
The developer command remains `vmdev`.

## Local builds and tests

Run from the repository root with the setup guide's `.venv-dev` active:

```bash
vmdev test lib --name=lib_common
vmdev test all
vmdev audit --strict
vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag latest
```

`vmdev test` runs pytest in the CLI's current interpreter. Component venvs isolate
installed wheels; `build/venvs/strategy-validation` is the separate environment
for recorded-data campaigns. Docker builds validate wheel freshness before
building the five configured `vynmatrix/*` service images.

After configuring `.env` as described in the OS guide, a local paper stack can
be started explicitly:

```bash
# Bash; see the Windows guide for PowerShell syntax.
EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false \
STRATEGY_LIST=SwingHighLowPMO \
docker compose --env-file .env -f docker/docker-compose.stack.yml \
  --profile indicator up -d
```

The core stack alone starts no indicator worker. Even with the profile selected,
signals require sufficient real price history, current bars, and strategy gates.
Bindings and account authority are separate; no signal or fill is promised by a
successful container start. Follow the [paper verification guide](docs/E2E_VERIFICATION_GUIDE.md)
for evidence, and the [readiness inventory](docs/STRATEGY_READINESS.md) for limits.

## Documentation

This is the canonical documentation index.

| Document | Purpose |
|---|---|
| [SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) | macOS/Linux setup and local checks |
| [SETUP_WINDOWS.md](SETUP_WINDOWS.md) | PowerShell setup and local checks |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution status, workflow, code style, testing |
| [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) | Synchronized agent architecture and safety guidance |
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | Architecture and code walkthrough |
| [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) | Command cheat sheet |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Configuration precedence and fail-closed boundaries |
| [docs/DATABASE.md](docs/DATABASE.md) | PostgreSQL schema, migrations, and isolated local operation |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Local Docker topology and future release boundary |
| [docs/E2E_VERIFICATION_GUIDE.md](docs/E2E_VERIFICATION_GUIDE.md) | Recorded-data paper pipeline verification |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operational reference and broker evidence requirements |
| [docs/BROKER_CREDENTIALS.md](docs/BROKER_CREDENTIALS.md) | Credential ownership and per-account boundaries |
| [docs/STRATEGY_READINESS.md](docs/STRATEGY_READINESS.md) | Source inventory and unverified readiness boundaries |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Migration scope, validation, privacy review, and publication decisions |
| [strategies/indicator/USQualityCompounder/README.md](strategies/indicator/USQualityCompounder/README.md) | Equity portfolio design and paper blockers |
| [docs/SCALING.md](docs/SCALING.md) | Deferred scaling options and tenant isolation |
| [docs/REVIEWER_CHECKLIST.md](docs/REVIEWER_CHECKLIST.md) | Review and audit criteria |
| [scripts/README.md](scripts/README.md) | Script purposes and local verification usage |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community expectations; private reporting channel pending |
| [CHANGELOG.md](CHANGELOG.md) | Inherited technical history, not current deployment evidence |

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing code. Use conventional
commit messages, review staged files, and run the checks appropriate to the
change. Hosting, public contribution terms, and review ownership must be
established before external PR submission. The local migration does not publish
or open a pull request.
