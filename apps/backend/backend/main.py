"""Entrypoint for the backend config API.

Connects as the tenant-scoped ``vm_backend_login`` role (via ``DATABASE_URL``) so Postgres
RLS is the hard backstop behind every request, and wires the pluggable secrets
backend (``SECRETS_BACKEND=db`` in prod) for broker-key onboarding.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from lib_application.db.session import get_session_factory
from lib_common.env_utils import parse_int_env
from lib_common.logging import get_logger
from lib_infrastructure.brokers import create_secrets_provider

from .api import create_app

logger = get_logger(__name__)


def build_app() -> FastAPI:
    """Assemble the production app graph (session factory + secrets provider)."""
    session_factory = get_session_factory()
    # The db secrets backend otherwise builds its own engine, doubling the
    # backend's pooled connections against the deployment budget.
    secrets_provider = create_secrets_provider(session_factory=session_factory)
    return create_app(session_factory=session_factory, secrets_provider=secrets_provider)


def main() -> None:
    port = parse_int_env("PORT", default=8081, min_value=1, max_value=65535, logger=logger)
    logger.info("Starting config API", port=port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
