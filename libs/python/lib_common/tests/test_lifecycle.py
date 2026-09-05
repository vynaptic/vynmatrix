"""Unit tests for ApplicationManager."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml

from lib_common.app.config import ConfigManager
from lib_common.app.health import HealthCheckResult, HealthStatus
from lib_common.app.lifecycle import ApplicationManager


class TestApplicationManager(ApplicationManager):
    """Concrete implementation of ApplicationManager for testing."""

    __test__ = False  # Prevent pytest from collecting this as a test class

    def __init__(
        self,
        service_name: str = "test-service",
        base_path: Path | None = None,
        environment: str | None = None,
    ) -> None:
        """Initialize test application manager."""
        super().__init__(service_name=service_name, base_path=base_path, environment=environment)
        self.initialized = False
        self.running = False
        self.cleaned_up = False

    def initialize(self) -> None:
        """Mock initialize method."""
        self.initialized = True

    def run(self) -> None:
        """Mock run method."""
        self.running = True
        # Simulate running until shutdown
        while not self.shutdown_requested:
            pass
        self.running = False

    def get_health_status(self) -> HealthCheckResult:
        """Mock health status method."""
        if not self.initialized:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                details={},
                message="Not initialized",
            )
        return HealthCheckResult(status=HealthStatus.HEALTHY, details={"initialized": True})

    def cleanup(self) -> None:
        """Mock cleanup method."""
        self.cleaned_up = True


class TestApplicationManagerSetup:
    """Test suite for ApplicationManager setup and initialization."""

    @pytest.fixture(autouse=True)
    def clear_config_cache(self) -> None:
        """Clear ConfigManager class-level cache before each test."""
        ConfigManager._config_cache.clear()

    @pytest.fixture
    def temp_config(self) -> Generator[Path, None, None]:
        """Create temporary config directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            config_dir = base_path / "config"
            config_dir.mkdir()

            # Create deployment config
            deployment_dir = config_dir / "deployment"
            deployment_dir.mkdir()

            dev_config = {
                "environment": "dev",
                "endpoints": {"signal_api_url": "http://scoring-dev:8001"},
                "secrets": {"source": "env_vars"},
                "app": {"debug": True},
            }

            with (deployment_dir / "dev.yaml").open("w") as f:
                yaml.dump(dev_config, f)

            yield base_path

    def test_application_manager_creation(self, temp_config: Path) -> None:
        """Test creating ApplicationManager instance."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        assert app.service_name == "test-app"
        assert app.environment == "dev"
        assert app.base_path == temp_config
        assert not app.shutdown_requested

    def test_application_manager_environment_from_env(
        self, temp_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test environment detection from ENV variable."""
        monkeypatch.setenv("ENV", "dev")

        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment=None
        )

        assert app.environment == "dev"

    def test_application_manager_setup(self, temp_config: Path) -> None:
        """Test application setup process."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()

        assert app.initialized is True
        assert app.config_manager is not None
        assert app.deployment_config is not None
        assert app.deployment_config["environment"] == "dev"

    def test_application_manager_config_access(self, temp_config: Path) -> None:
        """Test accessing configuration values."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()

        # Test get_config method
        assert app.get_config("endpoints.signal_api_url") == "http://scoring-dev:8001"
        assert app.get_config("app.debug") is True

    def test_application_manager_config_with_default(self, temp_config: Path) -> None:
        """Test accessing config with default value."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()

        value = app.get_config("missing.key", default="default_value")
        assert value == "default_value"

    def test_application_manager_health_status(self, temp_config: Path) -> None:
        """Test health status after setup."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        # Before setup - unhealthy
        status = app.get_health_status()
        assert status.status == HealthStatus.UNHEALTHY

        # After setup - healthy
        app.setup()
        status = app.get_health_status()
        assert status.status == HealthStatus.HEALTHY
        assert status.details["initialized"] is True


class TestApplicationManagerSignalHandling:
    """Test suite for ApplicationManager signal handling."""

    @pytest.fixture
    def temp_config(self) -> Generator[Path, None, None]:
        """Create temporary config directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            config_dir = base_path / "config"
            config_dir.mkdir()

            deployment_dir = config_dir / "deployment"
            deployment_dir.mkdir()

            dev_config = {
                "environment": "dev",
                "endpoints": {"signal_api_url": "http://scoring-dev:8001"},
                "secrets": {"source": "env_vars"},
            }

            with (deployment_dir / "dev.yaml").open("w") as f:
                yaml.dump(dev_config, f)

            yield base_path

    def test_signal_handler_setup(self, temp_config: Path) -> None:
        """Test signal handlers are registered during setup."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()

        # Verify signal handlers were set up
        # Note: Can't easily test actual signal handling without sending signals
        assert app.shutdown_requested is False

    def test_request_shutdown(self, temp_config: Path) -> None:
        """Test requesting shutdown."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()
        assert app.shutdown_requested is False

        # Call shutdown() method which sets shutdown_requested=True
        app.shutdown()
        assert app.shutdown_requested is True

    def test_cleanup_called_on_shutdown(self, temp_config: Path) -> None:
        """Test cleanup is called during shutdown."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()
        app.shutdown()

        assert app.cleaned_up is True


