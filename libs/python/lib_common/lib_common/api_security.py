"""Shared API security middleware and authentication for all engine services.

Provides:
- SecurityHeadersMiddleware: Standard security headers on all responses
- verify_api_key: FastAPI dependency for X-API-Key authentication

Usage in any FastAPI service:
    from lib_common.api_security import SecurityHeadersMiddleware, verify_api_key

    app = FastAPI(dependencies=[Depends(verify_api_key)])
    app.add_middleware(SecurityHeadersMiddleware)
"""

import hmac
import os
from typing import Any, cast

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_admin_api_key_header = APIKeyHeader(name="X-Admin-API-Key", auto_error=False)


# Paths that are exempt from API key authentication (e.g. health probes).
# Metrics are intentionally not exempt: scrapers must use the service API key.
_AUTH_EXEMPT_PATHS = {
    "/health",
    "/health/",
    "/healthz",
    "/ready",
    "/ready/",
    "/live",
    "/live/",
    "/docs",
    "/openapi.json",
}


def is_production_environment() -> bool:
    """Return True when missing API credentials must fail closed."""
    candidates = (
        os.environ.get("ENVIRONMENT"),
        os.environ.get("ENV"),
        os.environ.get("APP_ENV"),
        os.environ.get("RUN_MODE"),
    )
    return any(
        str(value or "").strip().lower() in {"production", "prod", "live"} for value in candidates
    )


def require_service_api_key_configured() -> None:
    """Fail startup when production service auth is not configured."""
    if is_production_environment() and not os.environ.get("API_KEY"):
        msg = "API_KEY is required in production"
        raise RuntimeError(msg)


def secret_matches(presented: str | None, expected: str) -> bool:
    """Compare API keys without leaking prefix length through short-circuiting."""
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


async def verify_api_key(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> str | None:
    """Verify service API key.

    Non-health routes fail closed in production when ``API_KEY`` is missing.
    Development and test environments keep the historical no-key behavior to
    avoid breaking local workflows.
    """
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return None
    expected = os.environ.get("API_KEY")
    if not expected:
        if is_production_environment():
            raise HTTPException(status_code=503, detail="API key not configured")
        return None
    if not secret_matches(api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


async def verify_admin_api_key(
    admin_api_key: str | None = Depends(_admin_api_key_header),
    service_api_key: str | None = Depends(_api_key_header),
) -> str | None:
    """Verify admin API key for sensitive operator actions.

    ``ADMIN_API_KEY`` is mandatory in production. Outside production, an unset
    admin key preserves local developer ergonomics. When an admin key is set,
    callers may send it via ``X-Admin-API-Key``; ``X-API-Key`` is accepted only
    when it matches the admin key to support simple internal clients.
    """
    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        if is_production_environment():
            raise HTTPException(status_code=503, detail="Admin API key not configured")
        return None

    presented = admin_api_key or service_api_key
    if not secret_matches(presented, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing admin API key")
    return presented


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store"
        return cast(Response, response)
