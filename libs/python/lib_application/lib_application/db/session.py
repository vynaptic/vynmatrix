"""Database session management.

Uses PostgreSQL for all environments (dev, staging, prod).
SQLite is only used for unit tests (in-memory).
"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from lib_common.env_utils import build_database_url, parse_int_env
from lib_common.logging import get_logger
from lib_common.metrics import gauge

logger = get_logger(__name__)

_DEFAULT_POOL_SIZE = 3
_DEFAULT_MAX_OVERFLOW = 2
_DEFAULT_POOL_TIMEOUT_SECONDS = 10
_DEFAULT_POOL_RECYCLE_SECONDS = 1800
_DEFAULT_POOL_CONNECTION_BUDGET = 10

_POOL_CAPACITY = gauge(
    "vm_database_pool_capacity",
    "Configured SQLAlchemy database connection capacity.",
    ("pool",),
)
_POOL_CHECKED_OUT = gauge(
    "vm_database_pool_checked_out",
    "SQLAlchemy database connections currently checked out.",
    ("pool",),
)
_POOL_OVERFLOW = gauge(
    "vm_database_pool_overflow",
    "SQLAlchemy database overflow connections currently open.",
    ("pool",),
)
_POOL_PRESSURE = gauge(
    "vm_database_pool_pressure_ratio",
    "Checked-out SQLAlchemy connections divided by configured capacity.",
    ("pool",),
)


def _connect_args_for(url: str) -> dict[str, Any]:
    """Pin the Postgres session timezone to UTC (MD-3).

    Several columns (e.g. ``prices.ts``) are tz-naive ``timestamp`` while the
    app compares them against tz-aware UTC datetimes. Postgres converts the
    tz-aware value into the *session* timezone for that comparison, so a non-UTC
    server timezone would silently shift the boundary. Pinning the session to
    UTC removes that undocumented dependency. No-op for sqlite (tests).
    """
    if url.startswith("postgresql"):
        return {"options": "-c timezone=utc"}
    return {}


def _postgres_pool_options() -> dict[str, int | bool]:
    """Return one fail-closed, process-local PostgreSQL pool budget.

    Runtime services normally pass an explicit service-role URL. Pool controls
    therefore cannot live only in the environment-derived URL branch: doing so
    silently restores SQLAlchemy's defaults for every production container.
    The same strict settings are applied to both URL paths.
    """

    pool_size = parse_int_env(
        "DB_POOL_SIZE",
        default=_DEFAULT_POOL_SIZE,
        min_value=1,
        max_value=50,
        strict=True,
    )
    max_overflow = parse_int_env(
        "DB_MAX_OVERFLOW",
        default=_DEFAULT_MAX_OVERFLOW,
        min_value=0,
        max_value=100,
        strict=True,
    )
    pool_timeout = parse_int_env(
        "DB_POOL_TIMEOUT_SECONDS",
        default=_DEFAULT_POOL_TIMEOUT_SECONDS,
        min_value=1,
        max_value=300,
        strict=True,
    )
    pool_recycle = parse_int_env(
        "DB_POOL_RECYCLE_SECONDS",
        default=_DEFAULT_POOL_RECYCLE_SECONDS,
        min_value=60,
        max_value=86400,
        strict=True,
    )
    connection_budget = parse_int_env(
        "DB_POOL_CONNECTION_BUDGET",
        default=_DEFAULT_POOL_CONNECTION_BUDGET,
        min_value=1,
        max_value=150,
        strict=True,
    )
    if pool_size + max_overflow > connection_budget:
        msg = (
            "Database pool capacity exceeds DB_POOL_CONNECTION_BUDGET: "
            f"{pool_size}+{max_overflow}>{connection_budget}"
        )
        raise ValueError(msg)
    return {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "pool_recycle": pool_recycle,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
    }


def _pool_metric_name(url: str) -> str:
    configured = os.getenv("DB_POOL_METRIC_NAME", "").strip()
    if configured:
        return configured
    try:
        username = make_url(url).username
    except (TypeError, ValueError):
        username = None
    return username or "database"


def _instrument_pool(engine: Engine, *, pool_name: str, capacity: int) -> None:
    """Expose bounded pool utilisation without logging connection URLs."""

    if getattr(engine.dialect, "name", "") != "postgresql":
        return

    pool = engine.pool

    def _refresh(*_args: Any) -> None:
        checked_out = int(getattr(pool, "checkedout", lambda: 0)())
        overflow = max(0, int(getattr(pool, "overflow", lambda: 0)()))
        if _POOL_CAPACITY is not None:
            _POOL_CAPACITY.labels(pool=pool_name).set(capacity)
        if _POOL_CHECKED_OUT is not None:
            _POOL_CHECKED_OUT.labels(pool=pool_name).set(checked_out)
        if _POOL_OVERFLOW is not None:
            _POOL_OVERFLOW.labels(pool=pool_name).set(overflow)
        if _POOL_PRESSURE is not None:
            _POOL_PRESSURE.labels(pool=pool_name).set(checked_out / capacity if capacity else 0)

    event.listen(pool, "checkout", _refresh)
    event.listen(pool, "checkin", _refresh)
    event.listen(pool, "invalidate", _refresh)
    _refresh()


def create_engine_for_env(
    env: str | None = None, db_url: str | None = None, echo: bool = False
) -> Engine:
    """
    Create database engine based on environment.

    All environments use PostgreSQL except 'test' which uses in-memory SQLite
    for fast unit tests.

    Args:
        env: Environment name ('dev', 'test', 'staging', 'prod'). Auto-detected if None.
        db_url: Explicit database URL. Overrides env-based detection.
        echo: Enable SQL query logging

    Returns:
        SQLAlchemy Engine instance
    """
    # Auto-detect environment
    if env is None:
        env = os.getenv("ENV", "dev")

    logger.info("Creating database engine for environment: %s", env)

    if db_url is None and env == "test":
        # In-memory SQLite for unit tests only
        engine = create_engine("sqlite:///:memory:", echo=echo)
        logger.info("Using in-memory SQLite for testing")
        return engine

    resolved_url = db_url or build_database_url(env=env)
    if db_url:
        logger.info("Using explicit database URL")

    engine_options: dict[str, Any] = {
        "echo": echo,
        "connect_args": _connect_args_for(resolved_url),
    }
    pool_options: dict[str, int | bool] = {}
    if resolved_url.startswith("postgresql"):
        pool_options = _postgres_pool_options()
        engine_options.update(pool_options)

    engine = create_engine(resolved_url, **engine_options)
    if pool_options:
        capacity = int(pool_options["pool_size"]) + int(pool_options["max_overflow"])
        _instrument_pool(
            engine,
            pool_name=_pool_metric_name(resolved_url),
            capacity=capacity,
        )
    logger.info("PostgreSQL connection established for environment: %s", env)

    return engine


def get_session_factory(
    *,
    db_url: str | None = None,
    env: str | None = None,
    echo: bool = False,
    expire_on_commit: bool = False,
    engine: Engine | None = None,
) -> sessionmaker[Any]:
    """Return a configured ``sessionmaker`` bound to a canonical engine.

    Canonical SQLAlchemy session factory for apps that previously called
    ``sqlalchemy.create_engine`` directly (e.g. ``apps/execution_engine/main.py``,
    ``apps/feedback_loop_engine/feedback_loop_engine/main.py``,
    ``apps/indicator_runner/.../signal_worker.py``).

    Centralising creation here ensures every consumer gets the environment-aware
    pool sizing, ``pool_pre_ping`` semantics, and SQLite test routing implemented
    in :func:`create_engine_for_env`.

    Args:
        db_url: Explicit database URL; takes precedence over ``env``.
        env: Environment name (``dev``/``test``/``staging``/``prod``);
            auto-detected from the ``ENV`` env var when ``None``.
        echo: Enable SQL query logging.
        expire_on_commit: SQLAlchemy ``expire_on_commit`` flag for the session
            factory. Apps that hand sessions to background workers typically
            want ``False`` (the default).
        engine: Reuse an already-constructed engine instead of creating a new
            one (useful when the engine lifecycle is owned elsewhere).

    Returns:
        A ``sessionmaker`` bound to the resolved engine.
    """
    bound_engine = (
        engine if engine is not None else create_engine_for_env(env=env, db_url=db_url, echo=echo)
    )
    return sessionmaker(bound_engine, expire_on_commit=expire_on_commit)


def dispose_engine(engine: Engine | None) -> None:
    """Dispose an SQLAlchemy engine if it exists.

    Safe to call with ``None``; intended for use in app shutdown hooks where
    the engine reference may not have been initialised.
    """
    if engine is not None:
        engine.dispose()


@contextmanager
def tenant_scope(
    session: Session,
    *,
    user_id: str | None = None,
) -> Generator[Session, None, None]:
    """Set the transaction-local tenant GUC that backend RLS policies filter on.

    Wrap a per-user unit-of-work with ``user_id`` so row-level security scopes every
    tenant table to that user (``USING (user_id = current_setting('app.current_tenant'))``).
    Cross-tenant services do not use a mutable session flag: their narrowly scoped,
    NOLOGIN group role is the authorization boundary.

    The GUC is transaction-local (``set_config(..., true)``), so it resets at the
    unit-of-work's commit/rollback and cannot leak across pooled connections. The
    runtime login roles inherit exactly one matching ``vm_*`` service group.

    Usage:
        session_factory = get_session_factory()
        with session_factory() as s, tenant_scope(s, user_id="u-123"):
            ...                       # sees only u-123's rows
    """
    # set_config / RLS is Postgres-only; on other dialects (e.g. sqlite test
    # fixtures) this is a harmless no-op — sqlite has no row-level security.
    bind = getattr(session, "bind", None) or (
        session.get_bind() if hasattr(session, "get_bind") else None
    )
    is_postgres = getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"
    if is_postgres and user_id is not None:
        session.execute(
            text("SELECT set_config('app.current_tenant', :uid, true)"),
            {"uid": str(user_id)},
        )
    yield session