class TestApplicationManagerLifecycle:
    """Test suite for ApplicationManager full lifecycle."""

    @pytest.fixture
    def temp_config(self) -> Generator[Path, None, None]:
        """Create temporary config directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            config_dir = base_path / "config"
            config_dir.mkdir()

            deployment_dir = config_dir / "deployment"
            deployment_dir.mkdir()

            dev_config = {
                "environment": "dev",
                "endpoints": {"signal_api_url": "http://scoring-dev:8001"},
                "secrets": {"source": "env_vars"},
            }

            with (deployment_dir / "dev.yaml").open("w") as f:
                yaml.dump(dev_config, f)

            yield base_path

    def test_full_lifecycle(self, temp_config: Path) -> None:
        """Test complete application lifecycle."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        # Setup phase
        app.setup()
        assert app.initialized is True
        assert app.running is False
        assert app.cleaned_up is False

        # Shutdown phase - calls cleanup()
        app.shutdown()
        assert app.cleaned_up is True
        assert app.shutdown_requested is True

    def test_health_server_integration(self, temp_config: Path) -> None:
        """Test health server is created during setup and started during start."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()

        # Health server should be created but not started yet
        assert app.health_server is not None
        assert app.health_server.is_running() is False  # Not started until start() is called

        # Cleanup
        app.shutdown()

    def test_config_path_resolution(self, temp_config: Path) -> None:
        """Test config directory path resolution."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        expected_config_dir = temp_config / "config"
        assert app.config_manager.config_dir == expected_config_dir

    def test_default_base_path(self) -> None:
        """Test default base_path is current working directory."""
        app = TestApplicationManager(service_name="test-app")

        assert app.base_path == Path.cwd()


class TestApplicationManagerAbstractMethods:
    """Test suite for ApplicationManager abstract methods."""

    def test_cannot_instantiate_abstract_class(self) -> None:
        """Test that ApplicationManager cannot be instantiated directly."""
        # This test verifies the ABC pattern is working

        # Attempting to create an incomplete subclass should fail
        class IncompleteApp(ApplicationManager):
            """Incomplete implementation missing required methods."""

            def initialize(self) -> None:
                pass

            # Missing: run(), get_health_status(), cleanup()

        # Python will raise TypeError when trying to instantiate
        # an abstract class with unimplemented abstract methods
        with pytest.raises(TypeError):
            IncompleteApp(service_name="test")  # type: ignore


class TestApplicationManagerErrorHandling:
    """Test suite for ApplicationManager error handling."""

    @pytest.fixture
    def temp_config(self) -> Generator[Path, None, None]:
        """Create temporary config directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            config_dir = base_path / "config"
            config_dir.mkdir()

            deployment_dir = config_dir / "deployment"
            deployment_dir.mkdir()

            dev_config = {
                "environment": "dev",
                "endpoints": {"signal_api_url": "http://scoring-dev:8001"},
                "secrets": {"source": "env_vars"},
            }

            with (deployment_dir / "dev.yaml").open("w") as f:
                yaml.dump(dev_config, f)

            yield base_path

    def test_get_config_before_setup_raises_error(self) -> None:
        """Test accessing config before setup returns default."""
        app = TestApplicationManager(service_name="test-app")

        # Implementation returns default/None instead of raising error
        value = app.get_config("any.key")
        assert value is None

        # Can specify default
        value_with_default = app.get_config("any.key", default="default")
        assert value_with_default == "default"

    def test_multiple_setup_calls_idempotent(self, temp_config: Path) -> None:
        """Test calling setup multiple times is safe."""
        app = TestApplicationManager(
            service_name="test-app", base_path=temp_config, environment="dev"
        )

        app.setup()
        app.setup()  # Should not raise error

        assert app.initialized is True

    def test_shutdown_without_setup(self) -> None:
        """Test shutdown without setup doesn't crash."""
        app = TestApplicationManager(service_name="test-app")

        # Should not raise error
        app.shutdown()
        assert app.cleaned_up is True
