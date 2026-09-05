"""Shared FastAPI service factory.

The service applications share authentication, security headers, health
surfaces, and lifecycle wiring through ``create_service_app()``:

* ``Depends(verify_api_key)`` route auth.
* ``SecurityHeadersMiddleware`` security-header middleware.
* ``FastAPI(title=..., version=..., dependencies=[...], lifespan=...)``.
* A ``@app.get("/health")`` route returning ``{"status": "ok"}``.

Per-service routes are added by the caller after the factory returns. The
tenant backend disables the service-key dependency only because it installs
its own mandatory admin/BFF authentication dependency.

Design notes:

* The factory is intentionally small. We do NOT extract per-service routes
  (each service still owns ``/execute-command`` and its diagnostic/evaluation routes)
  because their request/response models legitimately differ.
* ``add_health_route`` defaults to ``True`` because every existing service
  installs the same ``/health`` returning ``{"status": "ok"}``. Set to
  ``False`` if a caller wants to install a richer health endpoint.
* Future hardening can land here without touching the three call sites:
  e.g. structured-logging middleware, request-id propagation, /ready,
  /live, or Prometheus instrumentation.
"""

from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Response, status

from lib_common.api_security import (
    SecurityHeadersMiddleware,
    require_service_api_key_configured,
    verify_api_key,
)
from lib_common.env_utils import parse_int_env
from lib_common.logging import get_logger
from lib_common.metrics import CONTENT_TYPE_LATEST, metrics_payload

LifespanCallable = Callable[[FastAPI], Awaitable[None]]
"""Async-context-manager-compatible lifespan handler accepted by FastAPI."""
logger = get_logger(__name__)


def start_background_health_server(
    *,
    app: FastAPI,
    service_name: str,
    default_port: int,
) -> threading.Thread:
    """Run an ASGI health surface in one consistently configured daemon thread."""
    import uvicorn  # noqa: PLC0415

    host = os.environ.get("HOST", "0.0.0.0")
    port = parse_int_env(
        "PORT",
        default=default_port,
        min_value=1,
        max_value=65535,
        logger=logger,
    )
    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": host, "port": port, "log_level": "warning"},
        daemon=True,
        name=f"{service_name}-health-server",
    )
    thread.start()
    logger.info("Health server started", service=service_name, host=host, port=port)
    return thread


def create_service_app(
    *,
    title: str,
    version: str,
    description: str | None = None,
    lifespan: Any | None = None,
    add_health_route: bool = True,
    readiness_check: Callable[[], dict[str, bool]] | None = None,
    liveness_check: Callable[[], bool] | None = None,
    extra_dependencies: list[Any] | None = None,
    service_auth: bool = True,
) -> FastAPI:
    """Build a FastAPI app pre-wired with the project's standard middleware.

    Args:
        title: Human-readable service name (shown in OpenAPI).
        version: Semver string.
        description: Optional OpenAPI description.
        lifespan: FastAPI lifespan async context manager. Use this to wire
            startup/shutdown hooks (e.g. opening DB pools, registering
            background tasks).
        add_health_route: When ``True`` (default), install a basic
            ``GET /health`` returning ``{"status": "ok"}`` plus basic
            ``/ready``, ``/live``, and ``/metrics`` endpoints. Set to
            ``False`` if a caller installs custom endpoints.
        readiness_check: Optional callable returning named readiness checks.
            If any value is false, ``/ready`` responds with HTTP 503.
        liveness_check: Optional callable returning process liveness.
        extra_dependencies: Additional ``Depends(...)`` callables to apply
            to every route (in addition to ``verify_api_key``). Useful for
            request-id middleware or custom auth layers.
        service_auth: Apply the standard service API-key dependency and its
            production startup check. Set to ``False`` only for an app that
            installs a different fail-closed authentication boundary.

    Returns:
        A ``FastAPI`` instance with auth + security headers wired. Callers
        register their own routes against the returned app.
    """
    route_deps: list[Any] = [Depends(verify_api_key)] if service_auth else []
    if extra_dependencies:
        route_deps.extend(extra_dependencies)

    kwargs: dict[str, Any] = {
        "title": title,
        "version": version,
        "dependencies": route_deps,
    }
    if description is not None:
        kwargs["description"] = description

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service_auth:
            require_service_api_key_configured()
        if lifespan is None:
            yield
            return
        async with lifespan(app):
            yield

    kwargs["lifespan"] = _lifespan

    app = FastAPI(**kwargs)
    app.add_middleware(SecurityHeadersMiddleware)

    if add_health_route:

        @app.get("/health")
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/ready")
        async def _ready(response: Response) -> dict[str, Any]:
            checks: dict[str, bool] = {}
            if readiness_check is not None:
                try:
                    checks = readiness_check()
                except Exception as exc:  # pragma: no cover - defensive boundary
                    checks = {"readiness_check": False}
                    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                    return {"ready": False, "checks": checks, "error": str(exc)}
            ready = all(checks.values()) if checks else True
            if not ready:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"ready": ready, "checks": checks}

        @app.get("/live")
        async def _live(response: Response) -> dict[str, Any]:
            alive = True
            if liveness_check is not None:
                try:
                    alive = bool(liveness_check())
                except Exception as exc:  # pragma: no cover - defensive boundary
                    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                    return {"alive": False, "error": str(exc)}
            if not alive:
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"alive": alive}

        @app.get("/metrics")
        async def _metrics() -> Response:
            return Response(content=metrics_payload(), media_type=CONTENT_TYPE_LATEST)

    return app
