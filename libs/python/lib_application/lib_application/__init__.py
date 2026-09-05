"""
Application layer - use cases and orchestration.

This layer contains application-specific business logic (use cases)
that orchestrate domain entities and call repository interfaces.

It depends on the domain layer (entities, ports) but NOT on
infrastructure (no database imports).
"""

from lib_application.outbox import OutboxRecord, OutboxStore

__all__ = [
    "OutboxRecord",
    "OutboxStore",
]
