"""Application lifecycle management base class."""

import os
import signal
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from lib_common.app.config import ConfigManager
from lib_common.app.health import HealthCheckResult, HealthCheckServer
from lib_common.app.secrets import SecretsManager
from lib_common.env_utils import parse_int_env
from lib_common.logging import get_logger

logger = get_logger(__name__)


class ApplicationManager(ABC):
    """
    Abstract base class for application lifecycle management.

    Provides common functionality for:
    - Configuration loading
    - Secrets management
    - Health check server
    - Signal handling
    - Graceful shutdown

    Subclasses must implement:
    - initialize(): Setup application-specific components
    - run(): Main application logic
    - get_health_status(): Return current health status
    """

    def __init__(
        self,
        service_name: str,
        base_path: Path | None = None,
        environment: str | None = None,
    ) -> None:
        """
        Initialize application manager.

        Args:
            service_name: Name of the service
            base_path: Base path for configuration files
            environment: Environment name (dev, staging, production)
        """
        self.service_name = service_name
        self.base_path = base_path or Path.cwd()
        self.environment: str = environment or os.environ.get("ENV", "production")

        # Components
        self.config_manager = ConfigManager(base_path=self.base_path)
        self.secrets_manager: SecretsManager | None = None
        self.health_server: HealthCheckServer | None = None

        # State
        self.deployment_config: dict[str, Any] = {}
        self.secrets: dict[str, str] = {}
        self.shutdown_requested = False
        self._initialized = False

    def setup(self) -> None:
        """
        Setup application components.

        Loads configuration and secrets, initializes health server,
        and calls initialize() hook for subclass-specific setup.
        """
        logger.info("Starting %s - Environment: %s", self.service_name, self.environment)

        # Load configuration
        try:
            self.deployment_config = self.config_manager.load_deployment_config(self.environment)
            logger.info("Loaded deployment config for %s", self.environment)
        except Exception:
            logger.exception("Failed to load configuration")
            raise

        # Initialize secrets manager
        self.secrets_manager = SecretsManager()

        # Load secrets
        try:
            secrets_config = self.deployment_config.get("secrets", {})
            source = secrets_config.get("source", "env_vars")
            secret_names = secrets_config.get("names")

            self.secrets = self.secrets_manager.load_secrets(
                source=source, secret_names=secret_names
            )
            logger.info("Loaded secrets successfully")
        except Exception:
            logger.exception("Failed to load secrets")
            # Continue with empty secrets - may be optional for some apps

        # Setup signal handlers
        self._setup_signal_handlers()

        # Initialize health check server
        health_port = parse_int_env(
            "HEALTH_CHECK_PORT",
            default=8080,
            min_value=1,
            max_value=65535,
            logger=logger,
        )
        self.health_server = HealthCheckServer(
            port=health_port,
            health_check_func=self.get_health_status,
            service_name=self.service_name,
        )

        # Call subclass initialization
        try:
            self.initialize()
            self._initialized = True
            logger.info("%s initialized successfully", self.service_name)
        except Exception:
            logger.exception("Failed to initialize %s", self.service_name)
            raise

    def start(self) -> None:
        """
        Start the application.

        Starts health check server and calls run() hook.
        """
        if not self._initialized:
            msg = "Application not initialized. Call setup() first."
            logger.error(msg)
            raise RuntimeError(msg)

        # Start health check server
        if self.health_server:
            try:
                self.health_server.start()
                logger.info("Health check server started")
            except Exception:
                logger.exception("Failed to start health server")
                # Continue without health checks

        # Run main application logic
        try:
            logger.info("Starting %s main loop", self.service_name)
            self.run()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
            self.shutdown()
        except Exception:
            logger.exception("Fatal error in %s", self.service_name)
            self.shutdown()
            sys.exit(1)

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        if self.shutdown_requested:
            logger.debug("Shutdown already in progress")
            return

        logger.info("Shutting down %s...", self.service_name)
        self.shutdown_requested = True

        # Stop health server first (stop accepting traffic)
        if self.health_server:
            try:
                self.health_server.stop()
            except Exception:
                logger.exception("Error stopping health server")

        # Call subclass cleanup
        try:
            self.cleanup()
        except Exception:
            logger.exception("Error during cleanup")

        logger.info("%s shutdown complete", self.service_name)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        logger.debug("Signal handlers configured")

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        """
        Handle shutdown signals.

        Args:
            signum: Signal number
            frame: Current stack frame
        """
        logger.info("Received signal %s, initiating graceful shutdown", signum)
        self.shutdown()
        sys.exit(0)

    @abstractmethod
    def initialize(self) -> None:
        """
        Initialize application-specific components.

        Called during setup() after configuration and secrets are loaded.
        Subclasses must implement this method.
        """
        ...

    @abstractmethod
    def run(self) -> None:
        """
        Run main application logic.

        Called after setup() completes successfully.
        Subclasses must implement this method.
        """
        ...

    @abstractmethod
    def get_health_status(self) -> HealthCheckResult:
        """
        Get current health status of the application.

        Returns:
            HealthCheckResult with status and details

        Subclasses must implement this method to provide health information.
        """
        ...

    def cleanup(self) -> None:
        """
        Cleanup application resources.

        Called during shutdown. Subclasses can override to add
        custom cleanup logic. Default implementation does nothing.
        """
        return

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value safely.

        Args:
            key: Configuration key (supports dot notation)
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self.config_manager.get(key, default)

    def get_secret(self, key: str, default: str = "") -> str:
        """
        Get secret value safely.

        Args:
            key: Secret key name
            default: Default value if key not found

        Returns:
            Secret value or default
        """
        if self.secrets_manager:
            return str(self.secrets_manager.get(key, default))
        return str(self.secrets.get(key, default))
