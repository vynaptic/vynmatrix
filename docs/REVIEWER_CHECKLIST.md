# Pull-request reviewer checklist

> Goal: keep the architectural improvements from the May 2026 codebase audit
> from regressing. Most rules below are also mechanically enforced by
> `vmdev audit` (pre-commit + CI). This checklist is what you read by eye —
> the rules the audit can't yet automate.

When reviewing a PR, walk through these in order.

## 1. Audit gate (mechanical)

- [ ] **CI `vmdev audit` job is green.** If it failed:
  - LOC cap → split the file, do not paper over with a cap raise.
  - Session drift → use `lib_application.db.session.get_session_factory()`.
  - Duplicate `Signal`/`SignalAction` → import canonical, do not re-declare.
  - Forbidden tracked artefacts (`build/`, `reports/`, `*_REPORT.md`, etc.) → add to `.gitignore`.
  - Indicator signal-only violation → emit a `Signal`, not a broker call.
  - Bare-except baseline regression → narrow the new catch or update `BARE_EXCEPT_BASELINE` with rationale.

### Enforced rules

[`tools/dev_cli/dev_cli/commands/audit.py`](../tools/dev_cli/dev_cli/commands/audit.py) is
the source of truth; this table is a reviewer's summary. Flags: `--staged`, `--strict`
(warnings become errors), `--json`.

| Rule | Severity | Checks |
|---|---|---|
| `file-loc-cap` | error | production `.py` and `tools/` > 1 800 LOC; `tests/` > 2 500; `models/` submodule > 600 |
| `duplicate-signal-type` | error | `class Signal`/`class SignalAction` outside `lib_strategy/signals/signal.py` |
| `indicator-signal-only` | error | broker order calls in `strategies/indicator/` (nine forms, incl. `Order`, `LimitOrder`, `StopMarketOrder`, `MarketOnOpenOrder`, `MarketOnCloseOrder`) |
| `forbidden-tracked` | error | tracked file under `build/`, `*.egg-info/`, `reports/`, or matching `*_REPORT.{md,txt}` / `*_SUMMARY.md` / `*_ANALYSIS.md` / `*_REVIEW.md` |
| `bare-except-baseline` | error | broad handler count across `apps/` above `BARE_EXCEPT_BASELINE` |
| `bare-except-baseline-shrink` | warning | count below baseline — lower the constant to lock the gain in |
| `broad-except-scan-syntax` | error | a file under `apps/` will not parse |
| `env-parse-drift` | error | numeric/boolean env parsing not using `lib_common.env_utils` |
| `logger-canonical-drift` | warning | direct `logging.getLogger(` outside `lib_common/logging.py` |
| `session-drift-sessionmaker` | warning | `sessionmaker(...)` outside `lib_application.db.session` |
| `session-drift-create-engine` | warning | direct `create_engine(...)` outside the canonical helper |
| `runtime-dependency-contract` | error | Docker profiles must use exact constraint-matching pins without duplicating `svc-base` |
| `first-party-dependency-contract` | error | first-party dependency truth |
| `docker-python-base-drift` | error | `docker/*.Dockerfile` base must match `global.python_version` in `config/build.yaml` |
| `broker-capability-catalogue-drift` | error | `lib_infrastructure/brokers/capabilities.py` vs `docker/seed/02_seed_data.sql` |

`MIGRATION_EXEMPTIONS` downgrades a LOC error to a warning for an in-flight refactor; **every
entry must carry a tracking-PR reference**, and it is not a parking lot (currently empty).
`SESSION_DRIFT_ALLOWLIST` holds a handful of one-shot CLI paths — add with a justifying
comment rather than silencing the warning.

## 2. Domain layering (eyeball)

The repo follows a Clean-Architecture-ish layering. New code should respect it.

- [ ] **Production code lives in the right layer.**
  - `libs/python/lib_strategy/` — domain types (Signal, scoring types, ports, normalisation). No I/O, no DB, no broker logic.
  - `libs/python/lib_common/` — cross-cutting utilities (logging, config, retries, env, event bus, ApplicationManager). No domain logic.
  - `libs/python/lib_application/` — services + ORM. Imports `lib_strategy`, never the reverse.
  - `libs/python/lib_infrastructure/` — adapter implementations (broker adapters, SQLAlchemy repositories). Implements ports defined in `lib_strategy.ports`.
  - `apps/*` — orchestration. Imports services + ports; should not contain new domain logic.
- [ ] **No reverse imports.** `lib_strategy` never imports `lib_application` or `apps/`. `lib_common` never imports anything else from this repo.
- [ ] **Apps depend on ports, not implementations,** wherever a port already exists in `lib_strategy/ports/`. New repository-style integrations should add a port too.

## 3. Decomposition discipline

- [ ] **No new orchestration responsibilities on `ExecutionEngine` or any class > 1 200 LOC.** Add a focused collaborator and inject it. The obsolete `BacktestService` no longer exists.
- [ ] **No "while you're here" feature changes** in a refactor PR (and vice versa). Refactor PRs should be pure restructuring with golden tests as proof of behaviour preservation.
- [ ] **New collaborator extracted from a god class** has:
  - A module docstring stating its single responsibility.
  - A constructor that takes its dependencies explicitly (no implicit globals).
  - The original class delegates with **the same private/public method names** so existing tests + monkeypatch sites keep working.

## 4. Tests

