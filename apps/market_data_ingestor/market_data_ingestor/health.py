"""Lightweight health endpoint for the market data ingestor."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI

from lib_common.app import create_service_app


def create_health_app(
    readiness_check: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    """Create a health/readiness FastAPI app.

    When ``readiness_check`` is supplied (the scheduler's freshness probe in the
    ingest path), ``/ready`` reflects whether market data is actually flowing
    rather than the static ``{"process": True}`` used by the health-only API.
    """
    app = create_service_app(
        title="Market Data Ingestor",
        version="0.1.0",
        readiness_check=readiness_check or (lambda: {"process": True}),
    )

    _start_time = datetime.now(tz=UTC)

    @app.get("/status")
    def status() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "market_data_ingestor",
            "uptime_seconds": str(int((datetime.now(tz=UTC) - _start_time).total_seconds())),
        }

    return app
