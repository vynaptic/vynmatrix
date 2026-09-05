# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

> **Keep in sync with [CLAUDE.md](CLAUDE.md).** The two files are intentionally
> near-identical — Codex reads this one, Claude Code reads `CLAUDE.md`. They may differ
> *only* where each names its own tool and its counterpart file: the title, the line
> above, and this note. Any other edit here must be applied to `CLAUDE.md` in the same
> change, and `diff CLAUDE.md AGENTS.md` must show nothing beyond those three places.

## Purpose and priorities

vynmatrix is an independent local migration of a multi-strategy trading codebase.
It is publicly source-available under [LICENSE](LICENSE), which limits use to
personal, noncommercial purposes and requires reciprocity for externally released
enhancements. It is not an OSI-approved open-source license; [NOTICE](NOTICE)
preserves attribution and identifies known third-party provenance gaps.
No live deployment, account, strategy certification, or release authority transfers
with this source tree. Favour correctness, explicit authority, auditability, and
fail-closed behaviour over convenience.

- Write production-ready code with type hints, useful errors, structured logging, and tests
  appropriate to the change.
- Do not add throwaway demos, examples, temporary files, generated reports, or duplicate
  documentation. Keep one source of truth per topic.
- Do not present fabricated market data or simulated signals as integration evidence.
  Deterministic fixtures are correct at unit boundaries; backtests, soaks, and end-to-end
  proofs must use recorded real historical data.
- Keep execution in paper mode with `EXECUTION_ENGINE_ALLOW_LIVE=false`. This
  migration authorizes local development only; never arm or change a live-order gate.
  Publishing, pushing, releasing, and deploying require separate owner authorization.
- Preserve unrelated user changes. Never rewrite or delete work merely to obtain a clean
  tree.

## Efficient working method

1. Read only what the task needs. Search with targeted `rg` before adding a file, class,
   function, schema, or configuration surface — this repository has been consolidated
   repeatedly, and the implementation you need usually already exists.
2. Reuse or extend the established implementation when it fits. Follow nearby patterns and
   the Domain → Application → Infrastructure dependency direction.
3. Research external sources only when the task depends on current APIs, standards,
   regulations, security guidance, or an unfamiliar design decision. Do not make web
   research a gate for routine repository work.
4. Compare alternatives when a consequential design choice actually exists. Otherwise make
   the narrowest reversible choice and proceed.
5. Work directly for routine tasks. Delegate only bounded, independent investigations that
   materially reduce elapsed time; pass minimal context and do not recursively delegate
   unless asked.
6. Start validation with the closest checks and expand once when risk or scope warrants it.
   Do not repeatedly run the entire suite while iterating.
7. Limit command output at the source with paths, filters, tails, or concise reporters.

Stop when the requested outcome is implemented and proportionately verified. Report what
changed, which checks ran, and any remaining risk.

A prebuilt knowledge graph may exist in `graphify-out/` (derived, gitignored, absent on a
fresh clone). The `/graphify` skill covers how to use it; only the repo-specific limits
measured here are worth remembering: query with exact symbol names rather than prose, do not
raise `--budget` to chase a missing answer (an untruncated result emits every edge, so a 10k
budget returned 32k tokens), prefer `explain`/`affected` over `query` for a single symbol,
treat `graphify path` as unreliable for architecture claims, and never bulk-read
`graphify-out/GRAPH_REPORT.md`.

## Architecture invariants

- The canonical runtime path is Strategy → canonical `Signal` → normalization/scoring →
  database persistence → transactional outbox → execution → feedback. A contract change
  must account for its real producers and consumers.
- Application-owned tables live under
  `libs/python/lib_application/lib_application/db/models/`. Signal and execution handoffs
  stay durable, idempotent, and database-backed.
- Use the canonical option-spread implementation in
  `libs/python/lib_strategy/lib_strategy/spreads/option_spreads.py`; do not add a parallel
  calculator. It requires an explicit broker-observed contract multiplier — never assume one
  from the symbol or asset class.
- The runtime `execution_engine.models.OrderIntent`, broker-wire
  `lib_strategy.types.BrokerOrderIntent`, and ORM `lib_application.db.models.oms.OrderIntent`
  are intentional boundary-specific types. Keep the explicit conversions; do not merge them
  because their names overlap.
