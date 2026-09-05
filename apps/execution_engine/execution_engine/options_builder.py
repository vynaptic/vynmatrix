"""
Options order builder.

Pure adapter: delegates spread construction to lib_strategy and maps results
into execution-engine intents.
"""

from dataclasses import dataclass
from typing import cast

from lib_strategy.spreads.option_spreads import build_spread
from lib_strategy.types import OptionSpread, SpreadType

from .config import ExecutionMode, OptionsConfig
from .models import OptionsIntent, OptionsLeg


@dataclass
class SpreadResult:
    """Result of spread construction."""

    legs: list[OptionsLeg]
    net_debit: float  # Positive = debit, negative = credit
    max_profit: float | None
    max_loss: float
    breakeven: float
    probability_of_profit: float
    days_to_expiry: int
    contract_multiplier: float
    notes: str = ""
    spread_type: str | None = None


class OptionsOrderBuilder:
    """
    Builds options spread orders based on signal direction and configuration.

    Delegates strategy logic to lib_strategy.spreads.option_spreads.build_spread.
    """

    def __init__(self, config: OptionsConfig) -> None:
        """Initialize options builder."""
        self.config = config

    def build_spread(
        self,
        execution_mode: ExecutionMode,
        underlying_price: float,
        is_long: bool,
        implied_volatility: float,
        risk_amount: float,
        contract_multiplier: float,
        underlying_symbol: str | None = None,
    ) -> SpreadResult:
        """
        Build an options spread based on execution mode and direction.

        Args:
            execution_mode: Type of spread to build
            underlying_price: Current price of underlying
            is_long: True for bullish, False for bearish (unused for deterministic modes)
            implied_volatility: Current IV for pricing
            risk_amount: Maximum dollar amount to risk
            contract_multiplier: Observed value represented by one option contract
            underlying_symbol: Optional symbol for metadata

        Returns:
            SpreadResult with legs and risk metrics
        """
        del is_long

        if not ExecutionMode.is_options(execution_mode):
            msg = f"Unsupported options mode: {execution_mode}"
            raise ValueError(msg)

        symbol = underlying_symbol or ""

        spread = build_spread(
            spread_type=cast(SpreadType, execution_mode.value),
            underlying_symbol=symbol,
            underlying_price=underlying_price,
            implied_volatility=implied_volatility,
            days_to_expiry=self.config.days_to_expiry,
            strike_offset_pct=self.config.strike_offset_pct,
            spread_width_pct=self.config.spread_width_pct,
            risk_amount=risk_amount,
            contract_multiplier=contract_multiplier,
        )

        return _to_spread_result(spread)


def _to_spread_result(spread: OptionSpread) -> SpreadResult:
    legs = [
        OptionsLeg(
            option_type=leg["option_type"],
            strike=float(leg["strike"]),
            expiry=str(leg["expiry"]),
            side=leg["side"],
            quantity=int(leg["quantity"]),
            premium=float(leg["premium"]),
        )
        for leg in spread["legs"]
    ]

    max_profit_raw = spread["max_profit"]

    return SpreadResult(
        legs=legs,
        net_debit=float(spread["net_debit"]),
        max_profit=None if max_profit_raw is None else float(max_profit_raw),
        max_loss=float(spread["max_loss"]),
        breakeven=float(spread["breakeven"]),
        probability_of_profit=float(spread["probability_of_profit"]),
        days_to_expiry=int(spread["days_to_expiry"]),
        contract_multiplier=float(spread["contract_multiplier"]),
        notes=str(spread.get("notes", "")),
        spread_type=str(spread.get("spread_type")) if spread.get("spread_type") else None,
    )


def to_options_intent(
    symbol: str,
    spread_result: SpreadResult,
    broker_code: str,
) -> OptionsIntent:
    """Convert SpreadResult to OptionsIntent for order execution."""
    spread_type = spread_result.spread_type
    if not spread_type and spread_result.notes:
        spread_type = spread_result.notes.split(":")[0].strip()

    metadata = {
        "net_debit": spread_result.net_debit,
        "max_profit": spread_result.max_profit,
        "max_loss": spread_result.max_loss,
        "breakeven": spread_result.breakeven,
        "probability_of_profit": spread_result.probability_of_profit,
        "days_to_expiry": spread_result.days_to_expiry,
        "contract_multiplier": spread_result.contract_multiplier,
        "notes": spread_result.notes,
    }
    for index, leg in enumerate(spread_result.legs, start=1):
        metadata[f"leg{index}_premium"] = leg.premium

    return OptionsIntent(
        broker_code=broker_code,
        symbol=symbol,
        side="BUY" if spread_result.net_debit > 0 else "SELL",
        quantity=spread_result.legs[0].quantity if spread_result.legs else 0,
        order_type="limit",
        time_in_force="day",
        legs=[leg for leg in spread_result.legs],  # noqa: C416
        spread_type=spread_type,
        underlying_price=None,
        max_profit=spread_result.max_profit,
        max_loss=spread_result.max_loss,
        breakeven=spread_result.breakeven,
        metadata=metadata,
    )
