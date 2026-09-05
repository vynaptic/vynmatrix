"""Architecture-quality audit checks.

A single ``vmdev audit`` command bundles a set of mechanical guardrails that
the May 2026 codebase audit identified as the smallest set of repeatable
checks that meaningfully prevent the code-quality regressions we just
finished cleaning up:

* God classes (file length).
* Duplicate canonical types (``SignalAction``/``Signal`` redefinitions).
* Multiple SQLAlchemy ``sessionmaker`` factories (drift across libs/apps).
* Direct ``create_engine`` calls outside the canonical helper.
* Hand-rolled numeric/boolean environment parsing outside ``lib_common.env_utils``.
* Tracked build artefacts (``build/``, ``*.egg-info/``).
* Tracked stakeholder/report artefacts (``reports/``, ``*_REPORT.md``,
  ``*_SUMMARY.md``, ``*_ANALYSIS.md``, ``*_REVIEW.md``).
* Broad ``except Exception`` / ``except BaseException`` count baseline,
  including handlers that bind the exception with ``as``.
* Forbidden imports under ``strategies/indicator/`` (direct broker-order
  calls — indicator strategies must remain signal-only per CLAUDE.md).
* Models that grew past the post-split ceiling.
* Exact, non-duplicated Docker runtime profiles and statically auditable wheel
  dependencies, with unused/optional heavy packages kept out of production.
* Broker seed capabilities matching the runtime routing matrix.

Modes:
    ``vmdev audit``                - run all checks against the working tree.
    ``vmdev audit --staged``       - check only files in the git index (used
                                     by the pre-commit hook).
    ``vmdev audit --strict``       - fail on warnings as well as errors.
    ``vmdev audit --json``         - emit a machine-readable report.

The command is intentionally fast (~1 second) so it can run on every
pre-commit + every PR without slowing the developer loop.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Tunable thresholds. Each is a CONSCIOUS upper bound — raising it requires a
# code-review conversation about whether the file should be decomposed
# instead of letting the limit slide.
# ---------------------------------------------------------------------------

#: Maximum LOC for any production Python file. Files over this limit are
#: god-class candidates that the May 2026 audit broke up; 1 800 gives
#: meaningful headroom for legitimate orchestrators while still catching new
#: sprawl early.
PRODUCTION_FILE_MAX_LOC = 1_800

#: Test files get a more lenient cap (golden-output fixtures + parametrised
#: scenarios legitimately push test files higher than production code).
TEST_FILE_MAX_LOC = 2_500

#: Developer utilities (``tools/``) are not production runtime — research
#: scripts, optimization harnesses, etc. — but are held to the same 1 800-LOC
#: bound as production now that the FVGAlgoLong reconciliation harness is gone.
TOOLS_FILE_MAX_LOC = 1_800

#: Models live in ``libs/python/lib_application/lib_application/db/models/``. After
#: the historical 2 006-LOC ``models.py`` was split into per-domain
#: submodules, no single submodule should be this big again.
MODELS_FILE_MAX_LOC = 600

#: The current broad-exception baseline across ``apps/``. Audit reduces this
#: gradually — never let it grow past the recorded count without an explicit
#: reviewer override. Update this number in the same PR that legitimately
#: adds new boundary catches.
#: 2026-05-04: lowered from 114 → 92 after PR #12 narrowed risk_guard,
#: signal_worker, dispatcher, and feedback engine boundaries.
#: 2026-05-04: lowered from 92 → 80 after PR-G narrowed
#: ``execution_engine/persistence.py`` (7 sites → 0) and the DB-fetch /
#: NAV-write paths in ``execution_engine/metrics/pnl_service.py``
#: (5 sites narrowed; 2 remain as documented boundary catches around
#: per-symbol P&L compute and third-party market-data fetch).
#: 2026-05-04: lowered from 80 → 73 after PR-J narrowed
#: ``scoring_engine/storage.py`` (5 sites → 0) and the
#: instrument-resolution + canonical-write paths in
#: ``scoring_engine/engine.py`` (2 sites narrowed; the four
#: ``list_signals_by_*`` boundary catches remain ``noqa: BLE001``
#: because the store interface is pluggable and tests pin that
#: contract — see ``test_market_score_returns_zero_when_store_scan_fails``).
#: 2026-05-04: lowered from 73 → 71 after PR-L extracted
#: ``ScoreCalculator`` from ``scoring_engine/engine.py``. The four
#: per-axis ``list_signals_by_*`` boundary catches collapsed into a
#: single ``_fetch_or_scan`` helper in ``_score_calculator.py``, so
#: net 4 sites in ``engine.py`` → 2 sites in ``_score_calculator.py``.
#: 2026-05-05: lowered 71 → 45 (Sprint A bare-except narrowing + formatter
#: collapse). Narrowed DB query wrappers in
#: ``execution_engine/metrics/strategy_metrics.py``,
#: ``execution_engine/deduplication.py``, ``execution_engine/engine.py``,
#: ``execution_engine/pending_orders.py``, ``scoring_engine/providers_db.py``
#: from ``except Exception`` to ``except (SQLAlchemyError, OSError)``.
#: Narrowed enum constructors in ``execution_engine/engine.py`` and
#: ``execution_engine/broker_resolver.py`` to ``except ValueError``.
#: Narrowed config loaders in ``execution_engine/main.py`` and
#: ``scoring_engine/main.py`` to ``except (ValueError, ValidationError)``.
#: Programming bugs (AttributeError, KeyError, TypeError) now surface
#: instead of being silently swallowed; documented boundary catches
#: (shutdown, broker bridge, request boundary) remain broad with comments.
#: ``vmdev format`` then collapsed multiline ``except (..., ...):`` blocks
#: that the regex was already counting once each, dropping the count by 4.
#: 2026-05-07: lowered 45 → 42 (Sprint G). Narrowed DB updates in
#: ``execution_engine/metrics/drawdown_service.py`` (2 sites) to
#: ``except (SQLAlchemyError, OSError)``. Narrowed price-provider
#: construction fallback in ``feedback_loop_engine/main.py`` to
#: ``except (ValueError, TypeError, OSError)``.
#: 2026-05-09: raised 42 → 46 after the lint gate normalized four existing
#: documented boundary catches from multiline ``except (Exception):`` to
#: single-line ``except Exception:``. Broad-catch surface unchanged:
#: two in ``execution_engine/metrics/pnl_service.py`` and two in
#: ``scoring_engine/_score_calculator.py``.
#: 2026-06-19: lowered 46 → 45 after removing the dead aggregate-PnL boundary
#: catch in ``execution_engine/execution_persistence.update_daily_nav_snapshot``.
#: DailyNav was subsequently rebuilt as account-scoped accounting state.
#: 2026-06-23: lowered 45 → 44 after deleting the dead Layer-4 PositionService /
#: PositionDecision scoring machinery (its boundary catch went with it).
#: 2026-06-27: lowered 44 → 42 after refactoring ``execution_engine/alerts.py`` to
#: delegate to the shared ``lib_common.alerting`` multi-sink publisher — its two
#: best-effort webhook-send boundary catches moved into lib_common (outside the
#: apps/ gate), where a failing alert channel must not break the caller.
#: 2026-06-27: raised 42 → 43 for the PL-2 FIFO-vs-broker position drift check
#: (``reconciliation._fifo_position_findings``). It is an OPTIONAL, warn-only
#: drift check; any failure must NEVER abort the reconciliation cycle (the
#: caller's boundary would open the circuit breaker and halt trading), so a
#: broad catch is the safe-by-default choice — a narrow tuple cannot honor that.
#: 2026-07-02: raised 43 → 44 for the ``apps/backend`` config-API startup.
#: 2026-07-25: removed that broad catch. An explicitly selected, misconfigured
#: secrets backend is startup-fatal; local backend instead defaults to the
#: readable ``env`` provider and only credential-write routes fail closed (503).
#: That reduction is included in the 53 → 46 ratchet recorded below.
#: 2026-07-02: raised 44 → 46 for two broker-boundary catches in the naked-position
#: / reconciliation fixes: (1) ``order_executor._cancel_resting_orders`` — a failed
#: bracket-leg cancel (network, already-filled race) must never block the CLOSE
#: that flattens a live position; (2) ``reconciliation._stale_order_terminal_status``
#: — a pending-order status lookup failing mid-cycle must never abort reconciliation
#: (the caller's boundary would open the circuit breaker and halt trading). Both are
#: warn-and-continue paths against an open-ended broker/API failure surface.
#: 2026-07-24: expanded the scanner from a regex that only matched
#: ``except Exception:`` to an AST check that also catches
#: ``except Exception as exc``, tuple handlers containing Exception, bare
#: handlers, and BaseException. The 46 → 53 change records the seven existing
#: aliased handlers; it does not add runtime catches.
#: 2026-07-25: lowered the AST baseline 53 → 46 after the final implementation
#: pass removed seven broad catches from production app code (four after the
#: interim 50-count ratchet). The measured working-tree count is the ratchet;
#: future reductions must lower this value.
#: 2026-07-25: lowered 46 → 45 after the dormant strategy-version retirement
#: removed its final broad application-layer compatibility boundary.
#: 2026-07-25: lowered 45 → 44 after market-data scheduler failures were made
#: process-fatal (with finally cleanup) instead of being swallowed as a
#: false-green service shutdown.
#: 2026-07-26: lowered 44 → 43 by consolidating both reconciliation order
#: lookups behind the existing broker/API boundary and making unexpected
#: paper-lifecycle task failures process-fatal.
#: 2026-07-29: lowered 43 → 42 after deleting the unreachable scoring-engine
#: canonical-signal mirror path together with its boundary catch.
#: 2026-07-30: raised 42 → 43 for the scoring dispatcher's in-worker provider
#: resolver (``_resolve_provider_in_worker``): a deliberate caller-supplied
#: provider boundary catch mirroring ``_resolve_provider``'s fail-open
#: contract, required because the ingest unit of work now runs on a worker
#: thread where the async resolver cannot be awaited.
#: 2026-09-06: lowered 43 -> 42. Retiring the ``execution.results`` outbox
#: event removed the boundary catch in
#: ``execution_persistence._emit_result_message_standalone``; no handler was added.
BARE_EXCEPT_BASELINE = 42


#: Files where a known refactor is in-flight. The cap is still checked, but
#: the violation is downgraded to a warning. Each entry MUST carry a
#: tracking-issue / PR reference so the exemption is removed once the
#: refactor lands. Listing the file here is a contract: the team has read
#: the audit, agrees the refactor is the fix, and commits to delivering it.
#:
#: When you add an entry, also add the migration ticket to your sprint —
#: this list is not a parking lot.
MIGRATION_EXEMPTIONS: dict = {}


# ---------------------------------------------------------------------------
# Discovery patterns. Kept module-level so unit tests can introspect them.
# ---------------------------------------------------------------------------

PRODUCTION_DIRS = ("apps", "libs")
TOOLS_DIRS = ("tools",)
TEST_DIRS = ("tests",)

#: Files under these paths are exempt from the LOC cap (vendored, build
#: artefacts, etc.). Patterns are matched against ``str(path)``.
LOC_EXEMPT_PATTERNS = (
    re.compile(r"(?:^|/)build/"),
    re.compile(r"\.egg-info/"),
    re.compile(r"__pycache__"),
    re.compile(r"/\.venv-dev/"),
)

#: Tracked build / stakeholder-report artefacts that should be gitignored.
FORBIDDEN_TRACKED_PATTERNS = (
    re.compile(r"(?:^|/)build/"),
    re.compile(r"\.egg-info(?:/|$)"),
    re.compile(r"(?:^|/)reports/"),
    re.compile(r"_REPORT\.(?:md|txt)$"),
    re.compile(r"_SUMMARY\.md$"),
    re.compile(r"_ANALYSIS\.md$"),
    re.compile(r"_REVIEW\.md$"),
)

#: Direct broker-order calls indicator strategies are forbidden from making
#: per CLAUDE.md (signal-only contract).
FORBIDDEN_INDICATOR_CALLS = re.compile(
    r"self\.(?:Buy|Sell|MarketOrder|Liquidate|StopMarketOrder|LimitOrder|"
    r"MarketOnOpenOrder|MarketOnCloseOrder|Order)\("
)

#: ``SignalAction``/``Signal`` are canonical — they live in
#: ``libs/python/lib_strategy/lib_strategy/signals/signal.py``. Any other definition
#: is a duplication regression.
CANONICAL_SIGNAL_TYPE_PATH = "libs/python/lib_strategy/lib_strategy/signals/signal.py"

#: Canonical session/engine helper. Anything else creating ``sessionmaker``
#: or calling ``create_engine`` directly is drift.
CANONICAL_SESSION_PATH = "libs/python/lib_application/lib_application/db/session.py"

#: Allow-listed call sites that legitimately fall outside the canonical
#: session helper (test rigs, migrations, vendored packages).
SESSION_DRIFT_ALLOWLIST = {
    # Alembic must construct an engine before any
    # canonical helper exists.
    "scripts/db/alembic/env.py",
    # One-shot drift diagnostic: builds a throwaway migration-built DB and
    # compares it to the models; cannot use the app session factory.
    "scripts/db/check_schema_drift.py",
    # ``_StoreInfra`` (the AppScoreStore base) keeps its own engine builder so
    # it can switch on ``StaticPool`` for in-memory SQLite test rigs (the
    # canonical helper intentionally doesn't expose pool-class kwargs).
    "apps/scoring_engine/scoring_engine/_storage_base.py",
    # One-shot operational CLIs that don't run inside an app process and
    # therefore don't have an ``ApplicationManager``-managed engine to
    # inherit. Each is documented in ``scripts/README.md``.
    "scripts/replay_canonical_signals.py",
    # The audit module itself contains the regex strings it scans for
    # (``"sessionmaker("`` / ``"create_engine("`` literals inside
    # ``check_session_drift``'s warning messages). Self-matching is the
    # expected outcome, not a real drift site.
    "tools/dev_cli/dev_cli/commands/audit.py",
    # Tests routinely build ad-hoc engines for in-memory SQLite.
}
SESSION_DRIFT_ALLOWLIST_PATTERNS = (re.compile(r"(^|/)tests?(/|$)"),)


#: Single source of truth for the enforced interpreter: ``config/build.yaml``
#: ``global.python_version`` (the value the vmdev builder actually consumes).
#: Every ``docker/*.Dockerfile`` that bases off ``python:<tag>`` must match its
#: major.minor. A Dependabot base-image bump that skips ``build.yaml`` (as the
#: 3.11 -> 3.14 bump did) ships an interpreter the locked wheels were never
#: built or tested for — and the slim runtime images have no compiler to fall
#: back to a source build.
BUILD_CONFIG_PATH = "config/build.yaml"
#: Parse ``global.python_version`` without importing PyYAML — the CI audit job
#: installs only click+rich, so audit.py must stay dependency-light.
PYTHON_REQUIRED_RE = re.compile(r"^\s*python_version:\s*['\"]?(\d+)\.(\d+)", re.MULTILINE)
DOCKERFILE_FROM_RE = re.compile(r"^\s*FROM\s+(\S+)", re.MULTILINE)
DOCKERFILE_ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)", re.MULTILINE)
RUNTIME_REQUIREMENTS_PATHS = (
    "docker/requirements-svc-base.txt",
    "docker/requirements-platform.txt",
)
RUNTIME_CONSTRAINTS_PATH = "docker/constraints.txt"
RUNTIME_SETUP_GLOBS = (
    "libs/python/*/setup.py",
    "apps/*/setup.py",
    "strategies/*/setup.py",
)
BROKER_CAPABILITIES_PATH = (
    "libs/python/lib_infrastructure/lib_infrastructure/brokers/capabilities.py"
)
BROKER_SEED_PATH = "docker/seed/02_seed_data.sql"
BROKER_SEED_BLOCK_RE = re.compile(
    r"INSERT\s+INTO\s+brokers\s*\([^)]*\)\s*VALUES(?P<rows>.*?)"
    r"ON\s+CONFLICT\s*\(code\)",
    re.IGNORECASE | re.DOTALL,
)
BROKER_SEED_ROW_RE = re.compile(
    r"\(\s*'(?P<code>[^']+)'\s*,\s*'[^']+'\s*,\s*'(?P<payload>\{[^']+\})'\s*\)"
)
REQUIREMENT_PIN_RE = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;#]+)\s*(?:#.*)?$"
)
REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
FORBIDDEN_MANDATORY_RUNTIME_PACKAGES = frozenset(
    {
        "pyarrow",
        "ta-lib",
    }
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single audit hit."""

    rule: str
    severity: str  # "error" | "warning"
    path: str
    message: str
    extra: dict = field(default_factory=dict)


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Locate the repo root by walking up to ``.git``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / ".git").is_dir():
            return parent
    return here.parents[3]


