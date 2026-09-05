"""
Linear-futures fill simulation for the paper broker.

State ownership stays in :class:`~execution_engine.brokers.paper.PaperBroker`:
the broker remains the single mutable ledger — balance, positions, realized
P&L, observed marks, currency contexts, contract terms, and trade history all
live there. :class:`PaperFuturesSimulator` is a stateless per-asset-class fill
model holding only the broker reference; the margin/leverage family is split
out so a change to one asset class cannot silently corrupt another.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from execution_engine.brokers.base import BrokerOrderResult
from lib_common.logging import get_logger

if TYPE_CHECKING:
    from execution_engine.brokers.paper import PaperBroker
    from execution_engine.models import OrderCurrencyContext, OrderIntent

logger = get_logger(__name__)

_BALANCE_TOLERANCE = 1e-9


def _decimal_product(*values: Decimal | float | int) -> Decimal:
    """Multiply monetary inputs without carrying binary-float artifacts."""
    result = Decimal("1")
    for value in values:
        result *= value if isinstance(value, Decimal) else Decimal(str(value))
    return result


@dataclass(frozen=True)
class _LinearFuturesTerms:
    """Exact generic terms required to account for one linear contract fill."""

    contract_multiplier: float
    leverage: float
    contract_type: str


class PaperFuturesSimulator:
    """Linear-futures margin/leverage fill model; all state lives on the broker."""

    def __init__(self, broker: PaperBroker) -> None:
        self._broker = broker

    def _resolve_linear_futures_terms(
        self,
        intent: OrderIntent,
    ) -> _LinearFuturesTerms | None:
        """Parse explicit linear futures metadata without inferring a contract."""
        metadata = dict(intent.metadata or {})
        execution_mode = str(metadata.get("execution_mode") or "").strip().lower()
        marker_present = execution_mode in {"perpetual", "futures"} or any(
            key in metadata
            for key in (
                "contract_value_model",
                "contract_multiplier",
                "contract_type",
                "leverage",
            )
        )
        if not marker_present:
            if intent.symbol in self._broker._futures_leverages:
                msg = f"Futures order for {intent.symbol} omitted persisted linear contract terms"
                raise ValueError(msg)
            return None
        if execution_mode not in {"perpetual", "futures"}:
            msg = "Linear futures order requires execution_mode futures or perpetual"
            raise ValueError(msg)
        if metadata.get("contract_value_model") != "linear":
            msg = "Futures paper execution supports only explicit linear contracts"
            raise ValueError(msg)
        contract_type = str(metadata.get("contract_type") or "").strip().lower()
        if contract_type not in {"perpetual", "dated"}:
            msg = "Futures order requires contract_type perpetual or dated"
            raise ValueError(msg)
        expected_contract_type = "perpetual" if execution_mode == "perpetual" else "dated"
        if contract_type != expected_contract_type:
            msg = "Futures execution_mode conflicts with contract_type"
            raise ValueError(msg)
        try:
            multiplier = float(str(metadata["contract_multiplier"]))
            leverage = float(str(metadata["leverage"]))
        except (KeyError, TypeError, ValueError) as exc:
            msg = "Futures order requires explicit positive contract multiplier and leverage"
            raise ValueError(msg) from exc
        if (
            not math.isfinite(multiplier)
            or multiplier <= 0
            or not math.isfinite(leverage)
            or leverage <= 0
        ):
            msg = "Futures order requires explicit positive contract multiplier and leverage"
            raise ValueError(msg)
        return _LinearFuturesTerms(
            contract_multiplier=multiplier,
            leverage=leverage,
            contract_type=contract_type,
        )

    def _projected_futures_available(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        fill_price: float,
        account_notional: float,
        account_commission: float,
        terms: _LinearFuturesTerms,
    ) -> tuple[float, float]:
        """Project post-fill available balance and gross realized P&L."""
        _equity, current_available, _margin, _unrealized = self._broker._account_economics()
        position = self._broker._positions.get(symbol)
        if position is None:
            post_margin = account_notional / terms.leverage
            return current_available - account_commission - post_margin, 0.0

        current_leverage = self._broker._futures_leverages.get(symbol)
        current_multiplier = self._broker._contract_multipliers.get(symbol)
        current_contract_type = self._broker._futures_contract_types.get(symbol)
        if current_leverage is None or current_multiplier is None or current_contract_type is None:
            msg = f"Cannot apply a futures fill to non-futures position {symbol}"
            raise ValueError(msg)
        if (
            not math.isclose(current_leverage, terms.leverage, rel_tol=1e-12, abs_tol=1e-12)
            or not math.isclose(
                current_multiplier,
                terms.contract_multiplier,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or current_contract_type != terms.contract_type
        ):
            msg = f"Futures contract terms changed while position {symbol} is open"
            raise ValueError(msg)
        basis = self._broker._position_account_basis.get(symbol)
        if basis is None:
            msg = f"Futures position {symbol} omitted account-currency basis"
            raise ValueError(msg)

        same_direction = (position.side == "LONG" and side == "BUY") or (
            position.side == "SHORT" and side == "SELL"
        )
        current_unrealized = float(position.unrealized_pnl or 0.0)
        current_margin = (
            abs(self._broker._position_notional(symbol, position.quantity, position.current_price))
            / terms.leverage
        )
        gross_realized = 0.0
        if same_direction:
            post_quantity = position.quantity + quantity
            post_basis = basis + account_notional
            post_side = position.side
        else:
            closed_quantity = min(quantity, position.quantity)
            closing_notional = account_notional * closed_quantity / quantity
            closed_basis = basis * closed_quantity / position.quantity
            gross_realized = (
                closing_notional - closed_basis
                if position.side == "LONG"
                else closed_basis - closing_notional
            )
            if quantity < position.quantity:
                post_quantity = position.quantity - quantity
                post_basis = basis - closed_basis
                post_side = position.side
            elif quantity == position.quantity:
                post_quantity = 0.0
                post_basis = 0.0
                post_side = position.side
            else:
                post_quantity = quantity - position.quantity
                post_basis = account_notional - closing_notional
                post_side = "SHORT" if side == "SELL" else "LONG"

        if post_quantity > 0:
            settlement_post_notional = _decimal_product(
                post_quantity,
                fill_price,
                terms.contract_multiplier,
            )
            context = self._broker._currency_contexts[symbol]
            post_notional = self._broker._settlement_to_account(
                context,
                settlement_post_notional,
            )
            post_unrealized = (
                post_notional - post_basis if post_side == "LONG" else post_basis - post_notional
            )
            post_margin = post_notional / terms.leverage
        else:
            post_unrealized = 0.0
            post_margin = 0.0

        projected_available = (
            current_available
            + gross_realized
            - account_commission
            - current_unrealized
            + current_margin
            + post_unrealized
            - post_margin
        )
        return projected_available, gross_realized

    def _submit_linear_futures_fill(
        self,
        *,
        intent: OrderIntent,
        order_id: str,
        fill_quantity: float,
        fill_price: float,
        commission_pct: float,
        currency_context: OrderCurrencyContext,
        terms: _LinearFuturesTerms,
    ) -> BrokerOrderResult:
        """Apply one exact linear contract fill with margin accounting."""
        reduce_only = (intent.metadata or {}).get("reduce_only") is True
        position = self._broker._positions.get(intent.symbol)
        same_direction = position is not None and (
            (position.side == "LONG" and intent.side == "BUY")
            or (position.side == "SHORT" and intent.side == "SELL")
        )
        if reduce_only and (
            position is None or same_direction or fill_quantity > position.quantity
        ):
            return BrokerOrderResult.rejected(
                "Reduce-only futures order cannot open or reverse a position",
                code="reduce_only_violation",
            )

        settlement_notional = _decimal_product(
            fill_quantity,
            fill_price,
            terms.contract_multiplier,
        )
        commission = _decimal_product(settlement_notional, commission_pct)
        account_notional = self._broker._settlement_to_account(
            currency_context,
            settlement_notional,
        )
        account_commission = self._broker._settlement_to_account(
            currency_context,
            commission,
        )
        try:
            projected_available, projected_realized = self._projected_futures_available(
                symbol=intent.symbol,
                side=intent.side,
                quantity=fill_quantity,
                fill_price=fill_price,
                account_notional=account_notional,
                account_commission=account_commission,
                terms=terms,
            )
        except ValueError as exc:
            return BrokerOrderResult.rejected(str(exc), code="contract_terms_invalid")
        if projected_available < -_BALANCE_TOLERANCE:
            return BrokerOrderResult.rejected(
                f"Insufficient futures margin: projected available balance "
                f"{projected_available:.2f} {self._broker._currency}",
                code="insufficient_margin",
            )

        trade = {
            "trade_id": f"{order_id}:fill:1",
            "order_id": order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": fill_quantity,
            "quantity_unit": "contracts",
            "contract_multiplier": terms.contract_multiplier,
            "contract_type": terms.contract_type,
            "leverage": terms.leverage,
            "price": fill_price,
            "gross_notional": account_notional,
            "commission": commission,
            "commission_currency": currency_context.settlement_currency,
            "account_commission": account_commission,
            "account_currency": self._broker._currency,
            "timestamp": datetime.now(tz=UTC),
            "asset_class": "futures",
        }
        result = BrokerOrderResult.filled(
            order_id=order_id,
            quantity=fill_quantity,
            price=fill_price,
            commission=commission,
            commission_currency=currency_context.settlement_currency,
        )
        self._broker._commit_fill_before_state(intent, result=result, trade=trade)

        self._broker._contract_multipliers[intent.symbol] = terms.contract_multiplier
        self._broker._futures_leverages[intent.symbol] = terms.leverage
        self._broker._futures_contract_types[intent.symbol] = terms.contract_type
        realized = self._broker._update_position(
            intent.symbol,
            intent.side,
            fill_quantity,
            fill_price,
            account_notional=account_notional,
            allow_reversal=not reduce_only,
        )
        if not math.isclose(
            realized,
            projected_realized,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            msg = "Futures fill projection disagreed with applied realized P&L"
            raise ValueError(msg)
        self._broker._balance += realized - account_commission
        self._broker._realized_pnl -= account_commission
        if intent.symbol not in self._broker._positions:
            self._broker._contract_multipliers.pop(intent.symbol, None)
            self._broker._futures_leverages.pop(intent.symbol, None)
            self._broker._futures_contract_types.pop(intent.symbol, None)

        self._broker._trade_history.append(trade)
        logger.info(
            "Paper linear futures trade executed: %s %s %.4f contracts @ %.4f "
            "(multiplier: %.4f, leverage: %.2fx, commission: %.2f)",
            intent.side,
            intent.symbol,
            fill_quantity,
            fill_price,
            terms.contract_multiplier,
            terms.leverage,
            float(commission),
        )
        return result

    @staticmethod
    def _seed_futures_terms(
        entry: dict[str, Any],
        *,
        quantity_unit: str,
        contract_multiplier: float | None,
    ) -> tuple[float | None, str | None]:
        """Validate optional linear-futures terms carried by a replay seed."""
        raw_leverage = entry.get("leverage")
        raw_contract_type = entry.get("contract_type")
        if raw_leverage in (None, ""):
            if raw_contract_type not in (None, ""):
                msg = "Futures position rehydration contract_type requires leverage"
                raise ValueError(msg)
            return None, None
        try:
            leverage = float(str(raw_leverage))
        except (TypeError, ValueError) as exc:
            msg = "Futures position rehydration requires positive leverage"
            raise ValueError(msg) from exc
        if (
            not math.isfinite(leverage)
            or leverage <= 0
            or quantity_unit != "contracts"
            or contract_multiplier is None
        ):
            msg = "Futures position rehydration requires exact contract economics"
            raise ValueError(msg)
        contract_type = str(raw_contract_type or "").strip().lower()
        if contract_type not in {"perpetual", "dated"}:
            msg = "Futures position rehydration requires a valid contract_type"
            raise ValueError(msg)
        return leverage, contract_type
