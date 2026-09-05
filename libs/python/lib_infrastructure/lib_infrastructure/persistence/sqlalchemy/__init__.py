"""SQLAlchemy repository implementations.

Schema lifecycle belongs exclusively to Alembic. Session and engine helpers
live in ``lib_application.db.session``.
"""

from lib_infrastructure.persistence.sqlalchemy.repositories import (
    SQLAlchemyExecutionRepository,
)

__all__ = [
    "SQLAlchemyExecutionRepository",
]
