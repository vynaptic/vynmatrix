"""Health check server for container orchestrators."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

from lib_common.health import HealthStatus
from lib_common.logging import get_logger
from lib_common.metrics import CONTENT_TYPE_LATEST, metrics_payload

logger = get_logger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a health check."""

    status: HealthStatus
    details: dict[str, Any]
    message: str | None = None


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health checks."""

    # Will be set by HealthCheckServer
    health_check_func: Callable[[], HealthCheckResult] | None = None
    service_name: str = "unknown-service"

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path in ["/health", "/healthz"]:
            self._handle_health_check()
        elif self.path == "/status":
            self._handle_detailed_status()
        elif self.path == "/ready":
            self._handle_readiness_check()
        elif self.path == "/live":
            self._handle_liveness_check()
        elif self.path == "/metrics":
            self._handle_metrics()
        else:
            self._send_json_response(404, {"error": "Not found"})

    def _handle_health_check(self) -> None:
        """Handle basic health check endpoint."""
        try:
            if self.health_check_func is None:
                self._send_json_response(
                    503, {"status": "unhealthy", "reason": "health check not configured"}
                )
                return

            result = self.health_check_func()

            if result.status == HealthStatus.HEALTHY:
                status_code = 200
            elif result.status == HealthStatus.DEGRADED:
                status_code = 200  # Still accepting traffic
            else:
                status_code = 503

            response = {"status": result.status.value}
            if result.message:
                response["message"] = result.message

            self._send_json_response(status_code, response)

        except Exception as err:
            logger.exception("Health check error")
            self._send_json_response(500, {"status": "error", "message": str(err)})

    def _handle_detailed_status(self) -> None:
        """Handle detailed status endpoint."""
        try:
            if self.health_check_func is None:
                self._send_json_response(503, {"error": "health check not configured"})
                return

            result = self.health_check_func()

            response = {
                "service": self.service_name,
                "status": result.status.value,
                "details": result.details,
            }
            if result.message:
                response["message"] = result.message

            self._send_json_response(200, response)

        except Exception as err:
            logger.exception("Status check error")
            self._send_json_response(500, {"error": str(err)})

    def _handle_readiness_check(self) -> None:
        """
        Handle readiness probe for container orchestrators.

        Readiness indicates if service can accept traffic.
        """
        self._handle_health_check()

    def _handle_liveness_check(self) -> None:
        """
        Handle liveness probe for container orchestrators.

        Liveness indicates if service is still running (simpler than health).
        """
        self._send_json_response(200, {"status": "alive", "service": self.service_name})

    def _handle_metrics(self) -> None:
        """Expose the shared Prometheus registry on the internal health port."""
        try:
            payload = metrics_payload()
        except (OSError, ValueError) as err:
            logger.exception("Metrics rendering error")
            self._send_json_response(500, {"status": "error", "message": str(err)})
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json_response(self, status_code: int, data: dict[str, Any]) -> None:
        """
        Send JSON response.

        Args:
            status_code: HTTP status code
            data: Response data to serialize as JSON
        """
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        """Override to use structured logger instead of stderr."""
        logger.debug("%s - %s", self.address_string(), format % args)


class HealthCheckServer:
    """
    HTTP server for health checks.

    Provides endpoints for:
    - /health or /healthz: Basic health check (container startup/liveness)
    - /status: Detailed status information
    - /ready: Readiness probe (traffic routing)
    - /live: Liveness probe (restart trigger)
    - /metrics: Prometheus metrics, including configured child processes
    """

    def __init__(
        self,
        port: int = 8080,
        host: str = "0.0.0.0",
        health_check_func: Callable[[], HealthCheckResult] | None = None,
        service_name: str = "vm-strategy-runner",
    ) -> None:
        """
        Initialize health check server.

        Args:
            port: Port to listen on
            host: Host to bind to (default: 0.0.0.0 for containers)
            health_check_func: Function that returns HealthCheckResult
            service_name: Name of the service for identification
        """
        self.port = port
        self.host = host
        self.service_name = service_name
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None
        self._running = False

        # Configure handler class
        HealthCheckHandler.health_check_func = health_check_func
        HealthCheckHandler.service_name = service_name

    def start(self) -> None:
        """Start health check server in background thread."""
        if self._running:
            logger.warning("Health check server already running")
            return

        try:
            self._server = HTTPServer((self.host, self.port), HealthCheckHandler)
            self._thread = Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="health-check-server",
            )
            self._thread.start()
            self._running = True
            logger.info("Health check server started on %s:%s", self.host, self.port)
        except Exception:
            logger.exception("Failed to start health check server")
            raise

    def stop(self) -> None:
        """Stop health check server gracefully."""
        if not self._running:
            logger.debug("Health check server not running")
            return

        try:
            if self._server:
                self._server.shutdown()
                logger.info("Health check server stopped")

            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5.0)

            self._running = False

        except Exception:
            logger.exception("Error stopping health check server")

    def is_running(self) -> bool:
        """
        Check if server is running.

        Returns:
            True if server is running
        """
        return self._running
