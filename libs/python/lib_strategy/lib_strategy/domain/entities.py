"""Pure domain entities (no infrastructure dependencies).

These are business domain objects that represent core concepts.
They have NO dependencies on databases, frameworks, or external systems.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ExecutionLog:
    """Domain entity for execution logs."""

    log_id: str
    user_id: str
    account_id: int
    strategy_id: str
    signal_type: str  # LONG, SHORT, CLOSE
    execution_mode: str
    canonical_signal_id: int | None = None
    execution_details: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, executed, failed
    error_message: str | None = None
    run_id: str | None = None  # cross-container trace id (matches canonical_signals.run_id)
    created_at: datetime | None = None
