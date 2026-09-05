"""Domain layer - pure business entities and logic.

This layer has ZERO external dependencies. It contains only business rules
and domain logic that would exist regardless of the technology stack.
"""

from lib_strategy.domain.entities import ExecutionLog

__all__ = ["ExecutionLog"]
