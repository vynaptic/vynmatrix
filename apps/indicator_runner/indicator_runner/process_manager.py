"""
Indicator Runner - Process Management

Manages multiple signal_worker indicator strategy processes with health monitoring and auto-restart.
"""

import contextlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy.exc import SQLAlchemyError

from lib_common.config_validation import (
    IndicatorRunnerConfig,
    load_indicator_runner_config,
)
from lib_common.logging import get_logger
from lib_common.paper_promotion import load_paper_promotion_scope
from lib_common.runner_utils import (
    RestartPolicy,
    StrategyFilter,
    build_config_validator,
    iter_strategy_dirs,
    load_strategy_config,
)
from lib_data.watermark import WatermarkError

from .panel_binding import (
    PanelRuntimeBinding,
    panel_outbox_ordering_key,
    scoped_panel_worker_id,
)
from .runtime_journal import StrategyOperationalStatus, StrategyOperationalStatusReader
from .runtime_params import StrategyRuntimeParams
from .signal_worker import (
    HISTORICAL_REBUILD_EXIT_CODE,
    WORKER_STOP_BUDGET_SECONDS,
    parse_strategy_symbols,
)

logger = get_logger(__name__)

# Every worker gets this long to stop before it is killed. It exceeds the
# worker's own stop budget so a graceful stop completes inside it, the whole
# fleet shares one deadline instead of spending it per worker in turn, and it
# stays inside the platform supervisor's smallest stage share (55 s split over
# the three stop orders of the combined group, about 18 s), so the runner is
# not SIGKILLed while it is still waiting for its workers.
_STRATEGY_STOP_GRACE_SECONDS = WORKER_STOP_BUDGET_SECONDS + 3.0

_MAX_VALIDATION_ERRORS_SHOWN = 5
DEV_DISCOVERY_ENV = "INDICATOR_ALLOW_DEV_DISCOVERY"
_PAPER_READY_STATES = frozenset({"READY_FOR_PAPER_TRADING", "LIVE_CANDIDATE"})
_LIVE_READY_STATES = frozenset({"LIVE_CANDIDATE"})


@dataclass
class IndicatorProcess:
    """Represents a running indicator strategy process"""

    name: str
    path: Path
    config: dict
    run_mode: str = "paper"
    panel_binding: PanelRuntimeBinding | None = None
    process: subprocess.Popen | None = None
    start_time: datetime | None = None
    restart_count: int = 0
    last_restart: datetime | None = None
    status: str = "stopped"  # stopped, running, crashed, disabled

    @property
    def worker_id(self) -> str:
        """Return the durable worker partition shared with the child process."""
        base_worker_id = self.base_worker_id
        if self.panel_binding is None:
            return base_worker_id
        return scoped_panel_worker_id(base_worker_id, self.panel_binding)

    @property
    def base_worker_id(self) -> str:
        """Return the unscoped process identifier passed to the child."""
        return f"{self.name}:{self.run_mode}"

    @property
    def ordering_key(self) -> str:
        """Return the exact outbox partition owned by this process."""
        if self.panel_binding is None:
            return self.worker_id
        return panel_outbox_ordering_key(self.worker_id, self.panel_binding)


