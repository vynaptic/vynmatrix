"""
Options fill simulation for the paper broker.

State ownership stays in :class:`~execution_engine.brokers.paper.PaperBroker`:
the broker remains the single mutable ledger — balance, positions, realized
P&L, observed marks, currency contexts, contract multipliers, and trade
history all live there. :class:`PaperOptionsSimulator` is a stateless
per-asset-class fill model holding only the broker reference; the options
family is split out so a change to one asset class cannot silently corrupt
another.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from execution_engine.brokers.base import BrokerOrderResult
from execution_engine.brokers.paper_futures import _decimal_product
from lib_common.logging import get_logger

if TYPE_CHECKING:
    from execution_engine.brokers.paper import PaperBroker
    from execution_engine.models import OptionsIntent, OptionsLeg

logger = get_logger(__name__)


class PaperOptionsSimulator:
    """Options fill model (single- and multi-leg); all state lives on the broker."""

    def __init__(self, broker: PaperBroker) -> None:
        self._broker = broker

    def _submit_multi_leg_options_order(  # noqa: PLR0911
        self,
        intent: OptionsIntent,
    ) -> BrokerOrderResult:
        """Atomically price and fill a multi-leg option order."""
        assert intent.legs is not None
        try:
            currency_context = self._broker._require_currency_context(intent)
        except (TypeError, ValueError) as exc:
            return BrokerOrderResult.rejected(str(exc), code="currency_context_invalid")
        order_id = str(uuid.uuid4())[:12]

        contract_multiplier = self._resolve_contract_multiplier(intent)
        if contract_multiplier is None:
            return BrokerOrderResult.rejected(
                "Options execution requires an explicit positive contract multiplier"
            )

        leg_fills: list[tuple[OptionsLeg, str, float, Decimal]] = []
        total_premium = Decimal("0")
        total_commission = Decimal("0")
        for leg in intent.legs:
            opt_price = leg.premium
            if opt_price is None:
                return BrokerOrderResult.rejected(
                    "Multi-leg paper execution requires an explicit premium for every leg"
                )
            opt_price = float(opt_price)
            if not math.isfinite(opt_price) or opt_price <= 0 or leg.quantity <= 0:
                return BrokerOrderResult.rejected(
                    "Multi-leg option premiums and quantities must be positive"
                )
            leg_symbol = self._option_leg_symbol(intent.symbol, leg)
            leg_notional = _decimal_product(opt_price, leg.quantity, contract_multiplier)
            leg_commission = abs(_decimal_product(leg_notional, self._broker._commission_pct))
            total_commission += leg_commission
            if leg.side == "BUY":
                total_premium -= leg_notional
            elif leg.side == "SELL":
                total_premium += leg_notional
            else:
                return BrokerOrderResult.rejected(f"Unknown option leg side: {leg.side!r}")
            leg_fills.append((leg, leg_symbol, opt_price, leg_commission))

        net_cost = -total_premium + total_commission
        account_net_cost = self._broker._settlement_to_account(currency_context, net_cost)
        if account_net_cost > self._broker._balance:
            return BrokerOrderResult.rejected(
                f"Insufficient balance for options order: need "
                f"{account_net_cost:.2f} {self._broker._currency}"
            )

        aggregate_quantity = float(intent.legs[0].quantity)
        net_premium_per_contract = float(
            abs(total_premium) / _decimal_product(contract_multiplier, aggregate_quantity)
        )
        trade = {
            "trade_id": f"{order_id}:fill:1",
            "order_id": order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": aggregate_quantity,
            "price": net_premium_per_contract,
            "commission": total_commission,
            "commission_currency": currency_context.settlement_currency,
            "account_currency": self._broker._currency,
            "account_net_cost": account_net_cost,
            "timestamp": datetime.now(tz=UTC),
            "asset_class": "options",
            "legs": [
                {
                    "symbol": leg_symbol,
                    "side": leg.side,
                    "quantity": leg.quantity,
                    "price": opt_price,
                    "commission": leg_commission,
                }
                for leg, leg_symbol, opt_price, leg_commission in leg_fills
            ],
        }
        result = BrokerOrderResult.filled(
            order_id=order_id,
            quantity=aggregate_quantity,
            price=net_premium_per_contract,
            commission=total_commission,
            commission_currency=currency_context.settlement_currency,
        )
        self._broker._commit_fill_before_state(intent, result=result, trade=trade)

        self._broker._balance -= account_net_cost
        self._broker._realized_pnl -= self._broker._settlement_to_account(
            currency_context,
            total_commission,
        )
        for leg, leg_symbol, opt_price, _leg_commission in leg_fills:
            self._broker.set_currency_context(leg_symbol, currency_context)
            self._broker._contract_multipliers[leg_symbol] = contract_multiplier
            account_notional = self._broker._settlement_to_account(
                currency_context,
                abs(_decimal_product(opt_price, leg.quantity, contract_multiplier)),
            )
            self._broker._update_position(
                leg_symbol,
                leg.side,
                float(leg.quantity),
                opt_price,
                account_notional=account_notional,
            )

        self._broker._trade_history.append(trade)

        logger.info(
            "Paper options trade executed: %d legs, net premium: $%.2f, commission: $%.2f",
            len(intent.legs),
            float(total_premium),
            float(total_commission),
        )

        return result

    def _submit_single_leg_options_order(self, intent: OptionsIntent) -> BrokerOrderResult:
        """Submit a one-leg option order keyed by the exact contract symbol."""
        try:
            currency_context = self._broker._require_currency_context(intent)
        except (TypeError, ValueError) as exc:
            return BrokerOrderResult.rejected(str(exc), code="currency_context_invalid")
        if not intent.legs:
            return BrokerOrderResult.rejected("Options order requires at least one leg")
        leg = intent.legs[0]
        contract_multiplier = self._resolve_contract_multiplier(intent)
        if contract_multiplier is None:
            return BrokerOrderResult.rejected(
                "Options execution requires an explicit positive contract multiplier"
            )
        option_price = self._resolve_single_leg_option_price(intent, leg)
        if option_price is None or not math.isfinite(option_price) or option_price <= 0:
            return BrokerOrderResult.rejected(f"No option price available for {intent.symbol}")

        order_id = str(uuid.uuid4())[:12]

        notional = abs(_decimal_product(option_price, leg.quantity, contract_multiplier))
        commission = _decimal_product(notional, self._broker._commission_pct)
        account_notional = self._broker._settlement_to_account(currency_context, notional)
        account_commission = self._broker._settlement_to_account(currency_context, commission)

        if leg.side == "BUY":
            cost = account_notional + account_commission
            if cost > self._broker._balance:
                return BrokerOrderResult.rejected(
                    f"Insufficient balance for options order: need "
                    f"{cost:.2f} {self._broker._currency}"
                )
        trade = {
            "trade_id": f"{order_id}:fill:1",
            "order_id": order_id,
            "symbol": intent.symbol,
            "side": leg.side,
            "quantity": leg.quantity,
            "price": option_price,
            "commission": commission,
            "commission_currency": currency_context.settlement_currency,
            "account_notional": account_notional,
            "account_commission": account_commission,
            "account_currency": self._broker._currency,
            "timestamp": datetime.now(tz=UTC),
            "asset_class": "options",
        }
        result = BrokerOrderResult.filled(
            order_id=order_id,
            quantity=leg.quantity,
            price=option_price,
            commission=commission,
            commission_currency=currency_context.settlement_currency,
        )
        self._broker._commit_fill_before_state(intent, result=result, trade=trade)

        self._broker._contract_multipliers[intent.symbol] = contract_multiplier
        if leg.side == "BUY":
            self._broker._balance -= cost
        else:
            self._broker._balance += account_notional - account_commission

        self._broker._update_position(
            intent.symbol,
            leg.side,
            float(leg.quantity),
            option_price,
            account_notional=account_notional,
        )
        self._broker._realized_pnl -= account_commission
        self._broker._trade_history.append(trade)

        logger.info(
            "Paper single-leg option trade executed: %s %s %d @ %.4f "
            "(multiplier: %.2f, commission: %.2f)",
            leg.side,
            intent.symbol,
            leg.quantity,
            option_price,
            contract_multiplier,
            float(commission),
        )

        return result

    def _resolve_single_leg_option_price(
        self,
        intent: OptionsIntent,
        leg: OptionsLeg,
    ) -> float | None:
        """Resolve raw option premium for a one-leg order."""
        symbol_price = self._broker._get_price(intent.symbol)
        if symbol_price is not None:
            return symbol_price

        metadata = dict(intent.metadata or {})
        reference = metadata.get("reference_price", metadata.get("option_price"))
        if reference is not None:
            try:
                return float(reference)
            except (TypeError, ValueError):
                return None

        if intent.limit_price is not None:
            return float(intent.limit_price)
        premium = getattr(leg, "premium", None)
        return float(premium) if premium is not None else None

    @staticmethod
    def _option_leg_symbol(underlying: str, leg: OptionsLeg) -> str:
        """Build a stable paper-book identity for one exact option contract."""
        strike = format(float(leg.strike), "g")
        return f"{underlying}:{leg.expiry}:{strike}:{str(leg.option_type).upper()}"

    @staticmethod
    def _resolve_contract_multiplier(intent: OptionsIntent) -> float | None:
        """Return an explicit finite contract multiplier; never assume a market convention."""
        raw_multiplier = (intent.metadata or {}).get("contract_multiplier")
        try:
            multiplier = float(str(raw_multiplier))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(multiplier) or multiplier <= 0:
            return None
        return multiplier
