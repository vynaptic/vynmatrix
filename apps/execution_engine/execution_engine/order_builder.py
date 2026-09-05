"""
Order builder with execution mode routing.

Translates signals + user configuration into broker-specific order intents.
Routes to appropriate builders based on execution mode:
- SPOT/MARGIN: Direct order builder
- FUTURES/PERPETUAL: Futures order builder
- Options spreads: Options order builder
- NOTIFY_ONLY: Notification only
"""

import math
from decimal import Decimal

from lib_common.logging import get_logger
from lib_data.market_data import normalize_product_symbol
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import extract_price_provenance

from .config import AccountState, ExecutionMode, UserExecutionConfig
from .futures_builder import FuturesOrderBuilder, to_futures_intent
from .metrics.normalization import normalize_position_side
from .models import (
    CloseQuantityOverride,
    ExecutionRequest,
    OptionsIntent,
    OrderCurrencyContext,
    OrderIntent,
)
from .options_builder import OptionsOrderBuilder, to_options_intent
from .options_single_builder import OptionsSingleOrderBuilder
from .position_sizer import PositionSizer, SizingResult

logger = get_logger(__name__)
_TARGET_QUANTITY_TOLERANCE = Decimal("0.00000001")


class OrderSizingUnavailableError(RuntimeError):
    """Raised when an entry cannot be sized from an authoritative account state."""


class UnsupportedExecutionModeError(RuntimeError):
    """Raised when no builder handles the requested execution mode.

    Fail closed: an unhandled mode must surface as a structured blocked
    result — silently building zero intents reads as a successful no-op.
    """


