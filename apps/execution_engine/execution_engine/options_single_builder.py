"""
Single-leg options order builder.

Builds exact-contract options intents from canonical option symbols emitted by
signal-only strategies. The signal is expected to identify one concrete option
contract rather than an abstract directional spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from lib_common.logging import get_logger
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.types import OptionContractIdentity

from .config import AccountState, ExecutionMode, UserExecutionConfig
from .models import OptionsIntent, OptionsLeg
from .position_sizer import PositionSizer

logger = get_logger(__name__)

_EXPECTED_OPTION_SYMBOL_PARTS = 4


@dataclass(frozen=True)
class OptionContractSpec(OptionContractIdentity):
    """Concrete option contract identity and sizing metadata."""

    option_symbol: str


def parse_option_contract_symbol(
    symbol: str,
    *,
    lot_size: int,
    contract_multiplier: float,
) -> OptionContractSpec:
    """Parse a canonical option symbol with observed contract economics."""
    parts = symbol.split(":")
    if len(parts) != _EXPECTED_OPTION_SYMBOL_PARTS:
        msg = f"Invalid canonical option symbol: {symbol}"
        raise ValueError(msg)

    underlying_symbol, expiry, strike_raw, option_type_raw = parts
    if not underlying_symbol.strip():
        msg = f"Invalid canonical option symbol: {symbol}"
        raise ValueError(msg)
    try:
        date.fromisoformat(expiry)
        strike = float(strike_raw)
    except (TypeError, ValueError) as exc:
        msg = f"Invalid canonical option symbol: {symbol}"
        raise ValueError(msg) from exc
    if not math.isfinite(strike) or strike <= 0:
        msg = f"Invalid strike in canonical option symbol: {symbol}"
        raise ValueError(msg)
    option_type = option_type_raw.upper().strip()
    if option_type not in {"CALL", "PUT"}:
        msg = f"Unsupported option type in symbol: {symbol}"
        raise ValueError(msg)
    if isinstance(lot_size, bool) or lot_size <= 0:
        msg = "lot_size must be a positive integer"
        raise ValueError(msg)
    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
        msg = "contract_multiplier must be a finite positive number"
        raise ValueError(msg)

    return OptionContractSpec(
        option_symbol=symbol,
        underlying_symbol=underlying_symbol.strip(),
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        underlying_asset_class=None,
        lot_size=lot_size,
        contract_multiplier=contract_multiplier,
        underlying_price=None,
    )


def resolve_option_contract_spec(signal: Signal) -> OptionContractSpec:
    """Resolve option contract details from signal metadata or canonical symbol."""
    metadata = dict(signal.metadata or {})
    if metadata.get("lot_size") is None:
        msg = f"Option signal {signal.symbol!r} omitted observed lot_size"
        raise ValueError(msg)
    if metadata.get("contract_multiplier") is None:
        msg = f"Option signal {signal.symbol!r} omitted observed contract_multiplier"
        raise ValueError(msg)
    try:
        lot_size_decimal = Decimal(str(metadata["lot_size"]))
        contract_multiplier = float(metadata["contract_multiplier"])
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Option signal {signal.symbol!r} has invalid contract economics"
        raise ValueError(msg) from exc
    if (
        not lot_size_decimal.is_finite()
        or lot_size_decimal != lot_size_decimal.to_integral_value()
        or lot_size_decimal <= 0
    ):
        msg = f"Option signal {signal.symbol!r} has invalid lot_size"
        raise ValueError(msg)
    base = parse_option_contract_symbol(
        signal.symbol,
        lot_size=int(lot_size_decimal),
        contract_multiplier=contract_multiplier,
    )

    identity_fields = {
        "underlying_symbol": base.underlying_symbol,
        "expiry": base.expiry,
        "strike": base.strike,
        "option_type": base.option_type,
    }
    for field, canonical_value in identity_fields.items():
        supplied = metadata.get(field)
        if supplied is None:
            continue
        if field == "strike":
            try:
                matches = float(supplied) == canonical_value
            except (TypeError, ValueError):
                matches = False
        else:
            matches = str(supplied).strip().upper() == str(canonical_value).upper()
        if not matches:
            msg = (
                f"Option signal {signal.symbol!r} metadata {field}={supplied!r} "
                f"conflicts with canonical identity {canonical_value!r}"
            )
            raise ValueError(msg)

    underlying_price: float | None = None
    if metadata.get("underlying_price") is not None:
        try:
            underlying_price = float(metadata["underlying_price"])
        except (TypeError, ValueError) as exc:
            msg = f"Option signal {signal.symbol!r} has invalid underlying_price"
            raise ValueError(msg) from exc
        if not math.isfinite(underlying_price) or underlying_price <= 0:
            msg = f"Option signal {signal.symbol!r} has invalid underlying_price"
            raise ValueError(msg)

    return OptionContractSpec(
        option_symbol=signal.symbol,
        underlying_symbol=base.underlying_symbol,
        expiry=base.expiry,
        strike=base.strike,
        option_type=base.option_type,
        underlying_asset_class=(
            str(metadata["underlying_asset_class"])
            if metadata.get("underlying_asset_class") is not None
            else base.underlying_asset_class
        ),
        lot_size=base.lot_size,
        contract_multiplier=base.contract_multiplier,
        underlying_price=underlying_price,
    )


class OptionsSingleOrderBuilder:
    """Build single-leg options intents from concrete option signals."""

    def __init__(self) -> None:
        self._position_sizer: PositionSizer | None = None

    def build_entry(
        self,
        *,
        signal: Signal,
        config: UserExecutionConfig,
        account: AccountState,
        market_data: dict[str, Any] | None,
        account_to_settlement_rate: float = 1.0,
    ) -> OptionsIntent | None:
        """Build a single-leg entry order intent for an exact option contract."""
        spec = resolve_option_contract_spec(signal)
        premium = self._resolve_option_price(signal=signal, market_data=market_data)
        if premium is None or premium <= 0:
            logger.warning("Missing option premium for %s; skipping build", signal.symbol)
            return None

        stop_loss = signal.stop_loss
        if stop_loss is None or stop_loss <= 0:
            logger.warning(
                "Missing stop_loss for %s; skipping single-leg options build", signal.symbol
            )
            return None

        risk_amount = self._resolve_risk_amount(
            premium=premium,
            stop_loss=stop_loss,
            config=config,
            account=account,
            account_to_settlement_rate=account_to_settlement_rate,
        )
        quantity = self._quantity_for_risk(
            risk_amount=risk_amount,
            premium=premium,
            stop_loss=stop_loss,
            lot_size=spec.lot_size,
            contract_multiplier=spec.contract_multiplier,
        )
        if quantity <= 0:
            logger.warning(
                "Risk budget %.2f is below one lot for %s; skipping order build",
                risk_amount,
                signal.symbol,
            )
            return None

        side = "SELL" if signal.action == SignalAction.SHORT else "BUY"
        leg = OptionsLeg(
            option_type=spec.option_type,
            strike=spec.strike,
            expiry=spec.expiry,
            side=side,
            quantity=quantity,
            premium=premium,
        )

        max_loss = abs(stop_loss - premium) * quantity * spec.contract_multiplier
        max_profit = self._estimate_max_profit(
            signal=signal,
            premium=premium,
            quantity=quantity,
            contract_multiplier=spec.contract_multiplier,
        )

        metadata = {
            "execution_mode": ExecutionMode.OPTIONS_SINGLE.value,
            "signal_id": signal.signal_id,
            "reference_price": premium,
            "option_price": premium,
            "underlying_symbol": spec.underlying_symbol,
            "underlying_asset_class": spec.underlying_asset_class,
            "underlying_price": spec.underlying_price,
            "expiry": spec.expiry,
            "strike": spec.strike,
            "option_type": spec.option_type,
            "lot_size": spec.lot_size,
            "contract_multiplier": spec.contract_multiplier,
            "max_loss": max_loss,
            "max_profit": max_profit,
            "marked_level": signal.metadata.get("marked_level"),
            "session_date": signal.metadata.get("session_date"),
            "side_bucket": signal.metadata.get("side_bucket"),
            "entry_index": signal.metadata.get("entry_index"),
            "to_open_close": "to_open",
        }

        return OptionsIntent(
            broker_code=config.broker.value,
            symbol=spec.option_symbol,
            side=side,
            quantity=quantity,
            order_type="market",
            time_in_force="day",
            legs=[leg],
            spread_type=ExecutionMode.OPTIONS_SINGLE.value,
            underlying_price=spec.underlying_price,
            max_profit=max_profit,
            max_loss=max_loss,
            breakeven=None,
            metadata=metadata,
        )

    def build_close(
        self,
        *,
        signal: Signal,
        config: UserExecutionConfig,
        position: dict[str, Any],
    ) -> OptionsIntent | None:
        """Build a close order that offsets the open single-leg option position."""
        spec = resolve_option_contract_spec(signal)
        raw_qty = position.get("qty", position.get("quantity", 0))
        open_quantity = round(abs(float(raw_qty or 0)))
        if open_quantity <= 0:
            return None
        partial_requested = self._truthy(signal.metadata.get("partial_exit"))
        requested_fraction = self._requested_exit_fraction(signal) if partial_requested else 1.0
        quantity = self._close_quantity_for_fraction(
            open_quantity=open_quantity,
            lot_size=spec.lot_size,
            requested_fraction=requested_fraction,
            partial_requested=partial_requested,
        )
        partial_exit = partial_requested and quantity < open_quantity
        effective_fraction = quantity / open_quantity

        pos_side = str(position.get("side", "")).upper()
        if pos_side in {"SHORT", "SELL"}:
            side = "BUY"
        elif pos_side in {"LONG", "BUY"}:
            side = "SELL"
        else:
            logger.warning("Unknown position side '%s' for %s", pos_side, signal.symbol)
            return None

        close_price = self._resolve_option_price(signal=signal, market_data=None)
        leg = OptionsLeg(
            option_type=spec.option_type,
            strike=spec.strike,
            expiry=spec.expiry,
            side=side,
            quantity=quantity,
            premium=close_price,
        )
        metadata = {
            "execution_mode": ExecutionMode.OPTIONS_SINGLE.value,
            "purpose": "close_position",
            "signal_id": signal.signal_id,
            "reference_price": close_price,
            "option_price": close_price,
            "underlying_symbol": spec.underlying_symbol,
            "underlying_asset_class": spec.underlying_asset_class,
            "underlying_price": spec.underlying_price,
            "expiry": spec.expiry,
            "strike": spec.strike,
            "option_type": spec.option_type,
            "lot_size": spec.lot_size,
            "contract_multiplier": spec.contract_multiplier,
            "exit_reason": signal.metadata.get("exit_reason", "signal"),
            "marked_level": signal.metadata.get("marked_level"),
            "session_date": signal.metadata.get("session_date"),
            "side_bucket": signal.metadata.get("side_bucket"),
            "entry_index": signal.metadata.get("entry_index"),
            "partial_exit": partial_exit,
            "exit_qty_fraction": effective_fraction,
            "to_open_close": "to_close",
        }

        return OptionsIntent(
            broker_code=config.broker.value,
            symbol=spec.option_symbol,
            side=side,
            quantity=quantity,
            order_type="market",
            time_in_force="ioc",
            legs=[leg],
            spread_type=ExecutionMode.OPTIONS_SINGLE.value,
            underlying_price=spec.underlying_price,
            metadata=metadata,
        )

    def _get_position_sizer(self, config: UserExecutionConfig) -> PositionSizer:
        if self._position_sizer is None or self._position_sizer.config != config.sizing:
            self._position_sizer = PositionSizer(config.sizing)
        return self._position_sizer

    @staticmethod
    def _resolve_option_price(
        *,
        signal: Signal,
        market_data: dict[str, Any] | None,
    ) -> float | None:
        if market_data is not None:
            raw_price = market_data.get("price")
            if raw_price is not None:
                return float(raw_price)

        metadata = dict(signal.metadata or {})
        for candidate in (
            signal.entry_price,
            metadata.get("option_price"),
            metadata.get("reference_price"),
        ):
            if candidate is None:
                continue
            return float(candidate)
        return None

    def _resolve_risk_amount(
        self,
        *,
        premium: float,
        stop_loss: float,
        config: UserExecutionConfig,
        account: AccountState,
        account_to_settlement_rate: float = 1.0,
    ) -> float:
        sizer = self._get_position_sizer(config)
        sizing = sizer.calculate(
            price=premium,
            account=account,
            stop_loss=stop_loss,
            account_to_settlement_rate=account_to_settlement_rate,
        )
        return max(0.0, float(sizing.risk_amount))

    @staticmethod
    def _quantity_for_risk(
        *,
        risk_amount: float,
        premium: float,
        stop_loss: float,
        lot_size: int,
        contract_multiplier: float,
    ) -> int:
        risk_per_unit = abs(stop_loss - premium) * contract_multiplier
        risk_per_lot = risk_per_unit * lot_size
        if risk_per_lot <= 0:
            return 0
        lots = int(risk_amount // risk_per_lot)
        return lots * lot_size

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    @staticmethod
    def _requested_exit_fraction(signal: Signal) -> float:
        raw_fraction = signal.metadata.get("exit_qty_fraction", 1.0)
        try:
            fraction = float(raw_fraction)
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.0, fraction))

    @staticmethod
    def _close_quantity_for_fraction(
        *,
        open_quantity: int,
        lot_size: int,
        requested_fraction: float,
        partial_requested: bool,
    ) -> int:
        if not partial_requested:
            return open_quantity

        lot_size = max(1, lot_size)
        requested_quantity = open_quantity * requested_fraction
        rounded_lots = int((requested_quantity / lot_size) + 0.5)
        rounded_quantity = rounded_lots * lot_size
        if rounded_quantity <= 0 or rounded_quantity >= open_quantity:
            return open_quantity
        return min(open_quantity, rounded_quantity)

    @staticmethod
    def _estimate_max_profit(
        *,
        signal: Signal,
        premium: float,
        quantity: int,
        contract_multiplier: float,
    ) -> float | None:
        take_profit = signal.take_profit
        if take_profit is None:
            return None
        return abs(premium - float(take_profit)) * quantity * contract_multiplier
