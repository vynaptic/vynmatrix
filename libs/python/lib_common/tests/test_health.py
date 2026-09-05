"""Unit tests for HealthCheckServer."""

import json
import time
from http.client import HTTPConnection
from typing import Any

import pytest

import lib_common.app.health as health_module
from lib_common.app.health import (
    HealthCheckHandler,
    HealthCheckResult,
    HealthCheckServer,
    HealthStatus,
)

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_NOT_IMPLEMENTED = 501
DEFAULT_TEST_PORT = 8888


class MockHealthProvider:
    """Mock health check provider for testing."""

    def __init__(self, status: HealthStatus = HealthStatus.HEALTHY) -> None:
        self.status = status
        self.details: dict[str, Any] = {"test": "data"}
        self.message: str | None = None

    def get_health_status(self) -> HealthCheckResult:
        """Return mock health status."""
        return HealthCheckResult(status=self.status, details=self.details, message=self.message)


class TestHealthStatus:
    """Test suite for HealthStatus enum."""

    def test_health_status_values(self) -> None:
        """Test HealthStatus enum values."""
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"
        assert HealthStatus.DEGRADED.value == "degraded"

    def test_health_status_comparison(self) -> None:
        """Test HealthStatus enum comparison."""
        statuses = {
            HealthStatus.HEALTHY.value,
            HealthStatus.UNHEALTHY.value,
            HealthStatus.DEGRADED.value,
        }
        assert len(statuses) == len(HealthStatus)


class TestHealthCheckResult:
    """Test suite for HealthCheckResult."""

    def test_health_check_result_creation(self) -> None:
        """Test creating HealthCheckResult."""
        result = HealthCheckResult(
            status=HealthStatus.HEALTHY, details={"foo": "bar"}, message="All good"
        )

        assert result.status == HealthStatus.HEALTHY
        assert result.details == {"foo": "bar"}
        assert result.message == "All good"

    def test_health_check_result_to_dict_healthy(self) -> None:
        """Test HealthCheckResult dataclass fields."""
        result = HealthCheckResult(status=HealthStatus.HEALTHY, details={"component": "active"})

        # HealthCheckResult is a dataclass - access fields directly
        assert result.status == HealthStatus.HEALTHY
        assert result.status.value == "healthy"
        assert result.details == {"component": "active"}
        assert result.message is None

    def test_health_check_result_to_dict_unhealthy(self) -> None:
        """Test HealthCheckResult dataclass fields for unhealthy status."""
        result = HealthCheckResult(
            status=HealthStatus.UNHEALTHY,
            details={"error": "Database down"},
            message="Critical failure",
        )

        # HealthCheckResult is a dataclass - access fields directly
        assert result.status == HealthStatus.UNHEALTHY
        assert result.status.value == "unhealthy"
        assert result.message == "Critical failure"
        assert result.details == {"error": "Database down"}

    def test_health_check_result_to_dict_degraded(self) -> None:
        """Test HealthCheckResult dataclass fields for degraded status."""
        result = HealthCheckResult(
            status=HealthStatus.DEGRADED, details={}, message="Reduced capacity"
        )

        # HealthCheckResult is a dataclass - access fields directly
        assert result.status == HealthStatus.DEGRADED
        assert result.status.value == "degraded"
        assert result.message == "Reduced capacity"
        assert result.details == {}