class IndicatorRunner:
    """Manages multiple signal_worker indicator strategy processes"""

    def __init__(
        self,
        category: str,
        deployment_config: dict[str, Any],
        secrets: dict[str, str],
        *,
        startup_config: IndicatorRunnerConfig | None = None,
        operational_reader: StrategyOperationalStatusReader | None = None,
    ) -> None:
        self.category = category
        self.deployment_config = deployment_config
        self.secrets = secrets
        self.strategies: list[IndicatorProcess] = []
        self.max_restarts = 5
        self.restart_cooldown = 60  # seconds
        self.shutdown_flag = False
        self.cpu_warn_threshold = 80  # percent
        self.memory_warn_threshold_mb = 500
        self._repo_root = Path(__file__).resolve().parents[3]
        self._startup_config = startup_config or load_indicator_runner_config(
            deployment_config=deployment_config,
            secrets=secrets,
            repo_root=self._repo_root,
        )
        # Deployment environment (dev/staging/production) gates every strategy's
        # explicit ``environments`` allowlist and the dev-only discovery escape hatch.
        self._environment = self._startup_config.environment
        self._operational_reader = operational_reader
        self._schema_path = self._startup_config.schema_path
        self._config_validator = build_config_validator(self._schema_path, "indicator", logger)

    @property
    def loading_mode(self) -> str:
        """Return the frozen strategy selector reflected by health output."""
        return self._startup_config.loading_mode

    def load_strategies(self) -> None:
        """
        Load strategies in the category with filtering support.

        Filtering modes (in priority order):
        1. STRATEGY_NAME: Single strategy mode (for debugging/testing)
        2. STRATEGY_LIST: Comma-separated list of strategies (for container bundles)
        3. INDICATOR_ALLOW_DEV_DISCOVERY=true: discover all enabled strategies,
           accepted only when ENVIRONMENT=dev

        With no explicit selector, the runner loads nothing. This makes a missing
        production/staging STRATEGY_LIST fail closed instead of activating every
        enabled strategy that happens to ship in the image.
        """
        strategies_dir = (Path.cwd() / "strategies" / self.category).resolve()

        if not strategies_dir.exists():
            logger.error("Strategies directory not found: %s", strategies_dir)
            return

        strategy_filter = self._resolve_strategy_filter(strategies_dir)
        if strategy_filter is None:
            return
        target_strategy = strategy_filter.target_strategy
        strategy_list = strategy_filter.strategy_list

        # Track which strategies from STRATEGY_LIST were found
        found_strategies: set[str] = set()
        skipped_disabled: list[str] = []
        skipped_no_config: list[str] = []
        skipped_invalid: list[str] = []
        skipped_env: list[str] = []
        skipped_readiness: list[str] = []
        skipped_not_selected: list[str] = []

        for strategy_dir in iter_strategy_dirs(strategies_dir):
            strategy_name = strategy_dir.name

            # Apply filtering - skip if not matching filter criteria
            if not strategy_filter.matches(strategy_name):
                # On disk but excluded by STRATEGY_LIST / single-strategy mode.
                # Tracked and logged so selection mistakes are visible. The
                # environment gate runs after this filter; every native or
                # catalogue strategy must first be selected explicitly.
                skipped_not_selected.append(strategy_name)
                continue

            # Load and validate strategy
            config, status = load_strategy_config(
                strategy_dir=strategy_dir,
                strategy_name=strategy_name,
                validator=self._config_validator,
                logger=logger,
                default_mode="paper",
                allow_exec_mode_env=True,
                max_validation_errors=_MAX_VALIDATION_ERRORS_SHOWN,
                current_environment=self._environment,
            )
            if status == "missing":
                skipped_no_config.append(strategy_name)
                continue
            if status == "disabled":
                skipped_disabled.append(strategy_name)
                continue
            if status == "env_excluded":
                # Strategy's config.environments excludes the current environment
                # (e.g. a dev-only e2e-verification strategy in the cloud). Skip it
                # quietly — this is the intended "do nothing in cloud" path.
                skipped_env.append(strategy_name)
                continue
            if status == "invalid":
                skipped_invalid.append(strategy_name)
                continue
            if config is None:
                continue  # Should not happen, but safety check

            run_mode = str((config.get("runtime") or {}).get("mode", "paper")).strip().lower()
            if not self._is_ready_for_runtime(
                config,
                run_mode=run_mode,
                config_path=strategy_dir / "config.json",
            ):
                skipped_readiness.append(strategy_name)
                continue

            # Create strategy process object
            panel_binding = self._resolve_panel_binding(
                config=config,
                strategy_name=strategy_name,
                run_mode=run_mode,
            )
            strategy = IndicatorProcess(
                name=strategy_name,
                path=strategy_dir,
                config=config,
                run_mode=run_mode,
                panel_binding=panel_binding,
            )
            self.strategies.append(strategy)
            found_strategies.add(strategy_name)
            logger.info("Loaded strategy: %s", strategy_name)

        # In staging/production, every explicit member is a deployment
        # assertion.  Loading only the available subset would leave the
        # container healthy while silently omitting a requested strategy.
        if strategy_list:
            unavailable = strategy_list - found_strategies
            if self._environment not in {"staging", "production"}:
                unavailable -= set(skipped_disabled) | set(skipped_env)
            if unavailable:
                message = (
                    f"STRATEGY_LIST contains {len(unavailable)} unavailable strategies in "
                    f"{strategies_dir}: {sorted(unavailable)}"
                )
                logger.error(message)
                raise RuntimeError(message)

        # A named deployment target is an assertion, not a best-effort filter.
        # Exit non-zero when it cannot be loaded so orchestration never keeps an
        # economically inert, restart-looping container alive.
        if target_strategy and len(self.strategies) == 0:
            message = f"Target strategy '{target_strategy}' not found, enabled, or allowed"
            logger.error(message)
            raise RuntimeError(message)

        # Summary logging
        logger.info(
            "Strategy loading complete: %d loaded, %d disabled, %d env-excluded "
            "%d readiness-blocked, (env=%s), %d missing config, %d invalid config, "
            "%d on disk but not in STRATEGY_LIST",
            len(self.strategies),
            len(skipped_disabled),
            len(skipped_env),
            len(skipped_readiness),
            self._environment,
            len(skipped_no_config),
            len(skipped_invalid),
            len(skipped_not_selected),
        )
        if strategy_list and skipped_not_selected:
            logger.info(
                "Strategies present on disk but excluded by STRATEGY_LIST "
                "(add to STRATEGY_LIST only after its activation gates pass, e.g. "
                "an opt-in operational probe): %s",
                sorted(skipped_not_selected),
            )

    def _resolve_panel_binding(
        self,
        *,
        config: dict[str, Any],
        strategy_name: str,
        run_mode: str,
    ) -> PanelRuntimeBinding | None:
        parameters = dict(config.get("parameters") or {})
        if (
            str(parameters.get("universe_contract") or "").strip()
            != "point_in_time_sp500_membership"
        ):
            return None
        configured = self._startup_config.panel_runtime
        if configured is None:
            message = (
                f"Panel strategy {strategy_name!r} has no explicit "
                "owner/scope/activation runtime binding"
            )
            raise RuntimeError(message)
        if run_mode != "paper":
            message = f"Owner-scoped panel strategy {strategy_name!r} is paper-only"
            raise RuntimeError(message)
        return PanelRuntimeBinding(
            environment=configured.environment,
            data_use_scope=configured.data_use_scope,
            entitlement_owner_user_id=configured.entitlement_owner_user_id,
            activation_cutoff=configured.activation_cutoff,
        )

    def _is_ready_for_runtime(
        self,
        config: dict[str, Any],
        *,
        run_mode: str,
        config_path: Path,
    ) -> bool:
        """Enforce strategy certification at the production process boundary.

        Development remains the place for static review, backtests, walk-forward
        validation, and the real-history pipeline E2E. Staging/production workers
        may start only strategies explicitly certified for the requested capital
        mode. A selected but insufficiently certified strategy is treated as
        unavailable, causing an explicit production ``STRATEGY_LIST`` to fail.
        """
        if self._environment == "dev":
            return True

        readiness = str((config.get("metadata") or {}).get("readiness", "")).strip().upper()
        if run_mode == "live":
            allowed = _LIVE_READY_STATES
        elif run_mode == "paper":
            allowed = _PAPER_READY_STATES
        else:
            logger.error(
                "Strategy %s declares unsupported runtime mode %r",
                config.get("strategy_id"),
                run_mode,
            )
            return False

        if readiness not in allowed:
            logger.error(
                "Strategy %s readiness %r is not certified for %s runtime in %s",
                config.get("strategy_id"),
                readiness or "<missing>",
                run_mode,
                self._environment,
            )
            return False

        # Paper certification and live authorization are intentionally separate
        # gates. A paper evidence marker can never authorize live mode.
        if run_mode == "paper":
            return self._has_exact_paper_promotion(config, config_path=config_path)
        return True

    def _has_exact_paper_promotion(
        self,
        config: dict[str, Any],
        *,
        config_path: Path,
    ) -> bool:
        """Validate exact, evidence-backed paper authority for this config."""
        manifest_path = self._startup_config.paper_promotion_manifest
        image_tag = (self._startup_config.deploy_image_tag or "").strip()
        strategy_id = str(config.get("strategy_id") or "").strip()
        if manifest_path is None or not image_tag:
            logger.error(
                "Strategy %s paper promotion requires "
                "INDICATOR_PAPER_PROMOTION_MANIFEST and VM_DEPLOY_IMAGE_TAG",
                strategy_id,
            )
            return False
        scope, validation_errors = load_paper_promotion_scope(
            manifest_path=manifest_path,
            deploy_image_tag=image_tag,
            config_path=config_path,
        )
        if scope is None:
            logger.error(
                "Strategy %s paper promotion rejected: %s",
                strategy_id,
                "; ".join(validation_errors),
            )
            return False
        if scope.is_synchronized_portfolio:
            panel_runtime = self._startup_config.panel_runtime
            if (
                panel_runtime is None
                or panel_runtime.data_use_scope != scope.data_use_scope
                or panel_runtime.entitlement_owner_user_id != scope.user_id
            ):
                logger.error(
                    "Strategy %s synchronized promotion does not match its exact "
                    "paper-forward panel owner/scope binding",
                    strategy_id,
                )
                return False
        logger.info(
            "Validated exact paper promotion for strategy=%s version=%s "
            "model_scope=%s instrument_set_sha256=%s account_id=%s",
            strategy_id,
            scope.strategy_version,
            scope.model_scope,
            scope.instrument_set_sha256,
            scope.broker_account_id,
        )
        return True

    def _resolve_strategy_filter(self, strategies_dir: Path) -> StrategyFilter | None:
        """Resolve an explicit selector or the guarded dev-only discovery mode."""
        strategy_filter = StrategyFilter(
            target_strategy=self._startup_config.target_strategy,
            strategy_list=set(self._startup_config.strategy_names),
        )
        if strategy_filter.target_strategy:
            logger.info("Single strategy mode: Loading only '%s'", strategy_filter.target_strategy)
            return strategy_filter
        if strategy_filter.strategy_list:
            logger.info(
                "Container bundle mode: Loading %d strategies from STRATEGY_LIST",
                len(strategy_filter.strategy_list),
            )
            logger.debug("STRATEGY_LIST: %s", sorted(strategy_filter.strategy_list))
            return strategy_filter

        if not self._startup_config.allow_dev_discovery:
            logger.error(
                "No strategy selector configured; refusing unfiltered discovery. "
                "Set STRATEGY_NAME or STRATEGY_LIST. For explicit local discovery "
                "only, set ENVIRONMENT=dev and %s=true.",
                DEV_DISCOVERY_ENV,
            )
            return None
        if self._environment != "dev":
            logger.error(
                "%s=true is restricted to ENVIRONMENT=dev; refusing unfiltered "
                "strategy discovery in environment=%s",
                DEV_DISCOVERY_ENV,
                self._environment,
            )
            return None

        logger.warning(
            "Explicit development discovery enabled via %s; loading enabled strategies from %s",
            DEV_DISCOVERY_ENV,
            strategies_dir,
        )
        return strategy_filter

    def run_all(self) -> None:
        """Run all loaded strategies"""
        logger.info("Starting %s strategies...", len(self.strategies))

        start_delay = self._startup_config.start_delay_seconds

        for strategy in self.strategies:
            self._start_strategy(strategy)
            if start_delay > 0:
                logger.info("Delaying next strategy start by %.1f seconds", start_delay)
                time.sleep(start_delay)

        # Monitor processes
        self._monitor_processes()

    def _start_strategy(self, strategy: IndicatorProcess) -> None:
        """Start a single strategy process"""
        try:
            runner_kind = self._resolve_runner_kind(strategy)
            cmd = self._build_signal_worker_command(strategy)
            process_cwd = self._repo_root

            logger.info("Starting strategy: %s", strategy.name)
            logger.info("Runner kind for %s: %s", strategy.name, runner_kind)
            logger.debug("Command: %s", " ".join(cmd))

            # Start process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(process_cwd),
                env=self._get_process_env(strategy, runner_kind),
            )

            strategy.process = process
            strategy.start_time = datetime.now(tz=UTC)
            strategy.status = "running"

            # Stream the worker's stdout/stderr to the runner's stdout in real
            # time (prefixed with the strategy name) so a running worker's
            # bootstrap/warmup/emit logs and any internal errors are visible in
            # ``docker logs`` — not buffered in the pipe and only surfaced on exit.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    threading.Thread(
                        target=self._stream_worker_output,
                        args=(stream, strategy.name),
                        daemon=True,
                    ).start()

            logger.info("Strategy %s started with PID %s", strategy.name, process.pid)

        except Exception:
            logger.exception("Failed to start strategy %s", strategy.name)
            strategy.status = "crashed"

    @staticmethod
    def _stream_worker_output(stream: Any, prefix: str) -> None:
        """Forward a worker subprocess stream to the runner's stdout, line by line.

        Runs on a daemon thread per stream so output appears live in
        ``docker logs`` with a ``[strategy]`` prefix instead of being trapped in
        the pipe until the process exits.
        """
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="ignore").rstrip()
                if line:
                    print(f"[{prefix}] {line}", flush=True)
        except (ValueError, OSError):
            # Stream closed (e.g. during shutdown) — nothing left to forward.
            pass
        finally:
            with contextlib.suppress(ValueError, OSError):
                stream.close()

    def _build_signal_worker_command(self, strategy: IndicatorProcess) -> list[str]:
        """Build command to execute the DB-fed signal worker runtime."""
        return [
            sys.executable,
            "-m",
            "indicator_runner.signal_worker",
            "--strategy-path",
            str(strategy.path),
            "--worker-id",
            strategy.base_worker_id,
        ]

    def _resolve_runner_kind(self, strategy: IndicatorProcess) -> str:
        """The indicator runtime is signal_worker only (LEAN was retired)."""
        configured = str(strategy.config.get("runner_kind", "signal_worker")).strip().lower()
        if configured != "signal_worker":
            logger.warning(
                "runner_kind=%s for %s is unsupported; using signal_worker",
                configured,
                strategy.name,
            )
        return "signal_worker"

    def _get_process_env(self, strategy: IndicatorProcess, runner_kind: str) -> dict[str, str]:
        """Get environment variables for strategy process"""
        env = os.environ.copy()

        existing_path = env.get("PYTHONPATH", "")
        worker_paths = [
            str(self._repo_root / "apps" / "indicator_runner"),
            str(self._repo_root / "apps" / "market_data_ingestor"),
            str(self._repo_root / "apps" / "scoring_engine"),
            str(self._repo_root / "apps" / "execution_engine"),
            str(self._repo_root / "apps" / "feedback_loop_engine"),
            str(self._repo_root / "libs" / "python" / "lib_common"),
            str(self._repo_root / "libs" / "python" / "lib_strategy"),
            str(self._repo_root / "libs" / "python" / "lib_application"),
            str(self._repo_root / "libs" / "python" / "lib_infrastructure"),
            str(self._repo_root / "libs" / "python" / "lib_data"),
            str(self._repo_root / "libs" / "python" / "lib_indicators"),
        ]
        joined = os.pathsep.join(worker_paths)
        env["PYTHONPATH"] = f"{joined}{os.pathsep}{existing_path}" if existing_path else joined

        # Add strategy-specific env vars
        env["STRATEGY_ID"] = strategy.config.get("strategy_id", strategy.name)
        env["STRATEGY_NAME"] = strategy.name
        env["RUN_MODE"] = strategy.run_mode
        env["RUNNER_KIND"] = runner_kind
        env["SIGNAL_API_URL"] = self._startup_config.signal_api_url
        # Shared inter-service API key (X-API-Key) the emitter sends to the
        # scoring engine. Canonical env is API_KEY (matches every other service
        # and the server's verify_api_key): prefer an already-injected API_KEY
        # (manifest secret), then the loaded `api_key` secret.
        env["API_KEY"] = self._startup_config.api_key

        return env

    def _monitor_processes(self) -> None:
        """Monitor all strategy processes and restart if crashed"""
        logger.info("Starting process monitoring...")
        live_mode = bool(self.deployment_config.get("live_mode", False))

        while not self.shutdown_flag:
            for strategy in self.strategies:
                if strategy.status == "disabled":
                    continue

                # Check if process is still running
                if strategy.process and strategy.process.poll() is not None:
                    # Process has terminated
                    exit_code = strategy.process.returncode
                    strategy_live_mode = live_mode or strategy.run_mode == "live"
                    if exit_code == HISTORICAL_REBUILD_EXIT_CODE:
                        # A durable market-data generation requested a clean
                        # strategy/consolidator process. This is an expected
                        # state transition, not one of the ordinary crash
                        # attempts that can disable a healthy strategy.
                        strategy.status = "rebuilding"
                        strategy.last_restart = datetime.now(tz=UTC)
                        logger.warning(
                            "Restarting strategy %s for historical price rebuild",
                            strategy.name,
                        )
                        self._start_strategy(strategy)
                        continue
                    if exit_code == 0 and not strategy_live_mode:
                        strategy.status = "completed"
                        logger.info("Strategy %s completed successfully", strategy.name)
                        continue

                    logger.error(
                        "Strategy %s terminated with exit code %s", strategy.name, exit_code
                    )
                    # The worker's stdout/stderr (including any error output) was
                    # already streamed live to the runner's stdout by the
                    # per-strategy reader threads, so it is visible in the logs.
                    strategy.status = "crashed"

                    # Attempt restart if within limits
                    if self._should_restart(strategy):
                        attempt_count = strategy.restart_count + 1
                        logger.info(
                            "Restarting strategy %s (attempt %s/%s)",
                            strategy.name,
                            attempt_count,
                            self.max_restarts,
                        )
                        strategy.restart_count = attempt_count
                        strategy.last_restart = datetime.now(tz=UTC)
                        self._start_strategy(strategy)
                    elif strategy.restart_count >= self.max_restarts:
                        logger.error("Strategy %s exceeded max restarts, disabling", strategy.name)
                        strategy.status = "disabled"

                # Check process health (CPU, memory)
                elif strategy.process:
                    try:
                        proc = psutil.Process(strategy.process.pid)
                        cpu_percent = proc.cpu_percent(interval=1)
                        memory_mb = proc.memory_info().rss / 1024 / 1024

                        # Log if resource usage is high
                        if cpu_percent > self.cpu_warn_threshold:
                            logger.warning(
                                "Strategy %s high CPU usage: %s%%",
                                strategy.name,
                                cpu_percent,
                            )
                        if memory_mb > self.memory_warn_threshold_mb:
                            logger.warning(
                                "Strategy %s high memory usage: %.1fMB",
                                strategy.name,
                                memory_mb,
                            )

                    except psutil.NoSuchProcess:
                        logger.warning("Process for strategy %s no longer exists", strategy.name)
                        strategy.status = "crashed"

            # Sleep before next check
            time.sleep(5)

    def _should_restart(self, strategy: IndicatorProcess) -> bool:
        """Determine if strategy should be restarted"""
        policy = RestartPolicy(
            max_restarts=self.max_restarts,
            cooldown_seconds=self.restart_cooldown,
        )
        return bool(policy.can_restart(strategy.restart_count, strategy.last_restart))

    def get_status(self) -> dict[str, Any]:
        """Get status of all strategies"""
        operational = self._get_operational_status()
        return {
            "category": self.category,
            "total_strategies": len(self.strategies),
            "running": sum(1 for s in self.strategies if s.status == "running"),
            "crashed": sum(1 for s in self.strategies if s.status == "crashed"),
            "disabled": sum(1 for s in self.strategies if s.status == "disabled"),
            "completed": sum(1 for s in self.strategies if s.status == "completed"),
            "operational_ready": bool(operational) and all(status.ready for status in operational),
            "operational_slo": {
                "max_signal_backlog_age_seconds": (
                    self._startup_config.max_signal_backlog_age_seconds
                ),
                "max_strategy_lag_seconds": self._startup_config.max_strategy_lag_seconds,
            },
            "operational": [status.to_dict() for status in operational],
            "strategies": [
                {
                    "name": s.name,
                    "worker_id": s.worker_id,
                    "status": s.status,
                    "run_mode": s.run_mode,
                    "pid": s.process.pid if s.process else None,
                    "uptime": (
                        (datetime.now(tz=UTC) - s.start_time).total_seconds() if s.start_time else 0
                    ),
                    "restart_count": s.restart_count,
                }
                for s in self.strategies
            ],
        }

    def _get_operational_status(self) -> list[StrategyOperationalStatus]:
        """Read durable economic progress for every selected child worker."""
        statuses: list[StrategyOperationalStatus] = []
        for strategy in self.strategies:
            strategy_id = str(strategy.config.get("strategy_id") or "").strip()
            if self._operational_reader is None:
                statuses.append(
                    StrategyOperationalStatus.unavailable(
                        worker_id=strategy.worker_id,
                        strategy_id=strategy_id or strategy.name,
                        error="operational_status_reader_not_configured",
                    )
                )
                continue
            try:
                parameters = dict(strategy.config.get("parameters") or {})
                market_data = dict(strategy.config.get("market_data") or {})
                runtime_params = StrategyRuntimeParams.from_config(parameters, market_data)
                statuses.append(
                    self._operational_reader.read(
                        worker_id=strategy.worker_id,
                        strategy_id=strategy_id,
                        symbols=parse_strategy_symbols(runtime_params.universe),
                        source=runtime_params.source,
                        timeframe=runtime_params.timeframe,
                        asset_class=runtime_params.asset_class,
                        max_outbox_age_seconds=(
                            self._startup_config.max_signal_backlog_age_seconds
                        ),
                        max_strategy_lag_seconds=(self._startup_config.max_strategy_lag_seconds),
                        ordering_key=strategy.ordering_key,
                        panel_capable=(
                            str(parameters.get("universe_contract") or "").strip()
                            == "point_in_time_sp500_membership"
                        ),
                        strategy_version=str(strategy.config.get("strategy_version") or "").strip(),
                        max_panel_age_seconds=runtime_params.max_panel_age_seconds,
                    )
                )
            except (
                ArithmeticError,
                SQLAlchemyError,
                TypeError,
                ValueError,
                WatermarkError,
            ) as exc:
                logger.exception(
                    "Indicator operational status query failed",
                    worker_id=strategy.worker_id,
                    strategy_id=strategy_id or strategy.name,
                    error_type=type(exc).__name__,
                )
                statuses.append(
                    StrategyOperationalStatus.unavailable(
                        worker_id=strategy.worker_id,
                        strategy_id=strategy_id or strategy.name,
                        error=f"operational_status_unavailable:{type(exc).__name__}",
                    )
                )
        return statuses

    def shutdown(self) -> None:
        """Gracefully shutdown all strategy processes"""
        logger.info("Shutting down all strategies...")
        self.shutdown_flag = True

        running = [
            strategy
            for strategy in self.strategies
            if strategy.process is not None and strategy.process.poll() is None
        ]
        for strategy in running:
            process = strategy.process
            if process is None:
                continue
            logger.info("Terminating strategy %s (PID %s)", strategy.name, process.pid)
            process.terminate()
        # The workers stop concurrently; wait for all of them against one
        # deadline rather than granting the full grace to each in turn.
        deadline = time.monotonic() + _STRATEGY_STOP_GRACE_SECONDS
        for strategy in running:
            process = strategy.process
            if process is None:
                continue
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logger.warning("Strategy %s did not terminate gracefully, killing", strategy.name)
                process.kill()

        logger.info("All strategies shutdown complete")