def _staged_files(root: Path) -> list[Path]:
    """Return absolute paths of files in the current git index (staged)."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=root,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [root / line.strip() for line in out.splitlines() if line.strip()]


def _all_worktree_files(root: Path) -> list[Path]:
    """Return every tracked or non-ignored untracked worktree file.

    The default audit validates the code a developer is about to stage, so a
    newly created production module must not remain invisible until after its
    first ``git add``. Ignored build artefacts remain excluded by Git's normal
    ignore rules. ``--staged`` continues to inspect only the index.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [root / line.strip() for line in out.splitlines() if line.strip()]


def _is_loc_exempt(rel: str) -> bool:
    return any(pat.search(rel) for pat in LOC_EXEMPT_PATTERNS)


def _count_loc(path: Path) -> int:
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def _load_runtime_broker_catalogue(path: Path) -> dict[str, dict[str, list[str]]]:
    """Load the stdlib-only broker matrix without importing its package."""
    module_name = "_vmdev_broker_capabilities"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load broker capability matrix from {path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return {
            str(item.code): item.capabilities.catalogue_payload() for item in module.BROKER_SPECS
        }
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def _load_seed_broker_catalogue(path: Path) -> dict[str, dict[str, list[str]]]:
    """Parse broker capability documents from the canonical SQL seed."""
    match = BROKER_SEED_BLOCK_RE.search(path.read_text(encoding="utf-8"))
    if match is None:
        msg = f"Cannot find canonical broker INSERT in {path}"
        raise RuntimeError(msg)
    catalogue: dict[str, dict[str, list[str]]] = {}
    for row in BROKER_SEED_ROW_RE.finditer(match.group("rows")):
        code = row.group("code")
        payload = json.loads(row.group("payload"))
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), list)
            for key in ("asset_classes", "order_types", "features", "regions")
        ):
            msg = f"Broker {code!r} has an invalid capability seed payload"
            raise TypeError(msg)
        catalogue[code] = payload
    if not catalogue:
        msg = f"Canonical broker INSERT in {path} contains no parseable rows"
        raise RuntimeError(msg)
    return catalogue


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_file_loc(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Flag files over the LOC cap for their domain."""
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if _is_loc_exempt(rel):
            continue
        loc = _count_loc(path)

        if rel.startswith("libs/python/lib_application/lib_application/db/models/"):
            cap = MODELS_FILE_MAX_LOC
            label = "models-domain submodule"
        elif any(rel.startswith(d + "/") for d in TEST_DIRS) or "/tests/" in rel:
            cap = TEST_FILE_MAX_LOC
            label = "test file"
        elif any(rel.startswith(d + "/") for d in TOOLS_DIRS):
            cap = TOOLS_FILE_MAX_LOC
            label = "tools utility"
        elif any(rel.startswith(d + "/") for d in PRODUCTION_DIRS):
            cap = PRODUCTION_FILE_MAX_LOC
            label = "production file"
        else:
            continue

        if loc > cap:
            exemption_reason = MIGRATION_EXEMPTIONS.get(rel)
            severity = "warning" if exemption_reason is not None else "error"
            tail = (
                f" Migration exemption: {exemption_reason}"
                if exemption_reason
                else " Extract a collaborator class or split by domain."
            )
            report.add(
                Finding(
                    rule="file-loc-cap",
                    severity=severity,
                    path=rel,
                    message=f"{label} is {loc} LOC (cap {cap}).{tail}",
                    extra={"loc": loc, "cap": cap, "exempt": bool(exemption_reason)},
                )
            )


def check_duplicate_signal_types(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """``SignalAction`` / ``Signal`` must only be defined in the canonical file."""
    pattern = re.compile(r"^class (Signal|SignalAction)[\s(:]", re.MULTILINE)
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if rel == CANONICAL_SIGNAL_TYPE_PATH or _is_loc_exempt(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in pattern.finditer(text):
            cls = match.group(1)
            report.add(
                Finding(
                    rule="duplicate-signal-type",
                    severity="error",
                    path=rel,
                    message=(
                        f"Redefined ``{cls}`` outside the canonical "
                        f"``{CANONICAL_SIGNAL_TYPE_PATH}``. Import the canonical "
                        "type instead of redeclaring it."
                    ),
                )
            )


#: Canonical structured-logger helper. Anything calling ``logging.getLogger``
#: directly (outside the helper itself) is drift — Sprint B migrated 27 sites
#: off ``logging.getLogger(__name__)`` to ``lib_common.logging.get_logger``
#: so the entire codebase routes through the structured wrapper. The wrapper
#: supports keyword-style structured logs (``logger.info("msg", k=v)``); a
#: plain ``logging.Logger`` does not.
CANONICAL_LOGGER_PATH = "libs/python/lib_common/lib_common/logging.py"

#: Files allowed to call ``logging.getLogger`` directly (the canonical helper
#: itself, plus any legitimate stdlib logger configuration).
LOGGER_DRIFT_ALLOWLIST = {
    # The canonical helper IS the place that calls ``logging.getLogger``.
    CANONICAL_LOGGER_PATH,
    # ``vmdev`` CLI ships its own rich-based logger — intentionally NOT routed
    # through the structured wrapper because the CLI prints to a TTY, not a
    # log aggregator.
    "tools/dev_cli/dev_cli/utils/logger.py",
    # The audit module itself contains the regex literal it scans for
    # (``"logging.getLogger("`` inside ``check_logger_canonical_drift``).
    # Self-matching is the expected outcome, not a real drift site.
    "tools/dev_cli/dev_cli/commands/audit.py",
}

#: Path-prefix patterns for files allowed to call ``logging.getLogger``.
#: ``scripts/`` and ``tools/reconciliation/`` are one-shot operational CLIs
#: that don't run inside an app process and therefore don't benefit from the
#: structured wrapper.
LOGGER_DRIFT_ALLOWLIST_PATTERNS = (
    re.compile(r"(^|/)scripts/"),
    re.compile(r"(^|/)tools/reconciliation/"),
)


def check_logger_canonical_drift(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Direct ``logging.getLogger(...)`` calls outside the canonical helper."""
    pattern = re.compile(r"\blogging\.getLogger\s*\(")
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if _is_loc_exempt(rel) or "/tests/" in rel or "/test_" in rel:
            continue
        if rel in LOGGER_DRIFT_ALLOWLIST:
            continue
        if any(pat.search(rel) for pat in LOGGER_DRIFT_ALLOWLIST_PATTERNS):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        # Only flag when the file imports stdlib ``logging`` — otherwise the
        # match is a false positive on a docstring / comment.
        if "import logging" not in text:
            continue
        report.add(
            Finding(
                rule="logger-canonical-drift",
                severity="warning",
                path=rel,
                message=(
                    "Direct ``logging.getLogger(...)`` outside "
                    f"``{CANONICAL_LOGGER_PATH}``. Use "
                    "``lib_common.logging.get_logger`` instead so the module "
                    "gets the structured-logging adapter."
                ),
            )
        )


def check_session_drift(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Multiple ``sessionmaker`` factories or direct ``create_engine`` calls."""
    sessionmaker_pattern = re.compile(r"\bsessionmaker\s*\(")
    create_engine_pattern = re.compile(r"\b(?:sa_)?create_engine\s*\(")

    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if rel == CANONICAL_SESSION_PATH or _is_loc_exempt(rel):
            continue
        if rel in SESSION_DRIFT_ALLOWLIST:
            continue
        if any(pat.search(rel) for pat in SESSION_DRIFT_ALLOWLIST_PATTERNS):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if sessionmaker_pattern.search(text) and "from sqlalchemy.orm" in text:
            report.add(
                Finding(
                    rule="session-drift-sessionmaker",
                    severity="warning",
                    path=rel,
                    message=(
                        "``sessionmaker(...)`` outside the canonical "
                        f"``{CANONICAL_SESSION_PATH}``. Use "
                        "``lib_application.db.session.get_session_factory`` instead."
                    ),
                )
            )
        if create_engine_pattern.search(text) and "from sqlalchemy" in text:
            report.add(
                Finding(
                    rule="session-drift-create-engine",
                    severity="warning",
                    path=rel,
                    message=(
                        "Direct ``create_engine(...)`` outside "
                        f"``{CANONICAL_SESSION_PATH}``. Use "
                        "``lib_application.db.session.get_session_factory`` instead."
                    ),
                )
            )


def check_forbidden_tracked(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Build artefacts + stakeholder reports must not be tracked."""
    for path in files:
        rel = str(path.relative_to(root))
        for pat in FORBIDDEN_TRACKED_PATTERNS:
            if pat.search(rel):
                report.add(
                    Finding(
                        rule="forbidden-tracked",
                        severity="error",
                        path=rel,
                        message=(
                            "This path matches a gitignore-pattern that is "
                            "build/report output and must not be tracked. "
                            "Add it to ``.gitignore`` and remove from the index."
                        ),
                        extra={"pattern": pat.pattern},
                    )
                )


def check_indicator_signal_only(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Indicator strategies must not place broker orders directly."""
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if not rel.startswith("strategies/indicator/"):
            continue
        if _is_loc_exempt(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in FORBIDDEN_INDICATOR_CALLS.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            report.add(
                Finding(
                    rule="indicator-signal-only",
                    severity="error",
                    path=rel,
                    message=(
                        f"Direct broker-order call ``{match.group(0)}`` at line "
                        f"{line_no}. Indicator strategies must remain signal-only "
                        "per CLAUDE.md — emit a canonical ``Signal`` instead."
                    ),
                    extra={"line": line_no, "call": match.group(0)},
                )
            )


_BOOLEAN_ENV_LITERALS = frozenset(
    {
        "",
        "0",
        "1",
        "disabled",
        "enabled",
        "f",
        "false",
        "n",
        "no",
        "off",
        "on",
        "t",
        "true",
        "y",
        "yes",
    }
)


def _is_os_env_getter(node: ast.AST, env_aliases: set[str] | None = None) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "getenv":
        return isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
    if node.func.attr != "get" or not isinstance(node.func.value, ast.Attribute):
        return bool(
            env_aliases
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in env_aliases
        )
    owner = node.func.value
    return owner.attr == "environ" and isinstance(owner.value, ast.Name) and owner.value.id == "os"


def _unwrap_env_string_normalization(node: ast.AST) -> ast.AST:
    current = node
    while (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Attribute)
        and current.func.attr in {"casefold", "lower", "strip"}
        and not current.args
    ):
        current = current.func.value
    return current


def _scope_for(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda, ast.Module)):
            return current
    return current


def _assigned_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _is_env_mapping_expression(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ):
        return True
    if isinstance(node, ast.IfExp):
        return _is_env_mapping_expression(node.body, aliases) or _is_env_mapping_expression(
            node.orelse, aliases
        )
    return False


def _is_env_string_expression(
    node: ast.AST,
    *,
    env_aliases: set[str],
    env_values: set[str],
) -> bool:
    source = _unwrap_env_string_normalization(node)
    if _is_os_env_getter(source, env_aliases):
        return True
    return isinstance(source, ast.Name) and source.id in env_values


def _environment_dataflow(
    tree: ast.AST,
) -> tuple[dict[ast.AST, set[str]], dict[ast.AST, set[str]], dict[ast.AST, ast.AST]]:
    """Track simple local aliases without treating ordinary string reads as errors."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    assignments_by_scope: dict[ast.AST, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        assignments_by_scope.setdefault(_scope_for(node, parents), []).append(node)

    aliases_by_scope: dict[ast.AST, set[str]] = {}
    values_by_scope: dict[ast.AST, set[str]] = {}
    for scope, assignments in assignments_by_scope.items():
        aliases: set[str] = set()
        values: set[str] = set()
        changed = True
        while changed:
            changed = False
            for assignment in assignments:
                name = _assigned_name(assignment)
                value = _assignment_value(assignment)
                if name is None or value is None:
                    continue
                if name not in aliases and _is_env_mapping_expression(value, aliases):
                    aliases.add(name)
                    changed = True
                if name not in values and _is_env_string_expression(
                    value,
                    env_aliases=aliases,
                    env_values=values,
                ):
                    values.add(name)
                    changed = True
        aliases_by_scope[scope] = aliases
        values_by_scope[scope] = values
    return aliases_by_scope, values_by_scope, parents


def _boolean_compare_literals(node: ast.Compare) -> set[str]:
    values: set[str] = set()
    for comparator in node.comparators:
        candidates = (
            comparator.elts if isinstance(comparator, (ast.List, ast.Set, ast.Tuple)) else []
        )
        if isinstance(comparator, ast.Constant):
            candidates = [comparator]
        values.update(
            str(candidate.value).lower()
            for candidate in candidates
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
        )
    return values


def check_env_parse_drift(root: Path, files: Sequence[Path], report: AuditReport) -> None:
    """Require the canonical validated parsers for numeric and boolean env values."""
    for path in files:
        if not path.is_file() or path.suffix != ".py":
            continue
        rel = str(path.relative_to(root))
        if not rel.startswith(("apps/", "libs/")) or _is_loc_exempt(rel) or "/tests/" in rel:
            continue
        if rel == "libs/python/lib_common/lib_common/env_utils.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (OSError, SyntaxError):
            continue

        aliases_by_scope, values_by_scope, parents = _environment_dataflow(tree)
        for node in ast.walk(tree):
            scope = _scope_for(node, parents)
            env_aliases = aliases_by_scope.get(scope, set())
            env_values = values_by_scope.get(scope, set())
            raw_parse = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"float", "int"}
                and bool(node.args)
                and _is_env_string_expression(
                    node.args[0],
                    env_aliases=env_aliases,
                    env_values=env_values,
                )
            )
            bool_parse = False
            if isinstance(node, ast.Compare):
                literals = _boolean_compare_literals(node)
                bool_parse = (
                    _is_env_string_expression(
                        node.left,
                        env_aliases=env_aliases,
                        env_values=env_values,
                    )
                    and bool(literals)
                    and literals <= _BOOLEAN_ENV_LITERALS
                )
            if not raw_parse and not bool_parse:
                continue
            line = getattr(node, "lineno", 0)
            report.add(
                Finding(
                    rule="env-parse-drift",
                    severity="error",
                    path=rel,
                    message=(
                        f"Hand-rolled environment parsing at line {line}. "
                        "Use parse_int_env, parse_float_env, or parse_bool_env "
                        "from lib_common.env_utils."
                    ),
                    extra={"line": line},
                )
            )


def check_bare_except_baseline(root: Path, report: AuditReport) -> None:
    """Refuse to grow the broad-exception surface beyond the audit baseline.

    Parsing the syntax tree avoids the previous blind spot for
    ``except Exception as exc`` and multiline/tuple handlers. A syntax error
    is itself an audit error instead of silently hiding handlers from the
    count.
    """

    def is_broad(handler: ast.ExceptHandler) -> bool:
        if handler.type is None:
            return True
        candidate_types = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else (handler.type,)
        )
        return any(
            isinstance(candidate, ast.Name) and candidate.id in {"Exception", "BaseException"}
            for candidate in candidate_types
        )

    apps_dir = root / "apps"
    if not apps_dir.is_dir():
        return
    count = 0
    for py in apps_dir.rglob("*.py"):
        rel = str(py.relative_to(root))
        if _is_loc_exempt(rel) or "/tests/" in rel:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            report.add(
                Finding(
                    rule="broad-except-scan-syntax",
                    severity="error",
                    path=rel,
                    message=(
                        "Cannot audit exception handlers because the file has "
                        f"invalid syntax at line {exc.lineno}: {exc.msg}."
                    ),
                    extra={"line": exc.lineno, "error": exc.msg},
                )
            )
            continue
        count += sum(
            1 for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and is_broad(node)
        )

    if count > BARE_EXCEPT_BASELINE:
        report.add(
            Finding(
                rule="bare-except-baseline",
                severity="error",
                path="apps/",
                message=(
                    f"Broad exception-handler count is {count}, baseline is "
                    f"{BARE_EXCEPT_BASELINE}. Either narrow the new catches "
                    "to typed exceptions or update ``BARE_EXCEPT_BASELINE`` in "
                    "``tools/dev_cli/dev_cli/commands/audit.py`` with a "
                    "comment justifying the new boundary catch."
                ),
                extra={"count": count, "baseline": BARE_EXCEPT_BASELINE},
            )
        )
    elif count < BARE_EXCEPT_BASELINE:
        # Hint to lower the baseline so future regressions are caught earlier.
        report.add(
            Finding(
                rule="bare-except-baseline-shrink",
                severity="warning",
                path="apps/",
                message=(
                    f"Broad exception-handler count is {count}, below the "
                    f"recorded baseline {BARE_EXCEPT_BASELINE}. Lower "
                    "``BARE_EXCEPT_BASELINE`` to lock in the improvement."
                ),
                extra={"count": count, "baseline": BARE_EXCEPT_BASELINE},
            )
        )


def _resolve_dockerfile_ref(ref: str, args: dict[str, str]) -> str:
    """Substitute ``${VAR}`` / ``${VAR:-default}`` in a Dockerfile token."""

    def repl(match: re.Match[str]) -> str:
        expr = match.group(1)
        if ":-" in expr:
            name, _, default = expr.partition(":-")
            return args.get(name.strip(), default)
        return args.get(expr.strip(), "")

    return re.sub(r"\$\{([^}]+)\}", repl, ref)


def check_docker_python_base(root: Path, report: AuditReport) -> None:
    """``FROM python:<tag>`` in every Dockerfile must match the locked interpreter.

    Only Dockerfiles whose (ARG-resolved) base is ``python:`` are checked;
    ``quantconnect/lean`` and ``${BASE_IMAGE}`` bases are skipped. This is the
    gate that would have failed the 3.11 -> 3.14 Dependabot bump.
    """
    dep_path = root / BUILD_CONFIG_PATH
    docker_dir = root / "docker"
    if not dep_path.is_file() or not docker_dir.is_dir():
        return
    try:
        dep_text = dep_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    required = PYTHON_REQUIRED_RE.search(dep_text)
    if not required:
        return
    required_minor = f"{required.group(1)}.{required.group(2)}"

    for dockerfile in sorted(docker_dir.rglob("*Dockerfile*")):
        if not dockerfile.is_file():
            continue
        try:
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        args = {m.group(1): m.group(2) for m in DOCKERFILE_ARG_RE.finditer(text)}
        for from_match in DOCKERFILE_FROM_RE.finditer(text):
            image = _resolve_dockerfile_ref(from_match.group(1), args)
            if not image.startswith("python:"):
                continue
            tag = image[len("python:") :]
            version = re.match(r"(\d+)\.(\d+)", tag)
            if not version:
                continue
            found_minor = f"{version.group(1)}.{version.group(2)}"
            if found_minor == required_minor:
                continue
            report.add(
                Finding(
                    rule="docker-python-base-drift",
                    severity="error",
                    path=str(dockerfile.relative_to(root)),
                    message=(
                        f"Base image ``python:{tag}`` is Python {found_minor}, but "
                        f"``build.yaml`` requires {required_minor}. A base-image "
                        "bump must be matched by a deliberate bump of "
                        "build.yaml global.python_version in the same change — "
                        f"wheels are built and locked for Python {required_minor}, and "
                        "the slim runtime images carry no compiler for a source build."
                    ),
                    extra={"found": found_minor, "required": required_minor},
                )
            )