class TestHealthCheckServer:
    """Test suite for HealthCheckServer."""

    @staticmethod
    def _get_port(health_server: HealthCheckServer) -> int:
        """Helper to assert server is running and return port."""
        assert health_server._server is not None
        return health_server._server.server_port

    @pytest.fixture
    def health_provider(self) -> MockHealthProvider:
        """Create mock health provider."""
        return MockHealthProvider(status=HealthStatus.HEALTHY)

    @pytest.fixture
    def health_server(self, health_provider: MockHealthProvider) -> HealthCheckServer:
        """Create health check server on random port."""
        # Use port 0 to let OS assign available port
        return HealthCheckServer(
            health_check_func=health_provider.get_health_status, host="127.0.0.1", port=0
        )

    def test_health_server_creation(self, health_provider: MockHealthProvider) -> None:
        """Test creating health check server."""
        server = HealthCheckServer(
            health_check_func=health_provider.get_health_status, port=DEFAULT_TEST_PORT
        )

        assert server.host == "0.0.0.0"
        assert server.port == DEFAULT_TEST_PORT
        # Health check func is set on the handler class, not stored as instance attribute
        assert HealthCheckHandler.health_check_func == health_provider.get_health_status

    def test_health_server_start_stop(self, health_server: HealthCheckServer) -> None:
        """Test starting and stopping health check server."""
        health_server.start()
        time.sleep(0.5)  # Give server time to start

        # Server should be running
        assert health_server._thread is not None
        assert health_server._thread.is_alive()

        health_server.stop()
        time.sleep(0.5)  # Give server time to stop

        # Thread should stop
        assert not health_server._thread.is_alive()

    def test_health_endpoint_healthy(self, health_server: HealthCheckServer) -> None:
        """Test /health endpoint returns healthy status."""
        health_server.start()
        time.sleep(0.5)

        # Get actual port assigned by OS
        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/health")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK
            assert data["status"] == "healthy"
            # /health endpoint does not include details (only /status does)
        finally:
            conn.close()
            health_server.stop()

    def test_healthz_endpoint_alias(self, health_server: HealthCheckServer) -> None:
        """Test /healthz endpoint (alias for /health)."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK
            assert data["status"] == "healthy"
        finally:
            conn.close()
            health_server.stop()

    def test_status_endpoint(self, health_server: HealthCheckServer) -> None:
        """Test /status endpoint returns detailed status."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/status")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK
            assert data["status"] == "healthy"
            assert "details" in data
        finally:
            conn.close()
            health_server.stop()

    def test_live_endpoint(self, health_server: HealthCheckServer) -> None:
        """Test /live endpoint (liveness probe)."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/live")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK
            assert data["status"] == "alive"  # Liveness returns "alive", not "healthy"
        finally:
            conn.close()
            health_server.stop()

    def test_ready_endpoint(self, health_server: HealthCheckServer) -> None:
        """Test /ready endpoint (readiness probe)."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/ready")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK
            assert data["status"] == "healthy"
        finally:
            conn.close()
            health_server.stop()

    def test_metrics_endpoint(
        self,
        health_server: HealthCheckServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The health server exposes the shared Prometheus registry."""
        payload = b"vm_indicator_bars_processed_total 2.0\n"
        monkeypatch.setattr(health_module, "metrics_payload", lambda: payload)
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/metrics")
            response = conn.getresponse()

            assert response.status == HTTP_OK
            assert response.getheader("Content-Type") == health_module.CONTENT_TYPE_LATEST
            assert response.getheader("Cache-Control") == "no-cache, no-store, must-revalidate"
            assert response.read() == payload
        finally:
            conn.close()
            health_server.stop()

    def test_health_endpoint_unhealthy(self, health_provider: MockHealthProvider) -> None:
        """Test /health endpoint returns unhealthy status."""
        health_provider.status = HealthStatus.UNHEALTHY
        health_provider.message = "Service degraded"

        health_server = HealthCheckServer(
            health_check_func=health_provider.get_health_status, host="127.0.0.1", port=0
        )
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/health")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_SERVICE_UNAVAILABLE  # Service Unavailable
            assert data["status"] == "unhealthy"
            assert data["message"] == "Service degraded"
        finally:
            conn.close()
            health_server.stop()

    def test_health_endpoint_degraded(self, health_provider: MockHealthProvider) -> None:
        """Test /health endpoint returns degraded status."""
        health_provider.status = HealthStatus.DEGRADED
        health_provider.message = "Reduced capacity"

        health_server = HealthCheckServer(
            health_check_func=health_provider.get_health_status, host="127.0.0.1", port=0
        )
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/health")
            response = conn.getresponse()
            data = json.loads(response.read().decode())

            assert response.status == HTTP_OK  # Still returns 200 for degraded
            assert data["status"] == "degraded"
            assert data["message"] == "Reduced capacity"
        finally:
            conn.close()
            health_server.stop()

    def test_invalid_endpoint(self, health_server: HealthCheckServer) -> None:
        """Test invalid endpoint returns 404."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("GET", "/invalid")
            response = conn.getresponse()

            assert response.status == HTTP_NOT_FOUND
        finally:
            conn.close()
            health_server.stop()

    def test_post_request_not_allowed(self, health_server: HealthCheckServer) -> None:
        """Test POST request returns 501 Not Implemented."""
        health_server.start()
        time.sleep(0.5)

        actual_port = self._get_port(health_server)

        try:
            conn = HTTPConnection("127.0.0.1", actual_port)
            conn.request("POST", "/health")
            response = conn.getresponse()

            assert (
                response.status == HTTP_NOT_IMPLEMENTED
            )  # BaseHTTPRequestHandler returns 501 for unimplemented methods
        finally:
            conn.close()
            health_server.stop()