class OrderBuilder:
    """
    Translates a signal + user config into broker-specific order intents.

    Routes to appropriate sub-builders based on execution mode:
    - SPOT/MARGIN -> Direct order
    - FUTURES/PERPETUAL -> FuturesOrderBuilder
    - Options modes -> OptionsOrderBuilder
    - NOTIFY_ONLY -> No order, just notification
    """

    def __init__(self) -> None:
        """Initialize order builder with sub-builders."""
        self._options_builder: OptionsOrderBuilder | None = None
        self._options_single_builder: OptionsSingleOrderBuilder | None = None
        self._futures_builder: FuturesOrderBuilder | None = None
        self._position_sizer: PositionSizer | None = None

    def build(  # noqa: PLR0911
        self,
        req: ExecutionRequest,
        account: AccountState | None = None,
        market_data: dict | None = None,
    ) -> list[OrderIntent | OptionsIntent]:
        """
        Build order intents from execution request.

        Args:
            req: ExecutionRequest with signal and user configuration
            account: Current account state (for sizing)
            market_data: Current market data (price, IV, etc.)

        Returns:
            List of OrderIntent or OptionsIntent objects
        """
        # Parse user configuration
        user_config = self._parse_user_config(req)
        exec_mode = user_config.execution_mode
        broker_code = user_config.broker.value
        currency_context = getattr(req, "currency_context", None)
        close_override = getattr(req, "close_quantity_override", None)
        target_override = getattr(req, "target_position_override", None)
        if close_override is not None and target_override is not None:
            msg = "Close and target-position quantity overrides are mutually exclusive"
            raise ValueError(msg)
        if target_override is not None:
            return self._attach_currency_context(
                self._build_target_position_order(
                    req,
                    user_config,
                    account,
                    market_data,
                ),
                currency_context,
            )

        # Handle CLOSE signals
        if req.signal.action == SignalAction.CLOSE:
            return self._attach_currency_context(
                self._build_close_order(
                    req,
                    user_config,
                    broker_code,
                    account,
                    market_data,
                ),
                currency_context,
            )

        # Handle HOLD signals - no action
        if req.signal.action == SignalAction.HOLD:
            return []

        # Route to appropriate builder based on execution mode
        if exec_mode == ExecutionMode.NOTIFY_ONLY:
            logger.info("NOTIFY_ONLY mode - no order generated for %s", req.signal.symbol)
            return []

        if exec_mode in (ExecutionMode.SPOT, ExecutionMode.MARGIN):
            return self._attach_currency_context(
                self._build_spot_order(req, user_config, account, market_data),
                currency_context,
            )

        if ExecutionMode.is_futures(exec_mode):
            return self._attach_currency_context(
                self._build_futures_order(req, user_config, account, market_data),
                currency_context,
            )

        if exec_mode == ExecutionMode.OPTIONS_SINGLE:
            return self._attach_currency_context(
                list(self._build_options_single_order(req, user_config, account, market_data)),
                currency_context,
            )

        if ExecutionMode.is_options(exec_mode):
            # OptionsIntent is compatible with Union[OrderIntent, OptionsIntent]
            return self._attach_currency_context(
                list(self._build_options_order(req, user_config, account, market_data)),
                currency_context,
            )

        if exec_mode == ExecutionMode.PAPER:
            # Paper mode uses spot logic but routes to paper broker
            return self._attach_currency_context(
                self._build_spot_order(req, user_config, account, market_data),
                currency_context,
            )

        msg = f"No order builder handles execution mode {exec_mode!r}"
        raise UnsupportedExecutionModeError(msg)

    def _parse_user_config(self, req: ExecutionRequest) -> UserExecutionConfig:
        """Parse user configuration from request."""
        cfg = req.user_strategy_config or {}
        profile = req.profile or {}

        # The per-binding position cap lives in ``risk_caps`` (what the risk guard
        # enforces), while the position sizer reads the ``sizing`` block. If the
        # sizer is left to its 0.10 default while the binding cap is tighter, the
        # sizer sizes to 0.10 and the risk guard then BLOCKS the order (N7) instead
        # of the position being sized down. Reconcile the two here so the sizer never
        # sizes above the risk cap: take the min of any explicit sizing preference and
        # the binding's risk cap.
        sizing = dict(cfg.get("sizing") or {})
        risk_caps = cfg.get("risk_caps") or profile.get("risk_caps") or {}
        cap = risk_caps.get("max_position_pct")
        if cap is not None:
            existing = sizing.get("max_position_pct")
            sizing["max_position_pct"] = (
                float(cap) if existing is None else min(float(existing), float(cap))
            )

        data = {
            "user_id": req.user_id,
            "strategy_id": req.signal.strategy_id,
            "broker": cfg.get("broker", profile.get("broker", "paper")),
            "execution_mode": cfg.get("execution_mode", profile.get("execution_mode", "spot")),
            "sizing": sizing,
            "options": cfg.get("options", {}),
            "futures": cfg.get("futures", {}),
            "notification": cfg.get("notification", {}),
            "max_daily_trades": cfg.get("max_daily_trades", profile.get("max_daily_trades", 50)),
            "max_open_positions": cfg.get(
                "max_open_positions", profile.get("max_open_positions", 10)
            ),
            "enable_shorting": cfg.get("enable_shorting", profile.get("enable_shorting", True)),
            "require_stop_loss": cfg.get(
                "require_stop_loss", profile.get("require_stop_loss", True)
            ),
        }

        return UserExecutionConfig.from_dict(data)

    def _get_position_sizer(self, config: UserExecutionConfig) -> PositionSizer:
        """Get or create position sizer."""
        if self._position_sizer is None or self._position_sizer.config != config.sizing:
            self._position_sizer = PositionSizer(config.sizing)
        return self._position_sizer

    def _get_options_builder(self, config: UserExecutionConfig) -> OptionsOrderBuilder:
        """Get or create options builder."""
        if self._options_builder is None:
            self._options_builder = OptionsOrderBuilder(config.options)
        return self._options_builder

    def _get_options_single_builder(self) -> OptionsSingleOrderBuilder:
        """Get or create single-leg options builder."""
        if self._options_single_builder is None:
            self._options_single_builder = OptionsSingleOrderBuilder()
        return self._options_single_builder

    def _get_futures_builder(self, config: UserExecutionConfig) -> FuturesOrderBuilder:
        """Get or create futures builder."""
        if self._futures_builder is None:
            self._futures_builder = FuturesOrderBuilder(config.futures)
        return self._futures_builder

    @staticmethod
    def _require_account(account: AccountState | None) -> AccountState:
        if account is None:
            msg = "Order sizing requires authoritative broker account state"
            raise OrderSizingUnavailableError(msg)
        return account

    @staticmethod
    def _account_to_settlement_rate(req: ExecutionRequest) -> float:
        context = getattr(req, "currency_context", None)
        return float(context.account_to_settlement_rate) if context is not None else 1.0

    @staticmethod
    def _attach_currency_context(
        intents: list[OrderIntent | OptionsIntent],
        context: OrderCurrencyContext | None,
    ) -> list[OrderIntent | OptionsIntent]:
        for intent in intents:
            intent.currency_context = context
        return intents

    def _build_spot_order(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
    ) -> list[OrderIntent]:
        """Build spot/margin order."""
        signal = req.signal
        broker_code = config.broker.value

        # Get price
        price = market_data.get("price") if market_data else None
        if price is None:
            price = signal.entry_price or 0.0

        if price <= 0:
            logger.warning("No price available for %s; skipping order build", signal.symbol)
            return []

        account = self._require_account(account)
        sizer = self._get_position_sizer(config)
        sizing = sizer.calculate(
            price=price,
            account=account,
            stop_loss=signal.stop_loss,
            account_to_settlement_rate=self._account_to_settlement_rate(req),
        )
        quantity = sizing.quantity

        # Determine side
        side = "BUY" if signal.action == SignalAction.LONG else "SELL"

        # Build main order
        main_order = OrderIntent(
            broker_code=broker_code,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            order_type="market",
            time_in_force="gtc",
            metadata={
                "execution_mode": config.execution_mode.value,
                "confidence": signal.confidence,
                "horizon": signal.horizon,
                "signal_id": signal.signal_id,
                "reference_price": price,
            },
        )

        orders = [main_order]
        exit_side = "SELL" if side == "BUY" else "BUY"
        price_provenance = extract_price_provenance(signal.metadata)
        protective_metadata = {
            "parent_signal_id": signal.signal_id,
            "reduce_only": True,
            "market_data_source": price_provenance.get("price_source"),
            "market_data_timeframe": price_provenance.get("price_timeframe"),
            "source_price_id": price_provenance.get("source_price_id"),
            "source_content_revision": price_provenance.get("source_content_revision"),
            "source_price_ts": price_provenance.get("source_price_ts"),
        }

        # Protective exits. With BOTH a stop-loss and a take-profit, emit ONE native OCO
        # bracket — two independent SELL orders double-commit the position (the first
        # reserves the base balance, the second fails "insufficient balance"). The broker
        # adapter maps order_type="bracket" to the exchange's OCO/bracket order.
        if signal.stop_loss and signal.take_profit:
            orders.append(
                OrderIntent(
                    broker_code=broker_code,
                    symbol=signal.symbol,
                    side=exit_side,
                    quantity=quantity,
                    order_type="bracket",
                    time_in_force="gtc",
                    stop_price=signal.stop_loss,
                    limit_price=signal.take_profit,
                    metadata={**protective_metadata, "purpose": "bracket"},
                )
            )
        elif signal.stop_loss:
            orders.append(
                OrderIntent(
                    broker_code=broker_code,
                    symbol=signal.symbol,
                    side=exit_side,
                    quantity=quantity,
                    order_type="stop",
                    time_in_force="gtc",
                    stop_price=signal.stop_loss,
                    metadata={**protective_metadata, "purpose": "stop_loss"},
                )
            )
        elif signal.take_profit:
            orders.append(
                OrderIntent(
                    broker_code=broker_code,
                    symbol=signal.symbol,
                    side=exit_side,
                    quantity=quantity,
                    order_type="limit",
                    time_in_force="gtc",
                    limit_price=signal.take_profit,
                    metadata={**protective_metadata, "purpose": "take_profit"},
                )
            )

        return orders

    def target_position_quantity(
        self,
        req: ExecutionRequest,
        *,
        account: AccountState | None,
        market_data: dict | None,
    ) -> Decimal:
        """Calculate a portfolio target through the established spot sizer."""
        config = self._parse_user_config(req)
        if config.execution_mode not in {ExecutionMode.SPOT, ExecutionMode.PAPER}:
            msg = "Portfolio target reconciliation supports spot paper execution only"
            raise ValueError(msg)
        sizing_inputs = self._spot_sizing_inputs(
            req,
            config,
            account,
            market_data,
            target_position=True,
        )
        if sizing_inputs is None:
            msg = f"No current price is available to size target {req.signal.symbol}"
            raise OrderSizingUnavailableError(msg)
        _price, sizing = sizing_inputs
        target = Decimal(str(sizing.quantity))
        if not target.is_finite() or target < 0:
            msg = "Position sizer returned an invalid target quantity"
            raise ValueError(msg)
        return target

    def _spot_sizing_inputs(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
        *,
        target_position: bool = False,
    ) -> tuple[float, SizingResult] | None:
        override = getattr(req, "target_position_override", None)
        raw_price = (
            override.reference_price
            if target_position and override is not None
            else market_data.get("price")
            if market_data
            else None
        )
        if raw_price is None:
            raw_price = req.signal.entry_price or 0.0
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            return None
        current_account = self._require_account(account)
        sizer = self._get_position_sizer(config)
        fx_rate = self._account_to_settlement_rate(req)
        sizing = (
            sizer.calculate_target_position(
                price=price,
                account=current_account,
                stop_loss=req.signal.stop_loss,
                account_to_settlement_rate=fx_rate,
            )
            if target_position
            else sizer.calculate(
                price=price,
                account=current_account,
                stop_loss=req.signal.stop_loss,
                account_to_settlement_rate=fx_rate,
            )
        )
        return price, sizing

    def _build_target_position_order(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
    ) -> list[OrderIntent]:
        """Build only the strategy-relative delta to a fenced portfolio target."""
        override = req.target_position_override
        if override is None:
            msg = "Target-position order requires its internal override"
            raise ValueError(msg)
        if normalize_product_symbol(override.symbol) != normalize_product_symbol(req.signal.symbol):
            msg = "Target-position override symbol does not match the signal"
            raise ValueError(msg)
        if req.signal.action not in {SignalAction.LONG, SignalAction.CLOSE}:
            msg = "Target-position override requires LONG or CLOSE execution semantics"
            raise ValueError(msg)
        if config.execution_mode not in {ExecutionMode.SPOT, ExecutionMode.PAPER}:
            msg = "Portfolio target reconciliation supports spot paper execution only"
            raise ValueError(msg)
        current_account = self._require_account(account)
        if current_account.fetched_at < override.broker_observed_at:
            msg = "Target-position override is newer than the current broker snapshot"
            raise OrderSizingUnavailableError(msg)
        target_quantity = (
            Decimal("0")
            if override.target_allocation == 0
            else self.target_position_quantity(
                req,
                account=current_account,
                market_data=market_data,
            )
        )
        drift_fraction = self._target_drift_fraction(req)
        if drift_fraction != override.target_weight_drift_fraction:
            msg = "Target-position drift policy changed after reconciliation"
            raise ValueError(msg)
        revalidation_tolerance = max(
            _TARGET_QUANTITY_TOLERANCE,
            override.target_quantity * drift_fraction,
        )
        if abs(target_quantity - override.target_quantity) > revalidation_tolerance:
            msg = "Target position changed after authoritative reconciliation"
            raise OrderSizingUnavailableError(msg)
        broker_quantity = self._target_broker_quantity(
            current_account,
            symbol=req.signal.symbol,
        )
        if abs(broker_quantity - override.broker_quantity) > _TARGET_QUANTITY_TOLERANCE:
            msg = "Broker exposure changed after target reconciliation"
            raise OrderSizingUnavailableError(msg)
        if override.strategy_quantity - broker_quantity > _TARGET_QUANTITY_TOLERANCE:
            msg = "Strategy target exposure exceeds current broker exposure"
            raise OrderSizingUnavailableError(msg)
        delta = override.delta_quantity
        if delta == 0:
            return []
        if (
            delta < 0
            and abs(delta)
            - min(
                override.strategy_quantity,
                broker_quantity,
            )
            > _TARGET_QUANTITY_TOLERANCE
        ):
            msg = "Target-position reduction exceeds strategy-owned broker exposure"
            raise OrderSizingUnavailableError(msg)
        reduction = delta < 0
        reference_price = self._target_reference_price(req, market_data)
        if not reduction:
            self._validate_target_entry_cash(
                req,
                account=current_account,
                quantity=delta,
                price=reference_price,
            )
        metadata: dict[str, object] = {
            "purpose": "close_position" if reduction else "target_position_entry",
            "signal_id": req.signal.signal_id,
            "reference_price": reference_price,
            "rebalance_target_override": {
                "account_plan_id": override.account_plan_id,
                "plan_leg_id": override.plan_leg_id,
                "target_allocation": str(override.target_allocation),
                "target_quantity": str(override.target_quantity),
                "revalidated_target_quantity": str(target_quantity),
                "strategy_quantity": str(override.strategy_quantity),
                "broker_quantity": str(broker_quantity),
                "delta_quantity": str(delta),
                "projected_broker_quantity": str(broker_quantity + delta),
                "target_weight_drift_fraction": str(drift_fraction),
                "broker_observed_at": override.broker_observed_at.isoformat(),
                "reference_price": str(override.reference_price),
                "quote_observed_at": override.quote_observed_at.isoformat(),
                "settlement_currency": override.settlement_currency,
            },
        }
        if reduction:
            metadata["reduce_only"] = True
        return [
            OrderIntent(
                broker_code=config.broker.value,
                symbol=req.signal.symbol,
                side="SELL" if reduction else "BUY",
                quantity=float(abs(delta)),
                order_type="market",
                time_in_force="ioc" if reduction else "gtc",
                metadata=metadata,
            )
        ]

    @staticmethod
    def _validate_target_entry_cash(
        req: ExecutionRequest,
        *,
        account: AccountState,
        quantity: Decimal,
        price: float,
    ) -> None:
        required_settlement = quantity * Decimal(str(price))
        available_account = Decimal(str(account.available_cash))
        context = req.currency_context
        available_settlement = (
            context.account_to_settlement(available_account)
            if context is not None
            else available_account
        )
        if required_settlement - available_settlement > Decimal("0.000001"):
            msg = "Authoritative available cash cannot fund the target-position delta"
            raise OrderSizingUnavailableError(msg)

    @staticmethod
    def _target_drift_fraction(req: ExecutionRequest) -> Decimal:
        raw_value = req.signal.metadata.get("target_weight_drift_fraction")
        try:
            fraction = Decimal(str(raw_value))
        except (ArithmeticError, TypeError, ValueError) as exc:
            msg = "Portfolio target signal requires target_weight_drift_fraction metadata"
            raise ValueError(msg) from exc
        if not fraction.is_finite() or not 0 <= fraction < 1:
            msg = "Portfolio target signal has invalid target_weight_drift_fraction metadata"
            raise ValueError(msg)
        return fraction

    @staticmethod
    def _target_reference_price(
        req: ExecutionRequest,
        market_data: dict | None,
    ) -> float:
        override = getattr(req, "target_position_override", None)
        raw_price = override.reference_price if override is not None else None
        if raw_price is None and market_data:
            raw_price = market_data.get("price")
        if raw_price is None and override is None:
            raw_price = req.signal.entry_price
        try:
            price = float(raw_price) if raw_price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            msg = "Target-position order requires a finite positive reference price"
            raise OrderSizingUnavailableError(msg)
        return price

    @staticmethod
    def _target_broker_quantity(
        account: AccountState,
        *,
        symbol: str,
    ) -> Decimal:
        target = normalize_product_symbol(symbol)
        matches: list[Decimal] = []
        for position in account.positions:
            if normalize_product_symbol(str(position.get("symbol") or "")) != target:
                continue
            try:
                side = normalize_position_side(position.get("side"))
                quantity = Decimal(str(position.get("qty", position.get("quantity", 0))))
            except (ArithmeticError, TypeError, ValueError) as exc:
                msg = "Broker returned an invalid target-position quantity"
                raise OrderSizingUnavailableError(msg) from exc
            if not quantity.is_finite() or quantity < 0:
                msg = "Broker returned an invalid target-position quantity"
                raise OrderSizingUnavailableError(msg)
            matches.append(-quantity if side == "short" else quantity)
        if len(matches) > 1:
            msg = f"Broker returned duplicate normalized position {symbol}"
            raise OrderSizingUnavailableError(msg)
        quantity = matches[0] if matches else Decimal("0")
        if quantity < 0:
            msg = "Long-only target reconciliation found broker short exposure"
            raise OrderSizingUnavailableError(msg)
        return quantity

    def _build_futures_order(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
    ) -> list[OrderIntent]:
        """Build futures/perpetual order."""
        signal = req.signal
        builder = self._get_futures_builder(config)

        # Get price and available margin
        raw_price = market_data.get("price") if market_data else signal.entry_price
        if raw_price is None:
            logger.warning("No futures price available for %s; skipping order build", signal.symbol)
            return []
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            logger.warning("Invalid futures price for %s; skipping order build", signal.symbol)
            return []
        if not math.isfinite(price) or price <= 0:
            logger.warning("Invalid futures price for %s; skipping order build", signal.symbol)
            return []

        account = self._require_account(account)
        account_to_settlement_rate = self._account_to_settlement_rate(req)
        available_margin = account.available_cash * account_to_settlement_rate
        sizer = self._get_position_sizer(config)
        sizing = sizer.calculate(
            price=price,
            account=account,
            stop_loss=signal.stop_loss,
            account_to_settlement_rate=account_to_settlement_rate,
        )
        notional_size = sizing.notional_value

        # Build futures order
        is_long = signal.action == SignalAction.LONG
        futures_result = builder.build_order(
            symbol=signal.symbol,
            is_long=is_long,
            price=price,
            notional_size=notional_size,
            available_margin=available_margin,
        )

        # Convert to order intents
        return to_futures_intent(
            futures_result=futures_result,
            broker_code=config.broker.value,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

    def _build_options_order(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
    ) -> list[OptionsIntent]:
        """Build options spread order."""
        signal = req.signal
        builder = self._get_options_builder(config)

        # Get market data
        raw_price = market_data.get("price") if market_data is not None else signal.entry_price
        raw_iv = (
            market_data.get("implied_volatility")
            if market_data is not None
            else signal.metadata.get("implied_volatility")
        )
        raw_contract_multiplier = (
            market_data.get("contract_multiplier")
            if market_data is not None and "contract_multiplier" in market_data
            else signal.metadata.get("contract_multiplier")
        )
        if raw_price is None or raw_iv is None or raw_contract_multiplier is None:
            logger.warning(
                "Options spread requires observed price, implied volatility, "
                "and contract multiplier",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
            )
            return []
        try:
            price = float(raw_price)
            iv = float(raw_iv)
            contract_multiplier = float(raw_contract_multiplier)
        except (TypeError, ValueError):
            logger.warning(
                "Options spread received non-numeric market inputs",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
            )
            return []
        if (
            not math.isfinite(price)
            or not math.isfinite(iv)
            or not math.isfinite(contract_multiplier)
            or price <= 0
            or iv <= 0
            or contract_multiplier <= 0
        ):
            logger.warning(
                "Options spread received invalid market inputs",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                price=price,
                implied_volatility=iv,
                contract_multiplier=contract_multiplier,
            )
            return []

        account = self._require_account(account)
        sizer = self._get_position_sizer(config)
        sizing = sizer.calculate(
            price=price,
            account=account,
            stop_loss=signal.stop_loss,
            account_to_settlement_rate=self._account_to_settlement_rate(req),
        )
        risk_amount = sizing.risk_amount

        # Build spread
        is_long = signal.action == SignalAction.LONG
        spread_result = builder.build_spread(
            execution_mode=config.execution_mode,
            underlying_price=price,
            is_long=is_long,
            implied_volatility=iv,
            risk_amount=risk_amount,
            contract_multiplier=contract_multiplier,
            underlying_symbol=signal.symbol,
        )

        # Convert to options intent
        intent = to_options_intent(
            symbol=signal.symbol,
            spread_result=spread_result,
            broker_code=config.broker.value,
        )

        return [intent]

    def _build_options_single_order(
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        account: AccountState | None,
        market_data: dict | None,
    ) -> list[OptionsIntent]:
        """Build a single-leg options order for an exact option contract."""
        builder = self._get_options_single_builder()
        account = self._require_account(account)
        intent = builder.build_entry(
            signal=req.signal,
            config=config,
            account=account,
            market_data=market_data,
            account_to_settlement_rate=self._account_to_settlement_rate(req),
        )
        return [intent] if intent is not None else []

    def _build_close_order(  # noqa: PLR0911, PLR0912
        self,
        req: ExecutionRequest,
        config: UserExecutionConfig,
        broker_code: str,
        account: AccountState | None = None,
        market_data: dict | None = None,
    ) -> list[OrderIntent | OptionsIntent]:
        """Build order to close position.

        Uses account state to determine correct side and quantity.
        If no position is found, returns an empty list (safe no-op).
        """
        signal = req.signal

        # Look up current position from account state. Compare on the canonical
        # symbol: the signal is "ETHUSD", but the broker reports its own format
        # (live Coinbase "ETH-USD", the ledger "ETH/USD"). An exact-string match
        # would never find the position in live -> the CLOSE silently no-ops and
        # the position can NEVER be flattened (unbounded risk). Normalize both.
        position = None
        target_key = normalize_product_symbol(signal.symbol)
        if account and account.positions:
            for pos in account.positions:
                if normalize_product_symbol(str(pos.get("symbol") or "")) == target_key:
                    position = pos
                    break

        if position is None:
            logger.warning(
                "No open position found for %s; skipping CLOSE order",
                signal.symbol,
            )
            return []

        pos_qty = float(position.get("qty", position.get("quantity", 0)))
        if pos_qty == 0:
            logger.warning(
                "Position quantity is zero for %s; skipping CLOSE order",
                signal.symbol,
            )
            return []

        # Determine the closing side from the stored position side.
        # Brokers (including paper) store side ("LONG"/"SHORT") separately and
        # keep quantity as a positive number, so we must NOT infer direction
        # from the sign of quantity.
        try:
            pos_side = normalize_position_side(position.get("side"))
        except (TypeError, ValueError):
            logger.exception(
                "Position for %s has invalid side %r; refusing to build CLOSE order",
                signal.symbol,
                position.get("side"),
            )
            return []
        side = "SELL" if pos_side == "long" else "BUY"
        quantity = abs(pos_qty)
        override = getattr(req, "close_quantity_override", None)
        if override is not None:
            quantity = self._validated_close_quantity_override(
                override=override,
                signal_symbol=signal.symbol,
                broker_position_quantity=pos_qty,
                broker_position_side=pos_side,
            )

        if config.execution_mode == ExecutionMode.OPTIONS_SINGLE:
            if override is not None:
                msg = "Strategy-attributed close quantity is unsupported for options contracts"
                raise ValueError(msg)
            builder = self._get_options_single_builder()
            intent = builder.build_close(
                signal=signal,
                config=config,
                position=position,
            )
            return [intent] if intent is not None else []

        close_metadata: dict[str, object] = {
            "purpose": "close_position",
            "signal_id": signal.signal_id,
            "exit_reason": signal.metadata.get("exit_reason", "signal"),
        }
        if override is not None:
            close_metadata["rebalance_close_override"] = {
                "account_plan_id": override.account_plan_id,
                "plan_leg_id": override.plan_leg_id,
                "strategy_quantity": str(override.strategy_quantity),
                "broker_quantity": str(override.broker_quantity),
                "clamped_quantity": str(override.quantity),
                "broker_observed_at": override.broker_observed_at.isoformat(),
            }
        raw_reference_price = (
            market_data.get("price")
            if market_data is not None and market_data.get("price") is not None
            else signal.entry_price
        )
        if raw_reference_price is not None:
            try:
                reference_price = float(raw_reference_price)
            except (TypeError, ValueError):
                reference_price = 0.0
            if math.isfinite(reference_price) and reference_price > 0:
                close_metadata["reference_price"] = reference_price
        if ExecutionMode.is_futures(config.execution_mode):
            expected_contract_type = (
                "perpetual" if config.execution_mode == ExecutionMode.PERPETUAL else "dated"
            )
            try:
                contract_multiplier = float(position["contract_multiplier"])
                leverage = float(position["leverage"])
            except (KeyError, TypeError, ValueError):
                logger.exception(
                    "Futures position for %s omitted exact contract economics; "
                    "refusing to build CLOSE order",
                    signal.symbol,
                )
                return []
            if (
                str(position.get("quantity_unit") or "").strip().lower() != "contracts"
                or not math.isfinite(contract_multiplier)
                or contract_multiplier <= 0
                or not math.isfinite(leverage)
                or leverage <= 0
                or str(position.get("contract_type") or "").strip().lower()
                != expected_contract_type
            ):
                logger.error(
                    "Futures position for %s has invalid contract economics; "
                    "refusing to build CLOSE order",
                    signal.symbol,
                )
                return []
            close_metadata.update(
                {
                    "execution_mode": config.execution_mode.value,
                    "contract_value_model": "linear",
                    "contract_multiplier": contract_multiplier,
                    "leverage": leverage,
                    "contract_type": expected_contract_type,
                    "reduce_only": True,
                }
            )

        close_order = OrderIntent(
            broker_code=broker_code,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            order_type="market",
            time_in_force="ioc",
            metadata=close_metadata,
        )

        return [close_order]

    @staticmethod
    def _validated_close_quantity_override(
        *,
        override: CloseQuantityOverride,
        signal_symbol: str,
        broker_position_quantity: float,
        broker_position_side: str,
    ) -> float:
        """Revalidate an internal override against the current broker snapshot."""
        if normalize_product_symbol(override.symbol) != normalize_product_symbol(signal_symbol):
            msg = "Close quantity override symbol does not match the signal"
            raise ValueError(msg)
        current_quantity = abs(float(broker_position_quantity))
        if not math.isfinite(current_quantity) or current_quantity <= 0:
            msg = "Close quantity override requires current broker exposure"
            raise ValueError(msg)
        expected_negative = broker_position_side == "short"
        if override.broker_quantity.is_signed() != expected_negative:
            msg = "Close quantity override direction changed at the broker"
            raise ValueError(msg)
        quantity = float(override.quantity)
        if not math.isfinite(quantity) or quantity <= 0 or quantity > current_quantity:
            msg = "Close quantity override exceeds current broker exposure"
            raise ValueError(msg)
        return quantity
