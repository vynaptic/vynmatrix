"""
Unified Signal dataclass - canonical source for strategy signals.

This module defines the contract between:
- Indicator strategy runners
- Execution engine
- Backtest harness

All strategies MUST emit Signal objects. No direct execution allowed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from lib_common.asset_classes import normalize_optional_asset_class


class SignalAction(Enum):
    """Signal action types.

    LONG: Open or maintain long position
    SHORT: Open or maintain short position
    CLOSE: Close current position (flatten)
    HOLD: No action, maintain current state
    """

    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Signal:
    """
    Immutable trading signal emitted by an indicator strategy.

    This is the ONLY contract between strategies and execution.
    Strategies emit Signals; they never execute trades directly.

    Attributes:
        signal_id: Unique identifier (auto-generated UUID)
        strategy_id: Identifier of the emitting strategy
        strategy_type: Strategy type (currently ``indicator``)
        symbol: Trading symbol (e.g., "BTCUSD", "SPY")
        asset_class: Asset class for aggregation (crypto, equity, futures, etc.)
        sector: Optional sector grouping
        industry: Optional industry grouping
        index: Optional index grouping
        instrument_id: Internal instrument identifier (if available)
        sector_id: Optional sector identifier
        action: Signal action (LONG, SHORT, CLOSE, HOLD)
        confidence: Confidence score [0.0, 1.0]
        timestamp: Signal generation timestamp (UTC)
        horizon: Trading horizon (intraday, swing, position)
        expected_return: Optional expected return (mu) over horizon
        predicted_risk: Optional predicted risk/volatility (sigma) over horizon
        horizon_days: Optional horizon in days (numeric)
        entry_price: Suggested entry price (optional)
        stop_loss: Suggested stop loss price (optional)
        take_profit: Suggested take profit price (optional)
        size_hint: Optional position size hint (actual sizing is engine-driven)
        strategy_version: Optional strategy/model version for traceability
        run_id: Optional run identifier (backtest/live run trace)
        source: Environment source ("backtest", "paper", "live")
        raw_score: Optional raw score for downstream scoring/backtests
        expires_at: Optional expiry timestamp (UTC)
        features: Optional feature payload for backtests/scoring
        metadata: Additional context (model_version, features, reasoning, etc.)

    Immutability:
        Signal is frozen (immutable) to ensure thread-safety and prevent
        accidental modification after emission.

    Example:
        >>> signal = Signal(
        ...     strategy_id="supertrend_mtf_v1",
        ...     strategy_type="indicator",
        ...     symbol="SPY",
        ...     action=SignalAction.LONG,
        ...     confidence=0.85,
        ...     entry_price=450.50,
        ...     stop_loss=445.00,
        ...     take_profit=460.00,
        ...     horizon="swing",
        ... )
    """

    # Required fields
    strategy_id: str
    strategy_type: str  # currently "indicator"
    symbol: str
    action: SignalAction
    confidence: float

    # Auto-generated fields
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    # Optional fields with trading context
    horizon: str | None = None  # "intraday", "swing", "position"
    expected_return: float | None = None  # mu over horizon
    predicted_risk: float | None = None  # sigma over horizon
    horizon_days: float | None = None  # numeric horizon in days
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_hint: float | None = None  # Suggestion only; sizing is engine-driven
    asset_class: str | None = None
    sector: str | None = None
    industry: str | None = None
    index: str | None = None
    instrument_id: int | str | None = None  # internal ID if available
    sector_id: int | None = None
    strategy_version: str | None = None
    run_id: str | None = None
    source: str | None = None  # "backtest", "paper", "live"
    external_signal_id: str | None = None  # Deterministic idempotency key for canonical dedup

    # Optional scoring/backtest fields
    raw_score: float | None = None
    expires_at: datetime | None = None
    features: dict[str, Any] = field(default_factory=dict)

    # Extensible metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate signal fields."""
        object.__setattr__(
            self,
            "asset_class",
            normalize_optional_asset_class(self.asset_class),
        )
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"Confidence must be in [0.0, 1.0], got {self.confidence}"
            raise ValueError(msg)

        valid_types = {"indicator"}
        if self.strategy_type not in valid_types:
            msg = f"Invalid strategy_type: {self.strategy_type}. Must be one of {valid_types}"
            raise ValueError(msg)

        if self.predicted_risk is not None and self.predicted_risk <= 0:
            msg = f"predicted_risk must be positive, got {self.predicted_risk}"
            raise ValueError(msg)

        if self.horizon_days is not None and self.horizon_days <= 0:
            msg = f"horizon_days must be positive, got {self.horizon_days}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        timestamp = self.timestamp if isinstance(self.timestamp, datetime) else None
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "sector": self.sector,
            "industry": self.industry,
            "index": self.index,
            "instrument_id": self.instrument_id,
            "sector_id": self.sector_id,
            "action": self.action.value,
            "confidence": self.confidence,
            "timestamp": timestamp.isoformat() if timestamp else None,
            "horizon": self.horizon,
            "expected_return": self.expected_return,
            "predicted_risk": self.predicted_risk,
            "horizon_days": self.horizon_days,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "size_hint": self.size_hint,
            "strategy_version": self.strategy_version,
            "run_id": self.run_id,
            "source": self.source,
            "external_signal_id": self.external_signal_id,
            "raw_score": self.raw_score,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "features": self.features,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        """Create Signal from dictionary."""
        from lib_strategy.signals.normalization import (  # noqa: PLC0415
            normalize_signal_action,
        )

        action = normalize_signal_action(data.get("action", "HOLD"))

        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
        elif timestamp is None:
            timestamp = datetime.now(tz=UTC)

        expires_at = data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            else:
                expires_at = expires_at.astimezone(UTC)

        return cls(
            signal_id=data.get("signal_id", str(uuid.uuid4())),
            strategy_id=data["strategy_id"],
            strategy_type=data["strategy_type"],
            symbol=data["symbol"],
            asset_class=data.get("asset_class"),
            sector=data.get("sector"),
            industry=data.get("industry"),
            index=data.get("index"),
            instrument_id=data.get("instrument_id"),
            sector_id=data.get("sector_id"),
            action=action,
            confidence=data["confidence"],
            timestamp=timestamp,
            horizon=data.get("horizon"),
            expected_return=data.get("expected_return"),
            predicted_risk=data.get("predicted_risk"),
            horizon_days=data.get("horizon_days"),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            take_profit=data.get("take_profit"),
            size_hint=data.get("size_hint"),
            strategy_version=data.get("strategy_version"),
            run_id=data.get("run_id"),
            source=data.get("source"),
            external_signal_id=data.get("external_signal_id"),
            raw_score=data.get("raw_score"),
            expires_at=expires_at,
            features=data.get("features", {}),
            metadata=data.get("metadata", {}),
        )

    @property
    def is_entry(self) -> bool:
        """Check if signal is an entry signal (LONG or SHORT)."""
        return self.action in (SignalAction.LONG, SignalAction.SHORT)

    @property
    def is_exit(self) -> bool:
        """Check if signal is an exit signal (CLOSE)."""
        return self.action == SignalAction.CLOSE

    @property
    def risk_reward_ratio(self) -> float | None:
        """Calculate risk/reward ratio if prices are available."""
        entry = self.entry_price
        stop = self.stop_loss
        target = self.take_profit

        if entry is None or stop is None or target is None:
            return None

        risk = abs(entry - stop)
        reward = abs(target - entry)

        if risk == 0:
            return None

        return float(reward / risk)
