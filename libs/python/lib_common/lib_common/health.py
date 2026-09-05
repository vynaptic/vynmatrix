"""
Standardized Health Check Utilities.

This module provides a consistent health check interface for all API services
in the vynmatrix platform monorepo. Use these utilities instead of implementing
custom /health endpoints in each service.

Usage:
    from fastapi import FastAPI
    from lib_common.health import add_health_endpoints, HealthResponse

    app = FastAPI()
    add_health_endpoints(app)

    # Or for custom health checks:
    from lib_common.health import create_health_response, HealthStatus

    @app.get("/health")
    async def health():
        # Run custom checks
        db_ok = await check_database()
        return create_health_response(
            status=HealthStatus.HEALTHY if db_ok else HealthStatus.UNHEALTHY,
            checks={"database": db_ok},
        )
"""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import Response, status
from pydantic import BaseModel, ConfigDict, Field


class HealthStatus(StrEnum):
    """Health status values following standard conventions."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


class HealthResponse(BaseModel):
    """
    Standardized health check response format.

    Follows common health check conventions for load balancers and
    monitoring systems.
    """

    status: HealthStatus = HealthStatus.HEALTHY
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: str | None = None
    checks: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(use_enum_values=True)


class ReadinessResponse(BaseModel):
    """
    Readiness probe response.

    Used by container orchestrators to determine if the
    service is ready to accept traffic.
    """

    ready: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: dict[str, bool] = Field(default_factory=dict)


class LivenessResponse(BaseModel):
    """
    Liveness probe response.

    Used by container orchestrators to determine if the
    service should be restarted.
    """

    alive: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def create_health_response(
    status: HealthStatus = HealthStatus.HEALTHY,
    version: str | None = None,
    checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a standardized health response dictionary.

    Args:
        status: Health status (healthy, unhealthy, degraded)
        version: Optional service version string
        checks: Optional dict of individual check results

    Returns:
        Dictionary suitable for JSON response
    """
    response = HealthResponse(
        status=status,
        version=version,
        checks=checks or {},
    )
    return response.model_dump(mode="json")


def create_readiness_response(
    ready: bool = True,
    checks: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """
    Create a standardized readiness response dictionary.

    Args:
        ready: Whether service is ready
        checks: Optional dict of individual readiness checks

    Returns:
        Dictionary suitable for JSON response
    """
    response = ReadinessResponse(ready=ready, checks=checks or {})
    return response.model_dump(mode="json")


def create_liveness_response(alive: bool = True) -> dict[str, Any]:
    """
    Create a standardized liveness response dictionary.

    Args:
        alive: Whether service is alive

    Returns:
        Dictionary suitable for JSON response
    """
    response = LivenessResponse(alive=alive)
    return response.model_dump(mode="json")


def add_health_endpoints(
    app: Any,
    version: str | None = None,
    custom_health_check: Callable[[], dict[str, Any]] | None = None,
    custom_readiness_check: Callable[[], dict[str, bool]] | None = None,
) -> None:
    """
    Add standardized health endpoints to a FastAPI application.

    Adds the following endpoints:
    - GET /health - Overall health status (container startup/liveness)
    - GET /ready - Readiness probe (traffic routing)
    - GET /live - Liveness probe (restart trigger)

    Args:
        app: FastAPI application instance
        version: Optional service version to include in response
        custom_health_check: Optional callable that returns additional health checks
        custom_readiness_check: Optional callable that returns readiness checks
    """

    @app.get("/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        """Health check endpoint."""
        checks = {}
        if custom_health_check:
            try:
                checks = custom_health_check()
            except Exception as e:
                checks = {"error": str(e)}

        status = HealthStatus.HEALTHY
        if checks.get("error"):
            status = HealthStatus.UNHEALTHY

        return create_health_response(status=status, version=version, checks=checks)

    @app.get("/ready", response_model=ReadinessResponse)
    async def ready(response: Response) -> dict[str, Any]:
        """Readiness probe endpoint."""
        checks: dict[str, bool] = {}
        ready_status = True

        if custom_readiness_check:
            try:
                checks = custom_readiness_check()
                ready_status = all(checks.values()) if checks else True
            except Exception:
                ready_status = False
                checks = {"error": False}

        if not ready_status:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return create_readiness_response(ready=ready_status, checks=checks)

    @app.get("/live", response_model=LivenessResponse)
    async def live() -> dict[str, Any]:
        """Liveness probe endpoint."""
        return create_liveness_response(alive=True)


__all__ = [
    "HealthResponse",
    "HealthStatus",
    "LivenessResponse",
    "ReadinessResponse",
    "add_health_endpoints",
    "create_health_response",
    "create_liveness_response",
    "create_readiness_response",
]
