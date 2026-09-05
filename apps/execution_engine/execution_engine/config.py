"""
Execution engine configuration types.

Defines execution modes, user configuration, and broker settings.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from lib_common.config_validation import BrokerType, ExecutionMode, normalize_broker_code
from lib_strategy.types import AccountBalance

# BrokerType and ExecutionMode come from the single canonical configuration
# module; this service module groups them with execution-specific settings.


class SizingMethod(Enum):
    """Position sizing method."""

    FIXED_AMOUNT = "fixed_amount"  # Fixed dollar amount per trade
    FIXED_PCT = "fixed_pct"  # Fixed percentage of portfolio
    RISK_PCT = "risk_pct"  # Percentage of portfolio at risk (requires stop loss)
    KELLY = "kelly"  # Kelly criterion
    VOLATILITY = "volatility"  # Volatility-adjusted sizing


@dataclass
class OptionsConfig:
    """Configuration for options order construction."""

    days_to_expiry: int = 30  # Target DTE
    days_to_expiry_min: int = 14  # Minimum DTE
    days_to_expiry_max: int = 45  # Maximum DTE
    strike_offset_pct: float = 0.05  # OTM offset (5% = 5% OTM)
    spread_width_pct: float = 0.10  # Width of spread
    max_bid_ask_spread: float = 0.10  # Max acceptable spread (10%)
    min_open_interest: int = 100  # Minimum OI for liquidity
    min_delta: float = 0.20  # Minimum delta for long legs
    max_delta: float = 0.40  # Maximum delta for long legs


@dataclass
class FuturesConfig:
    """Configuration for futures order construction."""

    contract_size: float = 1.0  # Contract size multiplier
    max_leverage: float = 10.0  # Maximum leverage
    target_leverage: float = 2.0  # Default target leverage
    use_perpetual: bool = True  # Prefer perpetual over dated

    def __post_init__(self) -> None:
        """Reject ambiguous or non-positive linear contract configuration."""
        for field_name in ("contract_size", "max_leverage", "target_leverage"):
            raw_value = getattr(self, field_name)
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                msg = f"Futures {field_name} must be finite and positive"
                raise ValueError(msg) from exc
            if not math.isfinite(value) or value <= 0:
                msg = f"Futures {field_name} must be finite and positive"
                raise ValueError(msg)
            setattr(self, field_name, value)
        if not isinstance(self.use_perpetual, bool):
            msg = "Futures use_perpetual must be a boolean"
            raise TypeError(msg)


@dataclass
class SizingConfig:
    """Position sizing configuration."""

    method: SizingMethod = SizingMethod.RISK_PCT
    fixed_amount: float = 1000.0  # Account-currency amount for FIXED_AMOUNT
    fixed_pct: float = 0.02  # For FIXED_PCT (2%)
    risk_pct: float = 0.01  # For RISK_PCT (1% at risk)
    max_position_pct: float = 0.10  # Max single position (10% of portfolio)
    min_position_size: float = 1.0  # Account-currency minimum (dust guard; 10 was too
    # high for small retail crypto accounts — a 10% cap on a $56 account is only $5.66)
    kelly_fraction: float = 0.25  # Max Kelly fraction to use


@dataclass
class NotificationConfig:
    """Notification configuration for NOTIFY_ONLY mode."""

    webhook_url: str | None = None
    email: str | None = None
    telegram_chat_id: str | None = None
    include_reasoning: bool = True


@dataclass
class UserExecutionConfig:
    """
    User-specific execution configuration.

    This is loaded from user profile and determines how signals
    are executed for a specific user/strategy combination.
    """

    user_id: str
    strategy_id: str
    broker: BrokerType = BrokerType.PAPER
    execution_mode: ExecutionMode = ExecutionMode.SPOT
    sizing: SizingConfig = field(default_factory=SizingConfig)
    options: OptionsConfig = field(default_factory=OptionsConfig)
    futures: FuturesConfig = field(default_factory=FuturesConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

    # Account limits
    max_daily_trades: int = 50
    max_open_positions: int = 10
    max_portfolio_risk_pct: float = 0.20  # Max 20% of portfolio at risk

    # Additional settings
    enable_shorting: bool = True
    require_stop_loss: bool = True
    auto_close_on_exit_signal: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserExecutionConfig":
        """Create config from dictionary (e.g., from database/cache)."""
        sizing_data = data.get("sizing", {})
        options_data = data.get("options", {})
        futures_data = data.get("futures", {})
        notification_data = data.get("notification", {})

        return cls(
            user_id=data["user_id"],
            strategy_id=data["strategy_id"],
            broker=BrokerType(normalize_broker_code(data.get("broker", "paper"))),
            execution_mode=ExecutionMode(data.get("execution_mode", "spot")),
            sizing=SizingConfig(
                method=SizingMethod(sizing_data.get("method", "risk_pct")),
                fixed_amount=sizing_data.get("fixed_amount", 1000.0),
                fixed_pct=sizing_data.get("fixed_pct", 0.02),
                risk_pct=sizing_data.get("risk_pct", 0.01),
                max_position_pct=sizing_data.get("max_position_pct", 0.10),
                min_position_size=sizing_data.get("min_position_size", 1.0),
                kelly_fraction=sizing_data.get("kelly_fraction", 0.25),
            ),
            options=OptionsConfig(
                days_to_expiry=options_data.get("days_to_expiry", 30),
                days_to_expiry_min=options_data.get("days_to_expiry_min", 14),
                days_to_expiry_max=options_data.get("days_to_expiry_max", 45),
                strike_offset_pct=options_data.get("strike_offset_pct", 0.05),
                spread_width_pct=options_data.get("spread_width_pct", 0.10),
                max_bid_ask_spread=options_data.get("max_bid_ask_spread", 0.10),
                min_open_interest=options_data.get("min_open_interest", 100),
                min_delta=options_data.get("min_delta", 0.20),
                max_delta=options_data.get("max_delta", 0.40),
            ),
            futures=FuturesConfig(
                contract_size=futures_data.get("contract_size", 1.0),
                max_leverage=futures_data.get("max_leverage", 10.0),
                target_leverage=futures_data.get("target_leverage", 2.0),
                use_perpetual=futures_data.get("use_perpetual", True),
            ),
            notification=NotificationConfig(
                webhook_url=notification_data.get("webhook_url"),
                email=notification_data.get("email"),
                telegram_chat_id=notification_data.get("telegram_chat_id"),
                include_reasoning=notification_data.get("include_reasoning", True),
            ),
            max_daily_trades=data.get("max_daily_trades", 50),
            max_open_positions=data.get("max_open_positions", 10),
            max_portfolio_risk_pct=data.get("max_portfolio_risk_pct", 0.20),
            enable_shorting=data.get("enable_shorting", True),
            require_stop_loss=data.get("require_stop_loss", True),
            auto_close_on_exit_signal=data.get("auto_close_on_exit_signal", True),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "broker": self.broker.value,
            "execution_mode": self.execution_mode.value,
            "sizing": {
                "method": self.sizing.method.value,
                "fixed_amount": self.sizing.fixed_amount,
                "fixed_pct": self.sizing.fixed_pct,
                "risk_pct": self.sizing.risk_pct,
                "max_position_pct": self.sizing.max_position_pct,
                "min_position_size": self.sizing.min_position_size,
                "kelly_fraction": self.sizing.kelly_fraction,
            },
            "options": {
                "days_to_expiry": self.options.days_to_expiry,
                "days_to_expiry_min": self.options.days_to_expiry_min,
                "days_to_expiry_max": self.options.days_to_expiry_max,
                "strike_offset_pct": self.options.strike_offset_pct,
                "spread_width_pct": self.options.spread_width_pct,
                "max_bid_ask_spread": self.options.max_bid_ask_spread,
                "min_open_interest": self.options.min_open_interest,
                "min_delta": self.options.min_delta,
                "max_delta": self.options.max_delta,
            },
            "futures": {
                "contract_size": self.futures.contract_size,
                "max_leverage": self.futures.max_leverage,
                "target_leverage": self.futures.target_leverage,
                "use_perpetual": self.futures.use_perpetual,
            },
            "notification": {
                "webhook_url": self.notification.webhook_url,
                "email": self.notification.email,
                "telegram_chat_id": self.notification.telegram_chat_id,
                "include_reasoning": self.notification.include_reasoning,
            },
            "max_daily_trades": self.max_daily_trades,
            "max_open_positions": self.max_open_positions,
            "max_portfolio_risk_pct": self.max_portfolio_risk_pct,
            "enable_shorting": self.enable_shorting,
            "require_stop_loss": self.require_stop_loss,
            "auto_close_on_exit_signal": self.auto_close_on_exit_signal,
        }


@dataclass
class AccountState:
    """Current account state from broker."""

    broker: BrokerType
    equity: float  # Total account equity
    available_cash: float  # Available for trading
    margin_used: float = 0.0  # Used margin
    unrealized_pnl: float | None = 0.0  # Unrealized P&L when broker provides it
    realized_pnl: float | None = None  # Realized P&L when broker provides it
    open_positions: int = 0  # Number of open positions
    daily_trades: int = 0  # Trades today
    account_id: str | None = None
    currency: str = "USD"
    positions: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    source: Literal["broker", "profile_cache", "unattributed"] = "unattributed"
    balances: list[AccountBalance] = field(default_factory=list)

    @property
    def buying_power(self) -> float:
        """Calculate buying power."""
        return self.available_cash

    def can_open_position(self, config: UserExecutionConfig) -> bool:
        """Check if we can open a new position."""
        return self.open_positions < config.max_open_positions

    def can_trade_today(self, config: UserExecutionConfig) -> bool:
        """Check if we can make more trades today."""
        return self.daily_trades < config.max_daily_trades
