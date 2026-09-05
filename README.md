# vynmatrix

vynmatrix is a Python platform for signal generation, scoring, durable paper
execution, and feedback in one self-hosted deployment. It is an independent
migration: this repository carries no broker account, deployment, strategy
certification, or live-trading authority.

It is publicly source-available under the
[Vynmatrix Personal Noncommercial Reciprocity License 1.1](LICENSE). The
license permits personal, noncommercial use and requires source publication and
a pull request for every Enhancement; it is not an OSI-approved open-source
license. [NOTICE](NOTICE) preserves attribution and known fixture-provenance
limits.

## Scope

Indicator strategies emit canonical `Signal` records only. Scoring persists
decisions and execution commands through PostgreSQL's transactional outbox.
Execution then applies owner, broker-account, environment, instrument,
currency/FX, market-session, and risk authority before recording the canonical
order and fill ledger. Feedback evaluates those durable outcomes.

Local work must keep `EXECUTION_MODE=paper` and
`EXECUTION_ENGINE_ALLOW_LIVE=false`. A shipped strategy, a selected process, or
a registered database row never authorizes an order.

## Start here

Complete the platform-specific prerequisites in
[SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) or
[SETUP_WINDOWS.md](SETUP_WINDOWS.md), then continue with [SETUP.md](SETUP.md).
The supported local topology and the single-owner bootstrap contract are
documented in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/DATABASE.md](docs/DATABASE.md).

## Documentation

This is the canonical documentation index.

| Need | Canonical document |
| --- | --- |
| Shared local setup | [SETUP.md](SETUP.md) |
| macOS/Linux or Windows prerequisites | [SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md) / [SETUP_WINDOWS.md](SETUP_WINDOWS.md) |
| Contribution terms, branches, checks, and commits | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Commands and file locations | [docs/QUICK_REFERENCE.md](docs/QUICK_REFERENCE.md) |
| Runtime architecture and source navigation | [docs/USER_MANUAL.md](docs/USER_MANUAL.md) |
| Environment precedence and fail-closed settings | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| PostgreSQL bootstrap, migration, schema, and backup | [docs/DATABASE.md](docs/DATABASE.md) |
| Compose topology and release boundary | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Account and credential onboarding | [docs/BROKER_CREDENTIALS.md](docs/BROKER_CREDENTIALS.md) |
| Paper-pipeline evidence procedure | [docs/E2E_VERIFICATION_GUIDE.md](docs/E2E_VERIFICATION_GUIDE.md) |
| Runtime incidents and recovery | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Current strategy authority and readiness | [docs/STRATEGY_READINESS.md](docs/STRATEGY_READINESS.md) |
| Single-owner design decision | [docs/SINGLE_OWNER.md](docs/SINGLE_OWNER.md) |
| Event-driven delivery and bounded pools design | [docs/superpowers/specs/2026-09-05-event-driven-delivery-bounded-pools-design.md](docs/superpowers/specs/2026-09-05-event-driven-delivery-bounded-pools-design.md) |
| Deferred capacity work | [docs/SCALING.md](docs/SCALING.md) |
| Pull-request review | [docs/REVIEWER_CHECKLIST.md](docs/REVIEWER_CHECKLIST.md) |
| Script catalogue | [scripts/README.md](scripts/README.md) |
| Backend API contract | [apps/backend/README.md](apps/backend/README.md) |
| Strategy-specific contracts | [SwingHighLowPMO](strategies/indicator/SwingHighLowPMO/README.md) / [USQualityCompounder](strategies/indicator/USQualityCompounder/README.md) |
| Migration and publication history | [docs/MIGRATION.md](docs/MIGRATION.md) / [CHANGELOG.md](CHANGELOG.md) |
| License, attribution, and conduct | [LICENSE](LICENSE) / [NOTICE](NOTICE) / [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

Agent-specific repository guidance lives in [AGENTS.md](AGENTS.md) and
[CLAUDE.md](CLAUDE.md); the two files are intentionally synchronized.