- Every executable decision retains the concrete user, broker account, environment,
  currency/FX provenance, instrument identity, and applicable market-session authority.
  Missing, stale, mismatched, or ambiguous authority fails closed.
- Positions and execution metrics are projections, not restart-accounting inputs. Replay
  from the canonical execution ledger and preserve contract terms and fill-time FX data.
- Keep cash indices non-tradable. Execution targets an explicitly catalogued tradable
  contract. Do not guess venue schedules, currency parity, funding, liquidation, or tenant
  identity.
- One active deployment owner is designated explicitly through maintenance onboarding.
  Routine CLI/API callers cannot select another owner; retained historical user IDs and
  transaction-local RLS/account scope remain authoritative.
- Use the transactional outbox for scoring-to-execution delivery and preserve at-least-once
  delivery with idempotent consumers.

## Signal and strategy contracts

- `Signal` and `SignalAction` are defined once, in
  `libs/python/lib_strategy/lib_strategy/signals/signal.py`. Never declare a second copy —
  `vmdev audit` fails the build on duplicates.
- `SignalAction` is `LONG`/`SHORT`/`CLOSE`/`HOLD`; persistence writes `long`/`short`/`flat`/
  `hold`. Convert with `normalize_signal_action` / `normalize_scoring_action` from
  `libs/python/lib_strategy/lib_strategy/signals/normalization.py` rather than by hand.
- Indicator strategies are signal-only: emit via `emit_long()` / `emit_short()` /
  `emit_close()` and pass `timestamp=state.timestamp` so the deterministic signal id is
  stable. Never call a broker order API from `strategies/` — `vmdev audit` enforces this.
- Each indicator strategy exposes a `core.py` subclassing `PureSignalStrategy` plus a
  `config.json` with `runner_kind: signal_worker`; helper modules alongside it are fine
  (`USQualityCompounder` ships `panel.py`). There is no `main.py` and no strategy registry.
- Binding thresholds are **magnitude-based**: `abs(score) >= threshold`, with direction taken
  from the score's sign. Do not write `score >= threshold` — that silently drops shorts.
- Indicator source ships in the shared `vynmatrix/platform` image, but presence on disk does
  not authorize execution: the runtime `STRATEGY_LIST` is explicit and fail-closed, and the
  database strategy/version/binding gates remain authoritative.
- Asset-class values come from `libs/python/lib_common/lib_common/asset_classes.py`
  (`CANONICAL_ASSET_CLASSES`, `ASSET_CLASS_ALIASES`, `REFERENCE_ONLY_ASSET_CLASSES`,
  `TRADABLE_ASSET_CLASSES`, `SESSION_BASED_ASSET_CLASSES`). Use the constants; never
  hard-code the list and never coerce an ETF or cash index to equity.

## Change discipline

- Update code, schemas, event contracts, configuration, tests, and documentation only where
  the behaviour being changed genuinely reaches them. Do not make unrelated edits to satisfy
  a blanket sync rule.
- After a rename or removal, run one repository-wide stale-reference sweep. Add an
  `Unreleased` entry to `CHANGELOG.md` for notable operator- or user-visible changes, not
  for every internal edit.
- This repository uses conventional commits and pull requests. Do not introduce a parallel
  spec-driven workflow or OpenSpec tooling.
- Do not retain deprecated APIs or compatibility aliases without an identified external
  consumer and explicit approval.
- Keep `AGENTS.md` and `CLAUDE.md` synchronized as described at the top of each file.

## Commands and validation

Use repository tooling rather than installing packages or invoking build steps directly:

```bash
# First create and activate the tooling venv from the OS setup guide.
make setup
pre-commit install-hooks
vmdev build libs
vmdev build strategies
vmdev build venvs
vmdev build docker --from-config --tag latest
vmdev test lib --name=<library>
vmdev test team --team=<team>
vmdev test all
vmdev format
vmdev audit --strict
```

`vmdev git install` configures repository-local aliases and `.githooks` for both
the pre-commit quality wrapper and pre-push guard. `pre-commit install-hooks`
prepares check environments without replacing `core.hooksPath`; keep the prepared
`.venv-dev` active when committing.

