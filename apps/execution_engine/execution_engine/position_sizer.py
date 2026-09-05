"""
Position sizing calculations.

Provides multiple sizing methods:
- Fixed amount/percentage
- Risk-based sizing (requires stop loss)
- Kelly criterion
- Volatility-adjusted
"""

import math
from dataclasses import dataclass, replace

from lib_common.logging import get_logger
from lib_strategy.position_sizing import (
    MissingStopBehavior,
    PositionSizingDecision,
    PositionSizingMethod,
    PositionSizingPolicy,
    PositionSizingRequest,
    PreBrokerQuantityRounding,
    apply_pre_broker_quantity_rounding,
)

from .config import AccountState, SizingConfig, SizingMethod

logger = get_logger(__name__)


@dataclass
class SizingResult:
    """Result of position sizing calculation."""

    quantity: float  # Number of units to trade
    notional_value: float  # Settlement-currency value of position
    risk_amount: float  # Settlement-currency amount at risk
    risk_pct: float  # Percentage of portfolio at risk
    method_used: SizingMethod
    notes: str = ""


class PositionSizer:
    """
    Calculates position sizes based on user configuration and account state.

    Supports multiple sizing methods and applies safety limits.
    """

    def __init__(self, config: SizingConfig) -> None:
        """
        Initialize position sizer.

        Args:
            config: Sizing configuration from user preferences
        """
        self.config = config

    def calculate(
        self,
        price: float,
        account: AccountState,
        stop_loss: float | None = None,
        historical_win_rate: float | None = None,
        historical_avg_win: float | None = None,
        historical_avg_loss: float | None = None,
        volatility: float | None = None,
        account_to_settlement_rate: float = 1.0,
    ) -> SizingResult:
        """
        Calculate position size.

        Args:
            price: Current price of the asset
            account: Current account state
            stop_loss: Stop loss price (required for RISK_PCT method)
            historical_win_rate: Historical win rate (for Kelly)
            historical_avg_win: Average winning trade (for Kelly)
            historical_avg_loss: Average losing trade (for Kelly)
            volatility: Current volatility (for volatility-adjusted sizing)
            account_to_settlement_rate: Settlement units per one account-currency
                unit, resolved from a point-in-time observed FX rate.

        Returns:
            SizingResult with quantity and risk metrics
        """
        if (
            isinstance(account_to_settlement_rate, bool)
            or not math.isfinite(account_to_settlement_rate)
            or account_to_settlement_rate <= 0
        ):
            msg = "Position sizing requires a finite positive account-to-settlement rate"
            raise ValueError(msg)
        if account_to_settlement_rate != 1.0:
            # Prices and stop distances are settlement-denominated.  Convert
            # every account-denominated monetary input before the shared
            # kernel computes quantity; percentages remain dimensionless.
            settlement_account = replace(
                account,
                equity=account.equity * account_to_settlement_rate,
                available_cash=account.available_cash * account_to_settlement_rate,
                margin_used=account.margin_used * account_to_settlement_rate,
            )
            settlement_config = replace(
                self.config,
                fixed_amount=self.config.fixed_amount * account_to_settlement_rate,
                min_position_size=self.config.min_position_size * account_to_settlement_rate,
            )
            return PositionSizer(settlement_config).calculate(
                price=price,
                account=settlement_account,
                stop_loss=stop_loss,
                historical_win_rate=historical_win_rate,
                historical_avg_win=historical_avg_win,
                historical_avg_loss=historical_avg_loss,
                volatility=volatility,
            )

        method = self.config.method

        if method == SizingMethod.FIXED_AMOUNT:
            result = self._size_fixed_amount(price, account)
        elif method == SizingMethod.FIXED_PCT:
            result = self._size_fixed_pct(price, account)
        elif method == SizingMethod.RISK_PCT:
            result = self._size_risk_pct(price, account, stop_loss)
        elif method == SizingMethod.KELLY:
            result = self._size_kelly(
                price,
                account,
                historical_win_rate,
                historical_avg_win,
                historical_avg_loss,
            )
        elif method == SizingMethod.VOLATILITY:
            result = self._size_volatility(price, account, volatility)
        else:
            # Default to fixed percentage
            result = self._size_fixed_pct(price, account)

        # The shared fixed/risk kernel already applied the production cap and
        # minimum ordering. Product denomination and precision belong to the
        # broker adapter; other sizing families retain their existing path.
        if result.method_used in {SizingMethod.FIXED_PCT, SizingMethod.RISK_PCT}:
            return result

        # Apply safety limits
        return self._apply_limits(result, price, account)

    def calculate_target_position(
        self,
        *,
        price: float,
        account: AccountState,
        stop_loss: float | None = None,
        account_to_settlement_rate: float = 1.0,
    ) -> SizingResult:
        """Calculate total target exposure without treating deployed cash as absent.

        Rebalance sizing derives a target from equity and submits only its
        strategy-relative delta. Actual available cash is still checked at the
        order boundary before an increase is submitted.
        """
        target_account = replace(
            account,
            available_cash=max(account.available_cash, account.equity),
        )
        return self.calculate(
            price=price,
            account=target_account,
            stop_loss=stop_loss,
            account_to_settlement_rate=account_to_settlement_rate,
        )

    def _size_fixed_amount(self, price: float, account: AccountState) -> SizingResult:
        """Size based on fixed dollar amount."""
        amount = min(self.config.fixed_amount, account.buying_power)
        quantity = amount / price

        return SizingResult(
            quantity=quantity,
            notional_value=amount,
            risk_amount=amount,  # Worst case: lose entire position
            risk_pct=amount / account.equity if account.equity > 0 else 0,
            method_used=SizingMethod.FIXED_AMOUNT,
        )

    def _size_fixed_pct(self, price: float, account: AccountState) -> SizingResult:
        """Size based on fixed percentage of portfolio."""
        policy = PositionSizingPolicy.fixed_percent(
            policy_id="execution-engine-fixed-pct-v1",
            fixed_pct=self.config.fixed_pct,
            max_position_pct=self.config.max_position_pct,
            minimum_position_notional=self.config.min_position_size,
            pre_broker_quantity_rounding=PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
        )
        decision = policy.calculate(self._shared_request(price, account, stop_loss=None))
        return self._from_shared_decision(decision, price)

    def _size_risk_pct(
        self,
        price: float,
        account: AccountState,
        stop_loss: float | None,
    ) -> SizingResult:
        """Size based on percentage of portfolio at risk."""
        policy = PositionSizingPolicy.initial_stop_risk(
            policy_id="execution-engine-risk-pct-v1",
            risk_pct=self.config.risk_pct,
            fixed_pct_fallback=self.config.fixed_pct,
            max_position_pct=self.config.max_position_pct,
            minimum_position_notional=self.config.min_position_size,
            pre_broker_quantity_rounding=PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
            missing_stop_behavior=MissingStopBehavior.FALLBACK_FIXED_PCT,
        )
        decision = policy.calculate(self._shared_request(price, account, stop_loss=stop_loss))
        if decision.fallback_reason == "missing_stop_loss":
            logger.warning("RISK_PCT sizing requires stop_loss, falling back to FIXED_PCT")
        elif decision.fallback_reason == "zero_stop_distance":
            logger.warning("Stop loss equals price, falling back to FIXED_PCT")
        return self._from_shared_decision(decision, price)

    @staticmethod
    def _shared_request(
        price: float,
        account: AccountState,
        *,
        stop_loss: float | None,
    ) -> PositionSizingRequest:
        return PositionSizingRequest(
            price=price,
            equity=account.equity,
            buying_power=account.buying_power,
            stop_loss=stop_loss,
        )

    def _from_shared_decision(
        self,
        decision: PositionSizingDecision,
        price: float,
    ) -> SizingResult:
        method = (
            SizingMethod.FIXED_PCT
            if decision.method_used is PositionSizingMethod.FIXED_PCT
            else SizingMethod.RISK_PCT
        )
        notes = (
            f"Risk per unit: {decision.risk_per_unit:.2f} settlement units"
            if method is SizingMethod.RISK_PCT and decision.risk_per_unit is not None
            else ""
        )
        if decision.capped_by_max_position:
            notes += f" | Capped at {self.config.max_position_pct:.0%} of portfolio"
        if decision.below_minimum:
            notes += f" | Below minimum notional ({self.config.min_position_size})"

        # The shared decision applies the registered price-tiered base-quantity
        # rounding policy. Product denomination and exchange increments remain
        # broker-adapter responsibilities.
        quantity = decision.quantity
        return SizingResult(
            quantity=quantity,
            notional_value=quantity * price,
            risk_amount=decision.effective_sizing_risk_amount,
            risk_pct=decision.effective_sizing_risk_pct,
            method_used=method,
            notes=notes,
        )

    def _size_kelly(
        self,
        price: float,
        account: AccountState,
        win_rate: float | None,
        avg_win: float | None,
        avg_loss: float | None,
    ) -> SizingResult:
        """Size based on Kelly criterion."""
        if win_rate is None or avg_win is None or avg_loss is None:
            logger.warning("Kelly sizing requires historical data, falling back to FIXED_PCT")
            return self._size_fixed_pct(price, account)

        # Kelly formula: f* = (p * b - q) / b
        # where p = win rate, q = 1-p, b = avg_win/avg_loss
        p = win_rate
        q = 1.0 - p
        b = avg_win / avg_loss if avg_loss > 0 else 1.0

        kelly_fraction = (p * b - q) / b

        # Apply Kelly fraction cap
        kelly_fraction = max(0, min(kelly_fraction, self.config.kelly_fraction))

        amount = account.equity * kelly_fraction
        amount = min(amount, account.buying_power)
        quantity = amount / price

        return SizingResult(
            quantity=quantity,
            notional_value=amount,
            risk_amount=amount,
            risk_pct=kelly_fraction,
            method_used=SizingMethod.KELLY,
            notes=f"Kelly: {kelly_fraction:.2%} (capped at {self.config.kelly_fraction:.2%})",
        )

    def _size_volatility(
        self,
        price: float,
        account: AccountState,
        volatility: float | None,
    ) -> SizingResult:
        """Size inversely proportional to volatility."""
        if volatility is None or volatility <= 0:
            logger.warning("Volatility sizing requires volatility, falling back to FIXED_PCT")
            return self._size_fixed_pct(price, account)

        # Base position size
        base_amount = account.equity * self.config.fixed_pct

        # Adjust for volatility (inverse relationship)
        # Target 15% volatility as baseline
        target_vol = 0.15
        vol_adjustment = target_vol / volatility

        # Cap adjustment between 0.5x and 2x
        vol_adjustment = max(0.5, min(2.0, vol_adjustment))

        amount = base_amount * vol_adjustment
        amount = min(amount, account.buying_power)
        quantity = amount / price

        return SizingResult(
            quantity=quantity,
            notional_value=amount,
            risk_amount=amount,
            risk_pct=amount / account.equity if account.equity > 0 else 0,
            method_used=SizingMethod.VOLATILITY,
            notes=f"Vol adjustment: {vol_adjustment:.2f}x (vol: {volatility:.2%})",
        )

    def _apply_limits(
        self,
        result: SizingResult,
        price: float,
        account: AccountState,
    ) -> SizingResult:
        """Apply safety limits to sizing result."""
        # Check max position size
        max_position_value = account.equity * self.config.max_position_pct
        if result.notional_value > max_position_value:
            result.quantity = max_position_value / price
            result.notional_value = max_position_value
            result.notes += f" | Capped at {self.config.max_position_pct:.0%} of portfolio"

        # Check min position size
        if result.notional_value < self.config.min_position_size:
            result.quantity = 0
            result.notional_value = 0
            result.notes += f" | Below minimum notional ({self.config.min_position_size})"

        result.quantity = apply_pre_broker_quantity_rounding(
            result.quantity,
            price,
            PreBrokerQuantityRounding.PRICE_TIERED_DECIMALS,
        )

        # Recalculate notional after rounding
        result.notional_value = result.quantity * price
        # These sizing families define risk as worst-case position notional.
        # Keep the reported risk aligned with the executable post-cap,
        # post-rounding quantity instead of the earlier requested budget.
        result.risk_amount = result.notional_value
        result.risk_pct = result.risk_amount / account.equity if account.equity > 0 else 0.0

        return result


def calculate_options_quantity(
    max_risk: float,
    spread_max_loss: float,
) -> int:
    """
    Calculate number of options contracts based on risk.

    Returns:
        Number of contracts to trade
    """
    if spread_max_loss <= 0:
        return 0

    contracts = int(max_risk / spread_max_loss)
    return max(0, contracts)


def calculate_futures_quantity(
    notional_value: float,
    contract_value: float,
    leverage: float = 1.0,
) -> float:
    """
    Calculate number of futures contracts.

    Args:
        notional_value: Desired notional exposure
        contract_value: Value of one contract
        leverage: Leverage to apply

    Returns:
        Number of contracts (can be fractional for perpetuals)
    """
    if contract_value <= 0:
        return 0

    # With leverage, we need less margin
    margin_required = notional_value / leverage
    return margin_required / contract_value