- [ ] **Every new module has at least one test file.** A new collaborator class without unit tests is a tracking-debt PR, not a refactor.
- [ ] **Golden snapshots used responsibly.** Snapshot tests should assert on a *small* normalised dict, not entire JSON dumps. Use them only for cross-cutting contracts (e.g., `ExecutionResult.to_dict()` shape) where unit tests would miss interaction.
- [ ] **No tests that depend on live external services in default CI.** Kite, TrueData, Coinbase, etc. tests must be marked `@pytest.mark.integration` or behind an env flag.
- [ ] **E2E tests assert per-layer state**, not just final-output shape. The pipeline is Strategy → Scoring → DB → Execution → Feedback; a meaningful E2E test inspects all five.
- [ ] **Evidence identifies its boundary.** Current-time real-data transport,
  normal historical stale rejection, explicit historical paper/accounting replay,
  and fixture-based failure-mode tests are separate proofs. Skipped PostgreSQL
  checks and running containers are not acceptance evidence.

## 5. Public-API hygiene

- [ ] **No deprecated exports or compatibility aliases are introduced.** This
  repository has no external package consumers; coordinated internal callers
  move to the canonical API in the same change.
- [ ] **Same-named delegates preserved during decomposition.** When extracting a class, the original method signatures stay so `monkeypatch.setattr(engine, "_xxx", ...)` test rigs continue to work.
- [ ] **DB model relocations re-export from `models/__init__.py`** so external `from lib_application.db.models import X` and Alembic discovery both keep working.

## 6. Operational scripts

- [ ] **New script in `scripts/` is documented in `scripts/README.md`** (Purpose, Usage, Owner team).
- [ ] **The declared runtime stays within three running containers, including
  PostgreSQL.** Use application/workers groups or the combined `all` group, the
  shared `vynmatrix/platform` image, and explicit selectors. Maintenance stops
  both runtime groups before its one-shot job; bounded operational jobs use
  `exec` inside an existing group. Review the canonical
  [database lifecycle](DATABASE.md), not a parallel seed/startup path.
- [ ] **Child roles and progress remain isolated.** Runtime children receive
  their own database login and required service/admin keys, never maintenance
  credentials. Management health must remain distinct from feed/execution
  readiness; shutdown must reap descendants within the group deadline.
- [ ] **No new `*_REPORT.md` / `*_SUMMARY.md` / `*_ANALYSIS.md` / `*_REVIEW.md`** at repo root. Use `docs/historical/` for archived snapshots; ephemeral reports go under `reports/` (gitignored).
- [ ] **Stakeholder CSVs / backtest exports** go in `reports/` (gitignored). Do not commit them.

## 7. SQLAlchemy + Alembic

- [ ] **New ORM model added to `models/<domain>.py`,** not `models/__init__.py`.
- [ ] **`models/__init__.py` re-exports the new class** so `from lib_application.db.models import X` keeps working.
- [ ] **Alembic round-trip verified** if the model split changed (i.e., a new migration runs cleanly + downgrades).
- [ ] **Cross-domain `relationship("OtherClass")` strings** still resolve. SQLAlchemy resolves these via `Base.registry`, which works as long as every domain submodule is imported by `models/__init__.py` before any relationship is accessed.
- [ ] **Owner boundaries preserve history.** Owner designation is explicit
  maintenance authority; routine CLI/API changes share owner-relative application
  services and retain account/ledger identity. No implicit user selection,
  runtime owner designation, demo account authority, or commercial-data deletion
  is introduced. A populated commercial-retirement guard must block before DDL.

## 8. Indicator-strategy contract (CRITICAL)

- [ ] **No `self.Buy(...)`, `self.Sell(...)`, `self.MarketOrder(...)`, `self.Liquidate(...)`, etc. in `strategies/indicator/`.** Indicator strategies are signal-only — emit a canonical `Signal` instead. The audit blocks this; a passing audit means this is fine.
- [ ] **Registration, canary activation and promotion stay separate.** Registration
  is inactive. `vmdev db activate-canary` requires exact registered, enabled,
  dev-only `E2E_PIPELINE_CANARY_ONLY` source and explicit paper/live-false gates;
  it enables no account/binding and cannot promote a strategy. Swing remains
  permanently excluded from paper promotion and live trading. Any other candidate
  needs its own [readiness evidence](STRATEGY_READINESS.md).
- [ ] **Image evidence names the actual platform image.** The logical attestation
  role `indicator-runner` maps to `vynmatrix/platform`; retired-image manifests
  are rejected, not relabeled. An image/config/evidence change requires newly
  matched attestation and any independently eligible promotion evidence.

## 9. Documentation

- [ ] **README / CLAUDE.md updates accompany architectural changes.** If you added a new app, port, or top-level module, update both.
- [ ] **`scripts/README.md` ownership table** reflects any new scripts.
- [ ] **`docs/historical/`** entries are dated and clearly marked as point-in-time snapshots (not current state).

## 10. Sanity

- [ ] **PR title** uses conventional prefix (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`).
- [ ] **PR body has a Test plan** with explicit checkbox items for what the author ran locally.
- [ ] **Commit history is reviewable** — squash-and-merge is fine, but a sprawling stack of "fix typo" / "wip" commits in the PR signals the author wasn't done before opening it.

---

## When something needs to be a follow-up PR (not blocking the current one)

If you spot:

- A god-class to decompose,
- A duplicate utility to consolidate,
- A test gap not covered by this PR,

…**file an issue and link it from the PR description.** Do not stack
unrelated cleanup into a refactor PR; the audit gate exists so we can ship
small, focused changes without losing track.
