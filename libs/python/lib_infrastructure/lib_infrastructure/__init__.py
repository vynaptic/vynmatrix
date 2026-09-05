"""
Infrastructure layer - adapters for external systems.

This layer provides concrete implementations of domain ports:
- Database (SQLAlchemy)
- External APIs
- File systems
- Message queues
- etc.

The infrastructure layer depends on the domain layer but implements
its abstract interfaces (ports).
"""

from lib_infrastructure.persistence import (
    SQLAlchemyExecutionRepository,
)

__all__ = [
    "SQLAlchemyExecutionRepository",
]
