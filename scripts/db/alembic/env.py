from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure repo root on sys.path so we can import the scoring models
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Add libs to path for lib_* imports
LIBS_DIR = os.path.join(BASE_DIR, "libs", "python")
for lib_name in ["lib_common", "lib_strategy", "lib_application"]:
    lib_path = os.path.join(LIBS_DIR, lib_name)
    if lib_path not in sys.path and os.path.isdir(lib_path):
        sys.path.insert(0, lib_path)

from lib_common.env_utils import build_database_url, load_dotenv_file  # noqa: E402
from lib_application.db.models import Base as AppBase  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = AppBase.metadata


def _resolve_database_url() -> str:
    """Resolve DATABASE_URL from env, .env, or DB_* variables."""
    # Skip full-URL override if it contains unresolved ${} placeholders
    raw_url = os.environ.get("DATABASE_URL", "")
    if raw_url and "${" not in raw_url:
        return raw_url

    env_values = load_dotenv_file(os.path.join(BASE_DIR, ".env"))
    return build_database_url(dotenv_values=env_values)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = _resolve_database_url()

    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
