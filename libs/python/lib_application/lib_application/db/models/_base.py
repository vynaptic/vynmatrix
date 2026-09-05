"""Shared SQLAlchemy infrastructure for the ``lib_application.db.models`` package.

Holds the declarative ``Base``, the cross-domain type aliases
(``JSONType``/``ArrayType``/``SQLiteBigInteger``), and the ``generate_uuid``
helper. Every per-domain submodule under ``lib_application.db.models``
imports from this module so all ORM classes register against the **same**
``Base.metadata`` and Alembic continues to discover every table after the
``models.py`` → ``models/`` package split.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, BigInteger, Integer
from sqlalchemy.orm import DeclarativeBase


def generate_uuid() -> str:
    """Generate UUID for primary keys."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Use native JSON type for PostgreSQL.
# For SQLite tests, this works transparently.
JSONType = JSON


# ArrayType also uses JSON storage (PostgreSQL stores arrays as JSON).
ArrayType = JSON


# PostgreSQL keeps BIGINT primary keys; SQLite needs INTEGER primary keys for
# implicit autoincrement in unit tests.
SQLiteBigInteger = BigInteger().with_variant(Integer, "sqlite")


__all__ = [
    "ArrayType",
    "Base",
    "JSONType",
    "SQLiteBigInteger",
    "generate_uuid",
]