`vmdev test` has exactly three subcommands — `all`, `lib`, `team`. There is no
`vmdev test strategy`. Strategy work uses the separate `vmdev strategy` group
(`validate`, `attest`, `attest-correctness`, `measure-costs`, `measure-data-parity`), which
freezes and attests bounded historical campaigns rather than running unit tests.

Run `vmdev test ...` from the prepared `.venv-dev` tooling/test environment: the CLI
uses its own Python interpreter to invoke pytest, not the component venvs. For one focused
test, run `python -m pytest <path-or-node-id>` from that same environment. Component
venvs isolate packaged runtime checks; strategy validation uses its dedicated venv.

Choose checks by scope:

- Documentation or agent configuration: validate links, syntax, and the exact diff. Do not
  start Docker.
- Localized code: formatting, lint, type checks, and the closest unit or component tests.
- Cross-service or PR-ready: affected suites, then `vmdev audit --strict`; `vmdev test all`
  when the blast radius is broad.
- Paper pipeline acceptance: follow `docs/E2E_VERIFICATION_GUIDE.md` in an isolated
  local Docker environment. This is evidence gathering, not deployment or live authority.

Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false` throughout ordinary
development and verification.

Give commands for the platform in use. Only when the user's platform is unknown, provide both
a macOS/Linux and a PowerShell form. `SETUP_WINDOWS.md` covers the Windows specifics, and not
every `scripts/*.sh` has a `.ps1` sibling.

## Architecture audit gates

`vmdev audit` blocks regressions that a previous consolidation removed. It runs in
pre-commit and CI; the rules and their thresholds live in
[`tools/dev_cli/dev_cli/commands/audit.py`](tools/dev_cli/dev_cli/commands/audit.py), which
is the source of truth — read it rather than trusting a copy.

It fails on: oversized files, a second `Signal`/`SignalAction` declaration, tracked build or
report artefacts, broker order calls inside `strategies/`, and broad exception handlers
beyond the recorded baseline. It warns on `sessionmaker`/`create_engine` outside
`lib_application.db.session` — use `get_session_factory()` and `dispose_engine()`.

When a gate fails, fix the cause. Raising a cap, widening the baseline, or adding an
exemption requires a justification in the same change.

## Safety boundaries

- Use only services and images declared in `docker/docker-compose.stack.yml` and
  `config/containers.yaml`. Get explicit approval before creating an ad-hoc container,
  image, tag, or topology, and remove approved temporary artifacts afterward. Prefer
  the `vmdev db bootstrap` lifecycle, which stops application/workers before its declared
  maintenance job. Keep at most three running containers including PostgreSQL; use `exec`
  in an existing slot for bounded operational jobs. Never use wildcard Compose profiles.
- Never run broad destructive filesystem, Git, Docker, cloud, or database operations without
  resolving the exact target and confirming the requested scope authorizes them. Prefer
  recoverable operations.
- Never expose, commit, or copy secrets into commands, logs, fixtures, documentation, or
  approval rules. Use the repository's credential and environment mechanisms.
- Database and event-contract changes require migration, rollback, idempotency, and
  downstream-consumer consideration. Fail closed when evidence is incomplete.

## Read only when relevant

Do not preload the documentation set; open the smallest relevant section. The canonical
index of every repository document is [README.md § Documentation](README.md#documentation).

- `CONTRIBUTING.md` — contribution contract, branch/ship workflow, commit and code style
- `docs/QUICK_REFERENCE.md` — command cheat sheet and file locations
- `docs/USER_MANUAL.md` — architecture and code walkthrough, including the end-to-end
  pipeline, user bindings, and the feedback loop
- `docs/DATABASE.md` — schema and migration guidance
- `docs/CONFIGURATION.md` — runtime configuration precedence and fail-closed requirements
- `docs/DEPLOYMENT.md` — release/deployment boundary and promotion gates
- `docs/BROKER_CREDENTIALS.md` — account and credential boundaries
- `docs/REVIEWER_CHECKLIST.md` — review and audit criteria
- `docs/E2E_VERIFICATION_GUIDE.md` — release/promotion proof only
- `SETUP_MAC_LINUX.md` / `SETUP_WINDOWS.md` — environment setup, including the PowerShell
  equivalents for Windows
