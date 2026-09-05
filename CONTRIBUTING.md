# Contributing to vynmatrix

Thanks for your interest in contributing. This document captures the
development workflow for this independent codebase. Read [CLAUDE.md](CLAUDE.md)
first — it's the architectural source of truth and CONTRIBUTING.md
assumes you've already read it.

The project is not yet open-source. [LICENSE](LICENSE) is preserved unchanged;
publication, redistribution, and external contribution terms await a license/rights
decision. The local migration has no configured public repository or deployment.
Use the workflow below for authorized local work; repository hosting and PR
submission become relevant only after maintainers establish the destination.

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
vmdev build docker --from-config  # then config-declared service images

# Run tests
vmdev test all
```

For the full local-dev walkthrough, see [SETUP_MAC_LINUX.md](SETUP_MAC_LINUX.md)
or [SETUP_WINDOWS.md](SETUP_WINDOWS.md).

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

---

## Workflow

### Branch and ship

Work locally while the project has no published destination. Review staged paths
before committing; never add credentials, runtime databases, generated artifacts,
or personal configuration. Do not push or open a PR during the migration.

Once a hosting owner and contribution policy are established, contributors may use
a feature branch in their fork and open a PR against the reviewed destination.
The retained `vmdev git install` helper also supports a main-first workflow for
collaborators with write access: `git mr submit` creates a branch and PR, then
resets local `main`. It requires a configured `origin` and authenticated `gh`;
it is not part of offline setup. Example for that later hosted workflow:

```bash
git add <files>
git commit -m "<conventional message>"
git mr submit
```

Git helpers and aliases are installed in this repository only. The quality
wrapper and pre-push guard both live under `.githooks`, selected by
`core.hooksPath`; `pre-commit install-hooks` prepares their check environments.
The installed pre-push hook blocks direct pushes to `origin/main`. Follow the
repository's reviewed PR policy instead of bypassing that guard. Configure
`.github/CODEOWNERS` with real consenting maintainers only after the destination
repository exists.

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
surface in `market_data_ingestor`) keep `uvicorn` as the process supervisor and build with
`lib_common.app.fastapi.create_service_app` plus a FastAPI `lifespan` and `GracefulShutdown`.
Forcing an HTTP service into `ApplicationManager` splits the deploy contract — it adds a
second health endpoint on `HEALTH_CHECK_PORT` duplicating uvicorn's `/health` — without
removing real boilerplate.

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
| End-to-end pipeline | `tests/test_e2e_*.py` | Strategy → scoring → execution → feedback |
| Strategy contract tests | `tests/test_*strategy*.py` | Causality, malformed data, signal identity, lifecycle, and risk geometry |

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