def _normalized_requirement_name(name: str) -> str:
    """Return the PEP 503 comparison form without importing packaging."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _read_pinned_requirements(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read exact pins and return non-comment lines that are not exact pins."""

    pins: dict[str, str] = {}
    invalid: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = REQUIREMENT_PIN_RE.fullmatch(line)
        if match is None:
            invalid.append(line)
            continue
        name = _normalized_requirement_name(match.group(1))
        if name in pins:
            invalid.append(f"duplicate requirement: {name}")
            continue
        pins[name] = match.group(2)
    return pins, invalid


def _literal_requirement_values(
    node: ast.expr,
    assignments: dict[str, ast.expr],
    *,
    resolving: frozenset[str] = frozenset(),
) -> tuple[bool, list[str]]:
    """Resolve a static setup.py requirement list without executing packaging code."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True, [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[str] = []
        for element in node.elts:
            target = element.value if isinstance(element, ast.Starred) else element
            known, nested = _literal_requirement_values(
                target,
                assignments,
                resolving=resolving,
            )
            if not known:
                return False, values
            values.extend(nested)
        return True, values
    if isinstance(node, ast.Name) and node.id in assignments and node.id not in resolving:
        return _literal_requirement_values(
            assignments[node.id],
            assignments,
            resolving=resolving | {node.id},
        )
    return False, []


def _check_setup_runtime_dependencies(root: Path, report: AuditReport) -> None:
    """Keep optional/heavy packages out of wheel ``install_requires`` metadata."""

    setup_paths = sorted(
        {path for pattern in RUNTIME_SETUP_GLOBS for path in root.glob(pattern) if path.is_file()}
    )
    for path in setup_paths:
        relative = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError) as exc:
            report.add(
                Finding(
                    rule="runtime-dependency-contract",
                    severity="error",
                    path=relative,
                    message=f"Cannot inspect setup.py runtime dependencies: {exc}",
                )
            )
            continue
        assignments: dict[str, ast.expr] = {}
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                assignments[statement.targets[0].id] = statement.value
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.value is not None
            ):
                assignments[statement.target.id] = statement.value

        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            is_setup = isinstance(call.func, ast.Name) and call.func.id == "setup"
            is_setup = is_setup or (
                isinstance(call.func, ast.Attribute) and call.func.attr == "setup"
            )
            if not is_setup:
                continue
            install_keyword = next(
                (keyword for keyword in call.keywords if keyword.arg == "install_requires"),
                None,
            )
            if install_keyword is None:
                continue
            known, requirements = _literal_requirement_values(
                install_keyword.value,
                assignments,
            )
            if not known:
                report.add(
                    Finding(
                        rule="runtime-dependency-contract",
                        severity="error",
                        path=relative,
                        message=(
                            "install_requires must be a static string list so vmdev audit "
                            "can prove the production dependency closure."
                        ),
                    )
                )
                continue
            names = {
                _normalized_requirement_name(match.group(1))
                for requirement in requirements
                if (match := REQUIREMENT_NAME_RE.match(requirement)) is not None
            }
            forbidden = sorted(names & FORBIDDEN_MANDATORY_RUNTIME_PACKAGES)
            if forbidden:
                report.add(
                    Finding(
                        rule="runtime-dependency-contract",
                        severity="error",
                        path=relative,
                        message=(
                            "Optional/unused heavy packages cannot be mandatory wheel "
                            f"dependencies: {forbidden}"
                        ),
                    )
                )


def check_runtime_dependency_contract(root: Path, report: AuditReport) -> None:
    """Keep Docker dependency profiles deterministic, minimal, and non-duplicated."""

    constraints_path = root / RUNTIME_CONSTRAINTS_PATH
    base_path = root / RUNTIME_REQUIREMENTS_PATHS[0]
    if not constraints_path.is_file() or not base_path.is_file():
        return
    constraints, invalid_constraints = _read_pinned_requirements(constraints_path)
    if invalid_constraints:
        report.add(
            Finding(
                rule="runtime-dependency-contract",
                severity="error",
                path=RUNTIME_CONSTRAINTS_PATH,
                message=f"Runtime constraints must be exact pins: {invalid_constraints}",
            )
        )
        return

    base_pins, _ = _read_pinned_requirements(base_path)
    base_names = set(base_pins)
    for relative in RUNTIME_REQUIREMENTS_PATHS:
        path = root / relative
        if not path.is_file():
            report.add(
                Finding(
                    rule="runtime-dependency-contract",
                    severity="error",
                    path=relative,
                    message="Configured runtime requirements file is missing.",
                )
            )
            continue
        pins, invalid = _read_pinned_requirements(path)
        if invalid:
            report.add(
                Finding(
                    rule="runtime-dependency-contract",
                    severity="error",
                    path=relative,
                    message=f"Runtime requirements must be exact pins: {invalid}",
                )
            )
        forbidden = sorted(set(pins) & FORBIDDEN_MANDATORY_RUNTIME_PACKAGES)
        if forbidden:
            report.add(
                Finding(
                    rule="runtime-dependency-contract",
                    severity="error",
                    path=relative,
                    message=(
                        "Optional/unused heavy packages cannot enter the DigitalOcean "
                        f"runtime closure: {forbidden}"
                    ),
                )
            )
        if relative != RUNTIME_REQUIREMENTS_PATHS[0]:
            duplicated = sorted(set(pins) & base_names)
            if duplicated:
                report.add(
                    Finding(
                        rule="runtime-dependency-contract",
                        severity="error",
                        path=relative,
                        message=f"Service profile duplicates svc-base pins: {duplicated}",
                    )
                )
        drift = sorted(
            f"{name}=={version} (constraint: {constraints.get(name, 'missing')})"
            for name, version in pins.items()
            if constraints.get(name) != version
        )
        if drift:
            report.add(
                Finding(
                    rule="runtime-dependency-contract",
                    severity="error",
                    path=relative,
                    message=f"Runtime pins drift from {RUNTIME_CONSTRAINTS_PATH}: {drift}",
                )
            )
    _check_setup_runtime_dependencies(root, report)


def check_broker_capability_catalogue(root: Path, report: AuditReport) -> None:
    """Reject drift between runtime broker routing and database seed metadata."""
    runtime_path = root / BROKER_CAPABILITIES_PATH
    seed_path = root / BROKER_SEED_PATH
    if not runtime_path.exists() and not seed_path.exists():
        return
    if not runtime_path.is_file() or not seed_path.is_file():
        missing = [
            relative
            for relative, path in (
                (BROKER_CAPABILITIES_PATH, runtime_path),
                (BROKER_SEED_PATH, seed_path),
            )
            if not path.is_file()
        ]
        report.add(
            Finding(
                rule="broker-capability-catalogue-drift",
                severity="error",
                path=", ".join(missing),
                message="Broker capability parity cannot be audited because a source is missing",
            )
        )
        return

    try:
        runtime = _load_runtime_broker_catalogue(runtime_path)
        seeded = _load_seed_broker_catalogue(seed_path)
    except (
        AttributeError,
        ImportError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
    ) as exc:
        report.add(
            Finding(
                rule="broker-capability-catalogue-drift",
                severity="error",
                path=BROKER_SEED_PATH,
                message=f"Broker capability parity check failed closed: {exc}",
            )
        )
        return

    seeded.pop("paper", None)
    if runtime.keys() != seeded.keys():
        report.add(
            Finding(
                rule="broker-capability-catalogue-drift",
                severity="error",
                path=BROKER_SEED_PATH,
                message=(
                    "Broker code sets differ between runtime and seed: "
                    f"runtime={sorted(runtime)}, seed={sorted(seeded)}"
                ),
            )
        )
        return

    for code, expected in runtime.items():
        actual = seeded[code]
        if actual != expected:
            report.add(
                Finding(
                    rule="broker-capability-catalogue-drift",
                    severity="error",
                    path=BROKER_SEED_PATH,
                    message=(
                        f"Broker {code!r} seed capabilities differ from runtime: "
                        f"expected={expected!r}, actual={actual!r}"
                    ),
                    extra={"broker": code},
                )
            )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


#: ``from lib_x ...`` / ``import lib_x`` at any indentation — first-party
#: imports are the ground truth the dependency metadata must cover.
FIRST_PARTY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(lib_[a-z0-9_]+)", re.MULTILINE)
#: Quoted first-party entries inside ``install_requires`` (both spellings:
#: pip normalizes ``lib-common`` and ``lib_common`` identically per PEP 503).
SETUP_FIRST_PARTY_RE = re.compile(r"\"(lib[-_][a-z0-9_]+)\s*[\">=<~!]")
_FIRST_PARTY_SCAN_EXCLUDES = ("tests", "build", "__pycache__")


def _parse_build_components(text: str) -> list[tuple[str, str, set[str]]]:
    """Parse (name, path, dependencies) triples from ``config/build.yaml``.

    Line-based on purpose: the CI audit job installs only click+rich, so this
    module must stay PyYAML-free. Only the ``- name/path/dependencies`` keys
    are consumed; any other list key (e.g. ``strategy_groups``) terminates the
    dependency block.
    """
    components: list[tuple[str, str, set[str]]] = []
    name: str | None = None
    path: str | None = None
    deps: set[str] = set()
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            if name and path:
                components.append((name, path, deps))
            name = stripped.split(":", 1)[1].strip()
            path = None
            deps = set()
            in_deps = False
        elif stripped.startswith("path:"):
            path = stripped.split(":", 1)[1].strip()
            in_deps = False
        elif stripped.startswith("dependencies:"):
            in_deps = True
        elif in_deps and stripped.startswith("- "):
            deps.add(stripped[2:].strip())
        elif in_deps:
            in_deps = False
    if name and path:
        components.append((name, path, deps))
    return components


def check_first_party_dependency_contract(root: Path, report: AuditReport) -> None:
    """Every direct ``lib_*`` import must be declared in setup.py AND build.yaml.

    Wheel metadata is the statically auditable dependency contract (production
    images install wheels with ``--no-deps`` then ``pip check``, which only
    validates DECLARED metadata), and ``build.yaml`` drives venv construction.
    An undeclared first-party import passes both gates green while breaking
    any consumer that trusts the metadata — the two graphs must not diverge.
    """
    build_path = root / BUILD_CONFIG_PATH
    if not build_path.is_file():
        return
    try:
        build_text = build_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    for name, component_path, build_deps in _parse_build_components(build_text):
        if not component_path.startswith(("libs/", "apps/")):
            continue
        component_dir = root / component_path
        setup_path = component_dir / "setup.py"
        if not component_dir.is_dir() or not setup_path.is_file():
            continue

        imported: set[str] = set()
        for source in component_dir.rglob("*.py"):
            relative_parts = source.relative_to(component_dir).parts
            if any(part in _FIRST_PARTY_SCAN_EXCLUDES for part in relative_parts) or any(
                part.endswith(".egg-info") for part in relative_parts
            ):
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            imported.update(FIRST_PARTY_IMPORT_RE.findall(text))
        imported.discard(name)

        try:
            setup_text = setup_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        declared = {m.replace("-", "_") for m in SETUP_FIRST_PARTY_RE.findall(setup_text)}
        build_declared = {d.replace("-", "_") for d in build_deps}

        missing_setup = sorted(imported - declared)
        missing_build = sorted(imported - build_declared)
        if missing_setup:
            report.add(
                Finding(
                    rule="first-party-dependency-contract",
                    severity="error",
                    path=str(setup_path.relative_to(root)),
                    message=(
                        f"{name} imports {', '.join(missing_setup)} but install_requires "
                        "does not declare them — wheel metadata must cover every direct "
                        "first-party import (images install with --no-deps; pip check "
                        "cannot see undeclared gaps)."
                    ),
                )
            )
        if missing_build:
            report.add(
                Finding(
                    rule="first-party-dependency-contract",
                    severity="error",
                    path=BUILD_CONFIG_PATH,
                    message=(
                        f"{name} imports {', '.join(missing_build)} but its build.yaml "
                        "dependencies list omits them — the venv build graph must cover "
                        "every direct first-party import."
                    ),
                )
            )


def run_audit(*, staged: bool, root: Path | None = None) -> AuditReport:
    """Run all checks. Pure function for unit tests."""
    root = root or _repo_root()
    files = _staged_files(root) if staged else _all_worktree_files(root)
    files = [f for f in files if f.exists()]

    report = AuditReport(files_scanned=len(files))

    check_forbidden_tracked(root, files, report)
    check_file_loc(root, files, report)
    check_duplicate_signal_types(root, files, report)
    check_session_drift(root, files, report)
    check_logger_canonical_drift(root, files, report)
    check_indicator_signal_only(root, files, report)
    check_env_parse_drift(root, files, report)
    # Baseline checks always run against the full apps/ tree even on
    # ``--staged``: if you're adding a single new bare-except, we still want
    # to catch you crossing the baseline.
    check_bare_except_baseline(root, report)
    # Dockerfile interpreter parity always runs full-tree: a Dependabot bump
    # touches only the Dockerfile, which may not be in the staged Python set.
    check_docker_python_base(root, report)
    check_runtime_dependency_contract(root, report)
    check_first_party_dependency_contract(root, report)
    check_broker_capability_catalogue(root, report)

    return report


def _print_report(report: AuditReport, *, strict: bool) -> int:
    """Render to console; return process exit code."""
    if not report.findings:
        console.print(f"[green]✓ vmdev audit passed[/green] ({report.files_scanned} files scanned)")
        return 0

    table = Table(
        title=f"vmdev audit findings ({report.files_scanned} files scanned)",
        show_lines=False,
    )
    table.add_column("Severity", style="bold")
    table.add_column("Rule")
    table.add_column("Path", overflow="fold")
    table.add_column("Message", overflow="fold")
    for f in report.findings:
        sev_style = "red" if f.severity == "error" else "yellow"
        table.add_row(
            f"[{sev_style}]{f.severity}[/{sev_style}]",
            f.rule,
            f.path,
            f.message,
        )
    console.print(table)

    n_err = len(report.errors)
    n_warn = len(report.warnings)
    summary = f"[red]{n_err} error(s)[/red], [yellow]{n_warn} warning(s)[/yellow]"
    console.print(summary)

    if n_err or (strict and n_warn):
        return 1
    return 0


@click.command()
@click.option(
    "--staged",
    is_flag=True,
    help="Only scan files in the git index (used by the pre-commit hook).",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Fail on warnings as well as errors.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit a machine-readable JSON report instead of the table.",
)
def audit(staged: bool, strict: bool, json_output: bool) -> None:
    """Run repository-quality audit checks.

    Mechanical guardrails that prevent the design-principle regressions the
    May 2026 codebase audit cleaned up: god classes, duplicate canonical
    types, SQLAlchemy session drift, tracked build artefacts, broken
    indicator-signal-only contract, bare-except creep, and runtime dependency
    closure drift.

    See ``CLAUDE.md`` § "Architecture audit gates" for the full rule list.
    """
    report = run_audit(staged=staged)
    if json_output:
        payload = {
            "files_scanned": report.files_scanned,
            "errors": [f.__dict__ for f in report.errors],
            "warnings": [f.__dict__ for f in report.warnings],
        }
        click.echo(json.dumps(payload, indent=2, default=str))
        if report.errors or (strict and report.warnings):
            sys.exit(1)
        return

    sys.exit(_print_report(report, strict=strict))
