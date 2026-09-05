"""
Strategy Runner Application - Multi-Strategy Orchestrator

Runs multiple DB-fed PureSignalStrategy cores as separate processes with health monitoring.

Supports three strategy loading modes:
1. STRATEGY_NAME: Single strategy for debugging/testing
2. STRATEGY_LIST: Comma-separated list for container bundles (from containers.yaml)
3. INDICATOR_ALLOW_DEV_DISCOVERY=true: Explicit unfiltered discovery in dev only

Without one of these selectors, the runner fails closed and loads no strategies.
"""

from pathlib import Path

from sqlalchemy.engine import Engine

from indicator_runner.process_manager import IndicatorRunner
from indicator_runner.runtime_journal import StrategyOperationalStatusReader
from lib_application.db.session import (
    create_engine_for_env,
    dispose_engine,
    get_session_factory,
)
from lib_common.app import ApplicationManager, HealthCheckResult, HealthStatus
from lib_common.config_validation import (
    load_indicator_log_level,
    load_indicator_runner_config,
)
from lib_common.env_utils import build_database_url
from lib_common.logging import get_logger, setup_logging

logger = get_logger(__name__)


class StrategyRunnerApp(ApplicationManager):
    """
    Indicator Runner Application - Orchestrates PureSignalStrategy indicator cores.

    This app runs multiple signal cores as separate processes, monitors their health,
    and automatically restarts crashed processes.
    """

    def __init__(self) -> None:
        """Initialize indicator runner application."""
        super().__init__(
            service_name="indicator-runner",
            base_path=Path.cwd(),
            environment=None,  # Will use ENV from environment
        )
        self.strategy_runner: IndicatorRunner | None = None
        self._db_engine: Engine | None = None
        self.category: str = "indicator"  # Default category

    def initialize(self) -> None:
        """
        Initialize strategy runner components.

        Called by ApplicationManager.setup() after config and secrets are loaded.
        """
        logger.info("Initializing Indicator Runner...")

        # Get strategy category from config (default: indicator)
        self.category = self.get_config("strategies.category", default="indicator")
        logger.info("Strategy category: %s", self.category)

        # Get process management settings
        max_restarts = self.get_config("strategies.max_restarts", default=5)
        restart_cooldown = self.get_config("strategies.restart_cooldown", default=60)

        # Initialize the indicator runner with process management
        startup_config = load_indicator_runner_config(
            deployment_config=self.deployment_config,
            secrets=self.secrets,
            repo_root=Path(__file__).resolve().parents[3],
            environment=self.environment,
        )
        self._db_engine = create_engine_for_env(
            db_url=build_database_url(env=self.environment),
        )
        session_factory = get_session_factory(engine=self._db_engine)
        self.strategy_runner = IndicatorRunner(
            category=self.category,
            deployment_config=self.deployment_config,
            secrets=self.secrets,
            startup_config=startup_config,
            operational_reader=StrategyOperationalStatusReader(session_factory),
        )

        # Apply configuration
        self.strategy_runner.max_restarts = max_restarts
        self.strategy_runner.restart_cooldown = restart_cooldown

        # Load strategies from directory
        self.strategy_runner.load_strategies()

        logger.info(
            "Loaded %s strategies from %s",
            len(self.strategy_runner.strategies),
            self.category,
        )

    def run(self) -> None:
        """
        Main execution loop - runs all strategies and monitors them.

        Runs continuously until shutdown is requested.
        """
        if not self.strategy_runner or len(self.strategy_runner.strategies) == 0:
            logger.error("No strategies loaded, exiting")
            self.shutdown()
            return

        logger.info("Starting all strategies...")

        try:
            # Run all strategies (this includes monitoring loop)
            self.strategy_runner.run_all()

        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
            self.shutdown()

        except Exception:
            logger.exception("Error in strategy runner main loop")
            self.shutdown()

    def get_health_status(self) -> HealthCheckResult:
        """
        Get current health status.

        Returns HEALTHY if at least one strategy is running,
        DEGRADED if some crashed, UNHEALTHY if none running.
        """
        if not self.strategy_runner:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                details={},
                message="Strategy runner not initialized",
            )

        status = self.strategy_runner.get_status()
        running_count = status.get("running", 0)
        completed_count = status.get("completed", 0)
        crashed_count = status.get("crashed", 0)
        total_count = status.get("total_strategies", 0)
        operational = status.get("operational", [])
        operational_ready = bool(status.get("operational_ready", False))
        operational_unready_count = sum(
            1 for item in operational if not bool(item.get("ready", False))
        )

        # Determine health status
        if running_count == 0 and completed_count == 0:
            health_status = HealthStatus.UNHEALTHY
            message = "No strategies running"
        elif not operational_ready:
            health_status = HealthStatus.UNHEALTHY
            message = (
                f"{operational_unready_count}/{total_count} strategies outside operational SLO"
            )
        elif crashed_count > 0:
            health_status = HealthStatus.DEGRADED
            message = f"{crashed_count}/{total_count} strategies crashed"
        else:
            health_status = HealthStatus.HEALTHY
            if running_count == 0 and completed_count > 0:
                message = "All strategies completed"
            else:
                message = f"{running_count} strategies running"

        return HealthCheckResult(
            status=health_status,
            details={
                "category": self.category,
                "loading_mode": self.strategy_runner.loading_mode,
                "total_strategies": total_count,
                "running": running_count,
                "completed": completed_count,
                "crashed": crashed_count,
                "disabled": status.get("disabled", 0),
                "operational_ready": operational_ready,
                "operational_slo": status.get("operational_slo", {}),
                "operational": operational,
                "environment": self.environment,
                "strategies": status.get("strategies", []),
            },
            message=message,
        )

    def cleanup(self) -> None:
        """
        Cleanup resources before shutdown.

        Gracefully terminates all strategy processes.
        """
        logger.info("Cleaning up Indicator Runner...")

        if self.strategy_runner:
            self.strategy_runner.shutdown()
        dispose_engine(self._db_engine)
        self._db_engine = None

        logger.info("Indicator Runner cleanup complete")


def main() -> None:
    """Entry point for the Indicator Runner application."""
    setup_logging(load_indicator_log_level())
    try:
        app = StrategyRunnerApp()
        app.setup()  # Loads config, secrets, starts health server
        app.start()  # Runs until shutdown requested
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception:
        logger.exception("Fatal error in Strategy Runner")
        raise


if __name__ == "__main__":
    main()
