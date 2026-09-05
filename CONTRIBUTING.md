# Contributing to vynmatrix

Thanks for your interest in contributing. This document captures the
development workflow for this independent codebase. Read [CLAUDE.md](CLAUDE.md)
first — it's the architectural source of truth and CONTRIBUTING.md
assumes you've already read it.

vynmatrix is publicly source-available under the
[Vynmatrix Personal Noncommercial Reciprocity License 1.0](LICENSE). It is not
an OSI-approved open-source license. Read [LICENSE](LICENSE) and [NOTICE](NOTICE)
before using or contributing to the project.

## License and contribution terms

The license permits personal, noncommercial use. If you externally distribute
an enhancement or make it publicly available through a service, the license
requires you to publish its complete corresponding source under the same terms
and submit a good-faith pull request to
[`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix) within 30 days.
Private changes that are not externally released do not trigger that condition.
Maintainers may accept, reject, or request changes to a pull request.

By opening a pull request, you represent that you have the right to contribute
the change and license it under the project license. Preserve existing copyright,
attribution, and notice files, and add notices required for material you add.

---

## Quick start

```bash
# First follow the OS setup guide to create .venv-dev and install the
# constraints-pinned tooling/test dependencies. Keep that venv active.
make setup                 # creates dirs, installs vmdev CLI + local Git helpers
pre-commit install-hooks   # prepares environments for the .githooks/pre-commit wrapper

# Build everything
make build-wheels          # libraries and strategies first
vmdev build venvs          # then app/strategy venvs
vmdev build docker --from-config  # then the config-declared platform image

# Run tests
vmdev test all
```

For the full local-dev walkthrough, see [SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md)
or [SETUP_WINDOWS.md](SETUP_WINDOWS.md).

For installation, use the explicit owner and role configuration in
[docs/DATABASE.md](docs/DATABASE.md). The supported runtime is PostgreSQL plus
`application` and `workers`, or PostgreSQL plus the combined `all` group in the
same application service. Bootstrap stops runtime before its declared maintenance
job; bounded backfill/replay work executes inside an existing group. Do not add
automatic seeds, extra service containers, or a parallel database lifecycle.

---

## The contract

The code contains broker execution paths; ordinary development stays in paper
mode with the live gate disabled. Before submitting a change, confirm:

- [ ] **Pre-commit hooks pass.** They include `mypy`, `ruff`, `ruff-format`,
      syntax/format checks, and `vmdev audit`. Do not bypass them with `--no-verify`.
- [ ] **`vmdev audit` is clean.** Zero errors and (ideally) zero
      warnings. The audit gates against god classes, duplicate canonical
      types, session drift, tracked build artefacts, indicator strategies
      that bypass the signal-only contract, and bare-except baseline
      regressions. See [Architecture audit gates](CLAUDE.md#architecture-audit-gates).
- [ ] **Tests pass.** `vmdev test all` (or at minimum the libs and the
      apps your change touches).
- [ ] **Type hints on new code.** mypy is configured strict for
      `lib_strategy` and tightening across other libs over time.
- [ ] **Docs updated.** Update the relevant API, configuration, or contributor
      documentation when behavior changes. Keep `CLAUDE.md` and `AGENTS.md` in sync
      when agent guidance changes.
- [ ] **Owner and execution authority remain explicit.** Preserve historical
      user/account IDs and ledger attribution. Routine APIs resolve the designated
      owner, require authenticated access, and share application services with the
      CLI. Keep maintenance credentials out of runtime child environments.

---

## Workflow

### Branch and ship

Review staged paths before committing; never add credentials, runtime databases,
generated artifacts, or personal configuration. Use a feature branch in a fork
or working checkout, then open a pull request against
[`vynaptic/vynmatrix`](https://github.com/vynaptic/vynmatrix).

The retained `vmdev git install` helper supports a main-first workflow for
collaborators with write access: `git mr submit` creates a branch and pull
request, then resets local `main`. It requires a configured `origin` and
authenticated `gh`:

```text
git add <files>
git commit -m "<conventional message>"
git mr submit
```

Git helpers and aliases are installed in this repository only. The quality
wrapper and pre-push guard both live under `.githooks`, selected by
`core.hooksPath`; `pre-commit install-hooks` prepares their check environments.
Git cannot prevent a local commit on `main`, but the installed pre-push hook and
GitHub branch policy reject direct remote changes to `main`. Submit a pull
request, satisfy the required `ci-gate` check, and resolve conversations before
merge. [`.github/CODEOWNERS`](.github/CODEOWNERS) routes default review to
`@vynaptic`; it does not replace the server-side branch policy.

### Commit messages

Conventional commits with imperative voice:

- `feat: ...` — new feature
- `fix: ...` — bug fix
- `refactor: ...` — restructure without behaviour change
- `test: ...` — add / change tests
- `docs: ...` — documentation only
- `chore: ...` — maintenance

Example:

```
refactor: extract ScoreCalculator from ScoreEngine

Score-axis aggregation now lives in a focused helper class.
Tests pass; bare-except baseline 73 → 71.
```

---

## Code style

| Topic | Rule |
|---|---|
| Line length | 100 |
| Formatter and import order | `ruff format` + `ruff check` (run via `vmdev format`) |
| Linter | `ruff` (config in `pyproject.toml`) |
| Type checker | `mypy` (strict on `lib_strategy`, gradual elsewhere) |
| Exception messages | Bind to a variable first (`msg = ...; raise ValueError(msg)`) |
| Bare excepts | Avoid. The audit baseline ratchets down each PR. Use typed catches. Document boundary catches with `# noqa: BLE001 - reason`. |
| Logging in except | `logger.exception("...")`, not `logger.error("...", e)` |
| Re-raises | `raise X(...) from e` |

The pre-commit hooks enforce most of this. Run `vmdev format` before
committing to fix mechanical issues.

A few rules that trip people up and are not obvious from the table:

| Rule | Do this instead |
|---|---|
| `TRY300` | Return from the `else` block, not from inside `try` |
| `PLR2004` | Name the constant (`SYMBOL_PARTS_COUNT = 2`) rather than comparing to a literal |
| `RUF022` | Keep `__all__` alphabetically sorted |
| `ARG002` | Prefix an intentionally unused argument with `_` |
| `PLW0603` | Prefer a module-level accessor; if a singleton genuinely needs `global`, document it |
| Return types | Annotate explicitly; do not let a function return `Any` implicitly |

### When you're tempted to add `# noqa`

`# noqa` is a last resort, not a first solution. Work down this list before reaching for it:

1. **Fix the underlying issue** — restructure the code, add the missing type, refactor the import.
2. **If it is genuinely unfixable, document why** — a one-line reason after the code.
3. **Use the specific rule code** — `# noqa: E402`, never a bare `# noqa`.

Never silence these; fix them instead: missing annotations (`ANN*`), import order you could
restructure (`E402`), magic values (`PLR2004`), unused variables (`F841`).

These are legitimate, *with* a reason attached:

```python
# Imports that must follow a sys.path manipulation
sys.path.insert(0, str(Path(__file__).parent / "libs"))
from lib_application.db.models import Base  # noqa: E402 - must follow sys.path.insert

# Optional dependency imported at runtime
import optional_lib  # noqa: PLC0415 - optional dependency, imported lazily

# Signature fixed by an interface you do not control
def on_event(self, event: Event, _context: Context) -> None:  # noqa: ARG002 - interface requirement

# Singleton that genuinely needs module state
global _instance  # noqa: PLW0603 - singleton pattern requires global
```

The audit treats `# noqa: BLE001` on a bare-except as a documented boundary catch; every
other `# noqa` needs its one-line reason.

---

## Adding new code

Before writing anything new, **search for existing implementations**.
The audit team already consolidated lots of duplicate work; there's
probably a base class, helper, or service for what you're trying to
do. [docs/USER_MANUAL.md § Canonical symbol index](docs/USER_MANUAL.md#canonical-symbol-index)
maps the symbols you are most likely to re-invent to the file that already owns them.

| Building... | Subclass / use... |
|---|---|
| New indicator strategy | `lib_strategy.signals.PureSignalStrategy` |
| New FastAPI service | `lib_common.app.create_service_app` |
| New ApplicationManager-style worker | `lib_common.app.ApplicationManager` |
| New DB session | `lib_application.db.session.get_session_factory` |
| New event over the outbox | `lib_application.outbox.OutboxStore` + a `lib_common.internal_events.PlatformEvent` subclass |

**Pick the right base for a service.** `ApplicationManager` is for foreground worker loops —
`apps/indicator_runner` is the only app that uses it. The FastAPI services
(`scoring_engine`, `execution_engine`, `feedback_loop_engine`, `backend`, and the FastAPI
surface in `market_data_ingestor`) keep `uvicorn` as their HTTP server and build with
`lib_common.app.fastapi.create_service_app` plus a FastAPI `lifespan` and `GracefulShutdown`.
Forcing an HTTP service into `ApplicationManager` splits the deploy contract — it adds a
second health endpoint on `HEALTH_CHECK_PORT` duplicating uvicorn's `/health` — without
removing real boilerplate.

The container-level supervisor is `scripts/run_platform.py`; HTTP and worker
entrypoints remain isolated child processes with their own role, listener and
lifecycle. Extend that composition for an existing process responsibility rather
than creating another container or moving HTTP services into the indicator manager.

---

## Testing

`vmdev test` has only `all`, `lib --name=...`, and `team --team=...` subcommands.
It runs pytest with the CLI's current interpreter, so activate the prepared
`.venv-dev` before testing. The pytest source paths in `pyproject.toml` make
in-repository packages available without installing each application.

```bash
vmdev test lib --name=lib_common
python -m pytest libs/python/lib_common/tests  # focused test directory
vmdev test all
vmdev audit --strict
pre-commit run --all-files
```

Use an actual existing test path for focused work. PostgreSQL-backed tests need
a disposable local test database and their documented environment opt-ins;
credentialed broker tests are separate from default checks. Do not point tests at
personal or shared runtime databases. CI's exact dependency and database setup
is in [ci.yml](.github/workflows/ci.yml). Recorded market history is required for
paper pipeline evidence, as described in the [E2E guide](docs/E2E_VERIFICATION_GUIDE.md).

| Layer | Where | What |
|---|---|---|
| Per-library unit tests | `libs/python/lib_*/tests/` | Pure functions, ports |
| Per-app tests | `apps/*/tests/` | Service / engine logic |
| Integration | `tests/test_*.py` | Cross-component flows |
| Recorded-data PostgreSQL pipeline | `tests/test_public_strategy_pipeline_postgres_integration.py` | Exact account-scoped persistence and paper accounting; separate from the current-time Docker soak |
| Strategy contract tests | `tests/test_*strategy*.py` | Causality, malformed data, signal identity, lifecycle, and risk geometry |

The exact Swing development canary is `E2E_PIPELINE_CANARY_ONLY` and permanently
excluded from paper promotion and live trading. Its narrow maintenance activation
command verifies the already registered dev-only release; it creates no account,
binding, worker selector, or promotion authority. Follow the
[E2E guide](docs/E2E_VERIFICATION_GUIDE.md) for that command and the separate
current-time, historical-safety, replay/accounting, and failure-mode evidence.

---

## Reviewing PRs

The team uses [`docs/REVIEWER_CHECKLIST.md`](docs/REVIEWER_CHECKLIST.md) —
read it before approving anything. The audit catches mechanical
regressions; the checklist covers what the audit can't (architectural
intent, taste).

---

## Getting help

- Architecture / design questions: open a draft PR and ask in the description.
- Onboarding: SETUP_MAC_LINUX.md / SETUP_WINDOWS.md.
- Commands cheat-sheet: docs/QUICK_REFERENCE.md.
