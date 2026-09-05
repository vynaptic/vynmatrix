"""Domain decision type shared by scoring and execution dispatch."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from lib_common.time_utils import now_utc


@dataclass
class ExecutionDecision:
    """
    Decision output from scoring + binding evaluation.

    This is what the scoring engine produces for each eligible user.
    The signal_id field provides traceability back to the original signal
    that triggered this decision.
    """

    user_id: str
    binding_id: int
    instrument_id: int
    broker_account_id: int

    # Scores that triggered
    asset_score: Decimal
    sector_score: Decimal | None = None
    market_score: Decimal | None = None

    # Decision
    should_execute: bool = False
    execution_mode: str = "spot"
    direction: str = "long"  # long, short

    # Sizing
    position_size_pct: Decimal = Decimal("0.0")
    position_size_usd: Decimal | None = None

    # Signal traceability (added for signal-execution correlation)
    signal_id: str | None = None
    strategy_id: str | None = None
    symbol: str | None = None

    # Metadata
    reasoning: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: now_utc())

    def __post_init__(self) -> None:
        self.user_id = self.user_id.strip()
        if not self.user_id:
            msg = "ExecutionDecision requires user_id"
            raise ValueError(msg)
        if self.binding_id <= 0 or self.instrument_id <= 0 or self.broker_account_id <= 0:
            msg = "ExecutionDecision requires positive binding, instrument, and broker account IDs"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "user_id": self.user_id,
            "binding_id": self.binding_id,
            "instrument_id": self.instrument_id,
            "broker_account_id": self.broker_account_id,
            "asset_score": str(self.asset_score),
            "sector_score": str(self.sector_score) if self.sector_score else None,
            "market_score": str(self.market_score) if self.market_score else None,
            "should_execute": self.should_execute,
            "execution_mode": self.execution_mode,
            "direction": self.direction,
            "position_size_pct": str(self.position_size_pct),
            "position_size_usd": str(self.position_size_usd) if self.position_size_usd else None,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }
