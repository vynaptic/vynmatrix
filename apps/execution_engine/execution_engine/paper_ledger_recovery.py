"""Rebuild local-paper account state from canonical fill truth.

``executions`` is committed before the observability and position projections.
A process can therefore stop after the canonical fill commits but before
``execution_metrics`` or ``positions`` is updated.  Restart recovery must replay
the immutable fill ledger; neither projection is authoritative enough to seed a
broker account.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func

from lib_application.db.models import Broker, LinkedBrokerAccount
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)

from .metrics.canonical_executions import (
    CanonicalExecutionFill,
    load_canonical_executions,
)
from .models import OrderCurrencyContext

_EXECUTION_QUANTUM = Decimal("0.00000001")
_REPLAYABLE_METHODS = frozenset(
    {
        "SPOT",
        "MARGIN",
        "PERP",
        "FUTURES",
        "OPTIONS_SINGLE",
        "OPTIONS_STRATEGY",
    }
)


class PaperLedgerRecoveryError(RuntimeError):
    """Canonical paper fills cannot be replayed without inventing economics."""


@dataclass(frozen=True)
class PaperLedgerPosition:
    """One open position reconstructed from ordered canonical exact fills."""

    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    quantity_unit: str
    contract_multiplier: Decimal | None
    gross_notional: Decimal
    notional_currency: str
    account_cost_basis: Decimal
    leverage: Decimal | None = None
    contract_type: str | None = None

    def as_broker_position(self) -> dict[str, Any]:
        """Return the exact seed/projection shape consumed by the paper broker."""
        return {
            "symbol": self.symbol,
            "side": self.side.lower(),
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "quantity_unit": self.quantity_unit,
            "contract_multiplier": self.contract_multiplier,
            "leverage": self.leverage,
            "contract_type": self.contract_type,
            "gross_notional": self.gross_notional,
            "notional_currency": self.notional_currency,
            # This is deliberately transient. The canonical fills and their
            # per-order FX observations remain the durable cost-basis source.
            "account_cost_basis": self.account_cost_basis,
        }


@dataclass(frozen=True)
class PaperLedgerSeed:
    """Complete deterministic seed for one owned local-paper account."""

    initial_equity: Decimal
    cash_balance: Decimal
    realized_pnl: Decimal
    currency: str
    positions: tuple[PaperLedgerPosition, ...]
    execution_count: int
    last_execution_id: int | None

    def broker_positions(self) -> list[dict[str, Any]]:
        """Return an isolated mutable position list for broker construction."""
        return [position.as_broker_position() for position in self.positions]

    @property
    def current_equity(self) -> Decimal:
        """Value the recovered book only at persisted fill-origin marks."""
        long_value = sum(
            (
                position.gross_notional
                for position in self.positions
                if position.side == "LONG" and position.leverage is None
            ),
            Decimal("0"),
        )
        short_value = sum(
            (
                position.gross_notional
                for position in self.positions
                if position.side == "SHORT" and position.leverage is None
            ),
            Decimal("0"),
        )
        futures_unrealized = sum(
            (
                position.gross_notional - position.account_cost_basis
                if position.side == "LONG"
                else position.account_cost_basis - position.gross_notional
                for position in self.positions
                if position.leverage is not None
            ),
            Decimal("0"),
        )
        return self.cash_balance + long_value - short_value + futures_unrealized


@dataclass
class _OpenPosition:
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    quantity_unit: str
    contract_multiplier: Decimal | None
    leverage: Decimal | None
    contract_type: str | None
    account_cost_basis: Decimal
    account_to_settlement_rate: Decimal


def _decimal(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        msg = f"{field} must be explicitly persisted"
        raise PaperLedgerRecoveryError(msg)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"{field} must be a finite decimal"
        raise PaperLedgerRecoveryError(msg) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        qualifier = "positive " if positive else ""
        msg = f"{field} must be a finite {qualifier}decimal"
        raise PaperLedgerRecoveryError(msg)
    return parsed


def _positive_decimal(value: Any, *, field: str) -> Decimal:
    return _decimal(value, field=field, positive=True)


def _nonnegative_decimal(value: Any, *, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed < 0:
        msg = f"{field} cannot be negative"
        raise PaperLedgerRecoveryError(msg)
    return parsed


def _normalize_currency(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized or len(normalized) > 10 or not normalized.isalnum():  # noqa: PLR2004
        msg = f"{field} must be an explicit valid currency"
        raise PaperLedgerRecoveryError(msg)
    return normalized


def _parse_observed_at(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field} must be an ISO-8601 timestamp with an offset"
        raise PaperLedgerRecoveryError(msg)
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        msg = f"{field} must be an ISO-8601 timestamp with an offset"
        raise PaperLedgerRecoveryError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = f"{field} must include an explicit UTC offset"
        raise PaperLedgerRecoveryError(msg)
    return parsed


def _currency_context(fill: CanonicalExecutionFill) -> OrderCurrencyContext:
    raw = fill.order_payload.get("currency_context")
    if not isinstance(raw, Mapping):
        msg = f"Canonical paper execution {fill.exec_id} omitted persisted FX provenance"
        raise PaperLedgerRecoveryError(msg)
    required = {
        "account_currency",
        "settlement_currency",
        "account_to_settlement_rate",
        "requested_at",
        "observed_at",
        "source",
    }
    missing = sorted(required.difference(raw))
    if missing:
        msg = f"Canonical paper execution {fill.exec_id} FX provenance omitted {', '.join(missing)}"
        raise PaperLedgerRecoveryError(msg)
    requested_at = _parse_observed_at(
        raw["requested_at"],
        field=f"execution {fill.exec_id} FX requested_at",
    )
    observed_at = _parse_observed_at(
        raw["observed_at"],
        field=f"execution {fill.exec_id} FX observed_at",
    )
    try:
        context = OrderCurrencyContext(
            account_currency=str(raw["account_currency"]),
            settlement_currency=str(raw["settlement_currency"]),
            account_to_settlement_rate=_positive_decimal(
                raw["account_to_settlement_rate"],
                field=f"execution {fill.exec_id} account_to_settlement_rate",
            ),
            requested_at=requested_at,
            observed_at=observed_at,
            source=str(raw["source"]),
        )
    except (TypeError, ValueError) as exc:
        msg = f"Canonical paper execution {fill.exec_id} has invalid FX provenance"
        raise PaperLedgerRecoveryError(msg) from exc
    if context.settlement_currency != fill.settlement_currency:
        msg = (
            f"Canonical paper execution {fill.exec_id} FX settlement currency "
            f"{context.settlement_currency} conflicts with {fill.settlement_currency}"
        )
        raise PaperLedgerRecoveryError(msg)
    if context.requested_at > fill.fill_ts:
        msg = f"Canonical paper execution {fill.exec_id} uses FX requested after its fill"
        raise PaperLedgerRecoveryError(msg)
    return context


def _payload_symbol(fill: CanonicalExecutionFill) -> str:
    symbol = str(fill.order_payload.get("symbol") or "").strip()
    if not symbol:
        msg = f"Canonical paper execution {fill.exec_id} omitted its routed symbol"
        raise PaperLedgerRecoveryError(msg)
    return symbol


def _payload_side(fill: CanonicalExecutionFill) -> str:
    side = str(fill.order_payload.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"} or side != fill.side:
        msg = f"Canonical paper execution {fill.exec_id} has inconsistent order side"
        raise PaperLedgerRecoveryError(msg)
    return side


def _contract_multiplier(fill: CanonicalExecutionFill) -> Decimal:
    metadata = fill.order_payload.get("metadata")
    if not isinstance(metadata, Mapping):
        msg = f"Canonical option execution {fill.exec_id} omitted order metadata"
        raise PaperLedgerRecoveryError(msg)
    return _positive_decimal(
        metadata.get("contract_multiplier"),
        field=f"execution {fill.exec_id} contract_multiplier",
    )


@dataclass(frozen=True)
class _LinearFuturesTerms:
    contract_multiplier: Decimal
    leverage: Decimal
    contract_type: str


def _linear_futures_terms(fill: CanonicalExecutionFill) -> _LinearFuturesTerms:
    metadata = fill.order_payload.get("metadata")
    if not isinstance(metadata, Mapping):
        msg = f"Canonical futures execution {fill.exec_id} omitted order metadata"
        raise PaperLedgerRecoveryError(msg)
    if metadata.get("contract_value_model") != "linear":
        msg = (
            f"Canonical futures execution {fill.exec_id} does not identify "
            "an explicit linear contract"
        )
        raise PaperLedgerRecoveryError(msg)
    execution_mode = str(metadata.get("execution_mode") or "").strip().lower()
    if execution_mode not in {"perpetual", "futures"}:
        msg = f"Canonical futures execution {fill.exec_id} omitted execution_mode"
        raise PaperLedgerRecoveryError(msg)
    contract_type = str(metadata.get("contract_type") or "").strip().lower()
    if contract_type not in {"perpetual", "dated"}:
        msg = f"Canonical futures execution {fill.exec_id} has invalid contract_type"
        raise PaperLedgerRecoveryError(msg)
    expected_contract_type = "perpetual" if execution_mode == "perpetual" else "dated"
    if contract_type != expected_contract_type:
        msg = (
            f"Canonical futures execution {fill.exec_id} execution_mode "
            "conflicts with contract_type"
        )
        raise PaperLedgerRecoveryError(msg)
    return _LinearFuturesTerms(
        contract_multiplier=_positive_decimal(
            metadata.get("contract_multiplier"),
            field=f"execution {fill.exec_id} contract_multiplier",
        ),
        leverage=_positive_decimal(
            metadata.get("leverage"),
            field=f"execution {fill.exec_id} leverage",
        ),
        contract_type=contract_type,
    )


def _option_legs(
    fill: CanonicalExecutionFill,
    *,
    require_premium: bool,
) -> list[dict[str, Any]]:
    raw_legs = fill.order_payload.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        msg = f"Canonical option execution {fill.exec_id} omitted option legs"
        raise PaperLedgerRecoveryError(msg)
    result: list[dict[str, Any]] = []
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, Mapping):
            msg = f"Canonical option execution {fill.exec_id} leg {index} is malformed"
            raise PaperLedgerRecoveryError(msg)
        side = str(raw_leg.get("side") or "").strip().upper()
        option_type = str(raw_leg.get("option_type") or "").strip().upper()
        expiry = str(raw_leg.get("expiry") or "").strip()
        if side not in {"BUY", "SELL"} or option_type not in {"CALL", "PUT"} or not expiry:
            msg = f"Canonical option execution {fill.exec_id} leg {index} is incomplete"
            raise PaperLedgerRecoveryError(msg)
        raw_premium = raw_leg.get("premium")
        premium = (
            _positive_decimal(
                raw_premium,
                field=f"execution {fill.exec_id} leg {index} premium",
            )
            if require_premium or raw_premium is not None
            else None
        )
        result.append(
            {
                "side": side,
                "option_type": option_type,
                "expiry": expiry,
                "strike": _positive_decimal(
                    raw_leg.get("strike"),
                    field=f"execution {fill.exec_id} leg {index} strike",
                ),
                "quantity": _positive_decimal(
                    raw_leg.get("quantity"),
                    field=f"execution {fill.exec_id} leg {index} quantity",
                ),
                "premium": premium,
            }
        )
    return result


def _option_leg_symbol(underlying: str, leg: Mapping[str, Any]) -> str:
    strike = format(leg["strike"], "f")
    if "." in strike:
        strike = strike.rstrip("0").rstrip(".")
    return f"{underlying}:{leg['expiry']}:{strike}:{leg['option_type']}"


class _PaperLedgerReducer:
    def __init__(self, *, initial_cash: Decimal, account_currency: str) -> None:
        self.cash_balance = initial_cash
        self.realized_pnl = Decimal("0")
        self.account_currency = account_currency
        self.positions: dict[str, _OpenPosition] = {}
        self.positive_quantity_by_order: dict[int, Decimal] = {}
        self.requested_quantity_by_order: dict[int, Decimal] = {}
        self.multi_leg_orders: set[int] = set()

    @staticmethod
    def _settlement_to_account(
        context: OrderCurrencyContext,
        amount: Decimal,
    ) -> Decimal:
        converted = context.settlement_to_account(amount)
        if not converted.is_finite():
            msg = "Canonical paper replay produced a non-finite account amount"
            raise PaperLedgerRecoveryError(msg)
        return converted

    def _fee_in_account(
        self,
        fill: CanonicalExecutionFill,
        context: OrderCurrencyContext,
    ) -> Decimal:
        if fill.fee_amount == 0:
            return Decimal("0")
        fee_currency = _normalize_currency(
            fill.fee_currency,
            field=f"execution {fill.exec_id} fee currency",
        )
        if fee_currency == self.account_currency:
            return fill.fee_amount
        if fee_currency == context.settlement_currency:
            return self._settlement_to_account(context, fill.fee_amount)
        msg = (
            f"Canonical paper execution {fill.exec_id} fee currency {fee_currency} "
            f"cannot be converted by its persisted {self.account_currency}/"
            f"{context.settlement_currency} observation"
        )
        raise PaperLedgerRecoveryError(msg)

    def _apply_position(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        account_notional: Decimal,
        context: OrderCurrencyContext,
        quantity_unit: str,
        contract_multiplier: Decimal | None,
        leverage: Decimal | None = None,
        contract_type: str | None = None,
        allow_reversal: bool = False,
    ) -> None:
        position = self.positions.get(symbol)
        if position is None:
            self.positions[symbol] = _OpenPosition(
                symbol=symbol,
                side="LONG" if side == "BUY" else "SHORT",
                quantity=quantity,
                entry_price=price,
                current_price=price,
                quantity_unit=quantity_unit,
                contract_multiplier=contract_multiplier,
                leverage=leverage,
                contract_type=contract_type,
                account_cost_basis=account_notional,
                account_to_settlement_rate=context.account_to_settlement_rate,
            )
            return
        if (
            position.quantity_unit != quantity_unit
            or position.contract_multiplier != contract_multiplier
            or position.leverage != leverage
            or position.contract_type != contract_type
        ):
            msg = f"Canonical paper position {symbol} changed quantity economics"
            raise PaperLedgerRecoveryError(msg)

        same_direction = (position.side == "LONG" and side == "BUY") or (
            position.side == "SHORT" and side == "SELL"
        )
        if same_direction:
            total_quantity = position.quantity + quantity
            position.entry_price = (
                position.entry_price * position.quantity + price * quantity
            ) / total_quantity
            position.quantity = total_quantity
            position.current_price = price
            position.account_cost_basis += account_notional
            position.account_to_settlement_rate = context.account_to_settlement_rate
            return

        closed_quantity = min(quantity, position.quantity)
        closing_notional = account_notional * closed_quantity / quantity
        closed_basis = position.account_cost_basis * closed_quantity / position.quantity
        if position.side == "LONG":
            self.realized_pnl += closing_notional - closed_basis
        else:
            self.realized_pnl += closed_basis - closing_notional
        if quantity >= position.quantity:
            remaining = quantity - position.quantity
            if remaining > 0 and allow_reversal:
                remaining_notional = account_notional - closing_notional
                self.positions[symbol] = _OpenPosition(
                    symbol=symbol,
                    side="SHORT" if side == "SELL" else "LONG",
                    quantity=remaining,
                    entry_price=price,
                    current_price=price,
                    quantity_unit=quantity_unit,
                    contract_multiplier=contract_multiplier,
                    leverage=leverage,
                    contract_type=contract_type,
                    account_cost_basis=remaining_notional,
                    account_to_settlement_rate=context.account_to_settlement_rate,
                )
            else:
                del self.positions[symbol]
            return
        position.quantity -= quantity
        position.account_cost_basis -= closed_basis
        position.current_price = price
        position.account_to_settlement_rate = context.account_to_settlement_rate

    def _apply_cash_flow(
        self,
        *,
        side: str,
        account_notional: Decimal,
    ) -> None:
        if side == "BUY":
            self.cash_balance -= account_notional
        else:
            self.cash_balance += account_notional

    def _remember_order_quantity(self, fill: CanonicalExecutionFill) -> None:
        requested = _positive_decimal(
            fill.order_payload.get("quantity"),
            field=f"execution {fill.exec_id} requested quantity",
        )
        previous = self.requested_quantity_by_order.setdefault(fill.order_id, requested)
        if previous != requested:
            msg = f"Canonical paper order {fill.order_id} changed requested quantity"
            raise PaperLedgerRecoveryError(msg)
        if fill.quantity > 0:
            self.positive_quantity_by_order[fill.order_id] = (
                self.positive_quantity_by_order.get(fill.order_id, Decimal("0")) + fill.quantity
            )

    def _apply_standard_fill(
        self,
        fill: CanonicalExecutionFill,
        context: OrderCurrencyContext,
    ) -> None:
        side = _payload_side(fill)
        symbol = _payload_symbol(fill)
        settlement_notional = fill.quantity * fill.price
        account_notional = self._settlement_to_account(context, settlement_notional)
        self._apply_cash_flow(side=side, account_notional=account_notional)
        self._apply_position(
            symbol=symbol,
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            account_notional=account_notional,
            context=context,
            quantity_unit="asset",
            contract_multiplier=None,
        )

    def _apply_linear_futures_fill(
        self,
        fill: CanonicalExecutionFill,
        context: OrderCurrencyContext,
    ) -> None:
        side = _payload_side(fill)
        symbol = _payload_symbol(fill)
        terms = _linear_futures_terms(fill)
        metadata = fill.order_payload["metadata"]
        assert isinstance(metadata, Mapping)
        position = self.positions.get(symbol)
        same_direction = position is not None and (
            (position.side == "LONG" and side == "BUY")
            or (position.side == "SHORT" and side == "SELL")
        )
        reduce_only = metadata.get("reduce_only") is True
        if reduce_only and (
            position is None or same_direction or fill.quantity > position.quantity
        ):
            msg = (
                f"Canonical reduce-only futures execution {fill.exec_id} "
                "would open or reverse a position"
            )
            raise PaperLedgerRecoveryError(msg)

        settlement_notional = fill.quantity * fill.price * terms.contract_multiplier
        account_notional = self._settlement_to_account(
            context,
            settlement_notional,
        )
        realized_before = self.realized_pnl
        self._apply_position(
            symbol=symbol,
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            account_notional=account_notional,
            context=context,
            quantity_unit="contracts",
            contract_multiplier=terms.contract_multiplier,
            leverage=terms.leverage,
            contract_type=terms.contract_type,
            allow_reversal=not reduce_only,
        )
        self.cash_balance += self.realized_pnl - realized_before

    def _apply_single_option_fill(
        self,
        fill: CanonicalExecutionFill,
        context: OrderCurrencyContext,
    ) -> None:
        legs = _option_legs(fill, require_premium=False)
        if len(legs) != 1:
            msg = f"Canonical single-option execution {fill.exec_id} has {len(legs)} legs"
            raise PaperLedgerRecoveryError(msg)
        leg = legs[0]
        side = _payload_side(fill)
        if side != leg["side"]:
            msg = f"Canonical single-option execution {fill.exec_id} changed leg side"
            raise PaperLedgerRecoveryError(msg)
        multiplier = _contract_multiplier(fill)
        settlement_notional = fill.quantity * fill.price * multiplier
        account_notional = self._settlement_to_account(context, settlement_notional)
        self._apply_cash_flow(side=side, account_notional=account_notional)
        self._apply_position(
            symbol=_payload_symbol(fill),
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            account_notional=account_notional,
            context=context,
            quantity_unit="contracts",
            contract_multiplier=multiplier,
        )

    def _apply_multi_option_fill(
        self,
        fill: CanonicalExecutionFill,
        context: OrderCurrencyContext,
    ) -> None:
        if fill.order_id in self.multi_leg_orders:
            msg = f"Canonical multi-leg paper order {fill.order_id} has multiple exact fills"
            raise PaperLedgerRecoveryError(msg)
        self.multi_leg_orders.add(fill.order_id)
        _payload_side(fill)
        legs = _option_legs(fill, require_premium=True)
        if len(legs) < 2:  # noqa: PLR2004
            msg = f"Canonical multi-leg execution {fill.exec_id} has fewer than two legs"
            raise PaperLedgerRecoveryError(msg)
        multiplier = _contract_multiplier(fill)
        aggregate_quantity = legs[0]["quantity"]
        signed_premium = sum(
            (
                (-leg["premium"] if leg["side"] == "BUY" else leg["premium"])
                * leg["quantity"]
                * multiplier
                for leg in legs
            ),
            Decimal("0"),
        )
        expected_price = abs(signed_premium) / (multiplier * aggregate_quantity)
        if (
            abs(fill.quantity - aggregate_quantity) > _EXECUTION_QUANTUM
            or abs(fill.price - expected_price) > _EXECUTION_QUANTUM
        ):
            msg = (
                f"Canonical multi-leg execution {fill.exec_id} does not match "
                "its persisted leg economics"
            )
            raise PaperLedgerRecoveryError(msg)

        underlying = _payload_symbol(fill)
        for leg in legs:
            settlement_notional = leg["premium"] * leg["quantity"] * multiplier
            account_notional = self._settlement_to_account(context, settlement_notional)
            self._apply_cash_flow(side=leg["side"], account_notional=account_notional)
            self._apply_position(
                symbol=_option_leg_symbol(underlying, leg),
                side=leg["side"],
                quantity=leg["quantity"],
                price=leg["premium"],
                account_notional=account_notional,
                context=context,
                quantity_unit="contracts",
                contract_multiplier=multiplier,
            )

    def apply(self, fill: CanonicalExecutionFill) -> None:
        if fill.order_method not in _REPLAYABLE_METHODS:
            msg = f"Canonical paper execution {fill.exec_id} has unsupported method"
            raise PaperLedgerRecoveryError(msg)
        context = _currency_context(fill)
        if context.account_currency != self.account_currency:
            msg = (
                f"Canonical paper execution {fill.exec_id} account currency "
                f"{context.account_currency} conflicts with {self.account_currency}"
            )
            raise PaperLedgerRecoveryError(msg)
        self._remember_order_quantity(fill)

        if fill.quantity > 0:
            if fill.order_method == "OPTIONS_SINGLE":
                self._apply_single_option_fill(fill, context)
            elif fill.order_method == "OPTIONS_STRATEGY":
                self._apply_multi_option_fill(fill, context)
            elif fill.order_method in {"PERP", "FUTURES"}:
                self._apply_linear_futures_fill(fill, context)
            else:
                self._apply_standard_fill(fill, context)

        account_fee = self._fee_in_account(fill, context)
        self.cash_balance -= account_fee
        self.realized_pnl -= account_fee
        if self.cash_balance < 0:
            msg = (
                f"Canonical paper execution {fill.exec_id} reconstructs a negative "
                "account cash balance"
            )
            raise PaperLedgerRecoveryError(msg)
        if fill.order_method in {"PERP", "FUTURES"}:
            available = self._current_available_balance()
            if available < 0:
                msg = (
                    f"Canonical paper execution {fill.exec_id} reconstructs "
                    "negative available futures margin"
                )
                raise PaperLedgerRecoveryError(msg)

    def _current_available_balance(self) -> Decimal:
        available = self.cash_balance
        for position in self.positions.values():
            multiplier = position.contract_multiplier or Decimal("1")
            gross_notional = (
                position.quantity
                * position.current_price
                * multiplier
                / position.account_to_settlement_rate
            )
            if position.leverage is None:
                available -= gross_notional * Decimal("0.1")
                continue
            unrealized = (
                gross_notional - position.account_cost_basis
                if position.side == "LONG"
                else position.account_cost_basis - gross_notional
            )
            available += unrealized - gross_notional / position.leverage
        return available

    def finish(self) -> tuple[PaperLedgerPosition, ...]:
        for order_id, requested in self.requested_quantity_by_order.items():
            filled = self.positive_quantity_by_order.get(order_id, Decimal("0"))
            if filled == 0:
                msg = f"Canonical paper order {order_id} has fees without a positive fill"
                raise PaperLedgerRecoveryError(msg)
            if filled - requested > _EXECUTION_QUANTUM:
                msg = f"Canonical paper order {order_id} exceeds its requested quantity"
                raise PaperLedgerRecoveryError(msg)
            if order_id in self.multi_leg_orders and abs(filled - requested) > _EXECUTION_QUANTUM:
                msg = f"Canonical multi-leg paper order {order_id} is not fully filled"
                raise PaperLedgerRecoveryError(msg)

        positions: list[PaperLedgerPosition] = []
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            multiplier = position.contract_multiplier or Decimal("1")
            settlement_mark = position.quantity * position.current_price * multiplier
            gross_notional = settlement_mark / position.account_to_settlement_rate
            if (
                position.quantity <= 0
                or position.entry_price <= 0
                or position.current_price <= 0
                or position.account_cost_basis <= 0
                or gross_notional <= 0
            ):
                msg = f"Canonical paper position {symbol} has invalid recovered economics"
                raise PaperLedgerRecoveryError(msg)
            positions.append(
                PaperLedgerPosition(
                    symbol=position.symbol,
                    side=position.side,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    current_price=position.current_price,
                    quantity_unit=position.quantity_unit,
                    contract_multiplier=position.contract_multiplier,
                    leverage=position.leverage,
                    contract_type=position.contract_type,
                    gross_notional=gross_notional,
                    notional_currency=self.account_currency,
                    account_cost_basis=position.account_cost_basis,
                )
            )
        return tuple(positions)


def _validate_routed_instruments(session: Any, fills: list[CanonicalExecutionFill]) -> None:
    validated_orders: set[int] = set()
    for fill in fills:
        if fill.order_id in validated_orders:
            continue
        symbol = _payload_symbol(fill)
        try:
            resolved = resolve_instrument(
                session,
                symbol,
                instrument_id=fill.instrument_id,
            )
        except InstrumentResolutionError as exc:
            msg = (
                f"Canonical paper order {fill.order_id} routed symbol {symbol!r} "
                f"does not identify instrument {fill.instrument_id}"
            )
            raise PaperLedgerRecoveryError(msg) from exc
        if resolved is None:
            msg = f"Canonical paper order {fill.order_id} has an unknown routed symbol"
            raise PaperLedgerRecoveryError(msg)
        validated_orders.add(fill.order_id)


def _validate_configured_money(
    value: Any,
    *,
    expected: Decimal,
    field: str,
) -> None:
    if value is None:
        return
    configured = _decimal(value, field=field)
    if configured != expected:
        msg = f"{field} conflicts with the persisted linked-account configuration"
        raise PaperLedgerRecoveryError(msg)


def recover_paper_ledger(
    session_factory: Any | None,
    position_store: Any | None,
    *,
    user_id: str,
    broker_code: str,
    environment: str,
    account_id: int,
    account_currency: str,
    configured_equity: float | None,
    configured_cash: float | None,
) -> PaperLedgerSeed:
    """Replay one exact paper account and repair its non-authoritative positions.

    ``execution_metrics`` is intentionally not read or synthesized.  It is an
    observability snapshot whose historical marks and drawdown state cannot be
    recovered exactly from a fill, while ``executions`` plus each order's
    persisted FX observation contain the complete paper cash/position economics.
    """

    normalized_user = str(user_id or "").strip()
    normalized_broker = str(broker_code or "").strip().lower()
    normalized_environment = str(environment or "").strip().lower()
    normalized_currency = _normalize_currency(account_currency, field="account currency")
    if not normalized_user:
        msg = "Paper ledger recovery requires user_id"
        raise PaperLedgerRecoveryError(msg)
    if not normalized_broker:
        msg = "Paper ledger recovery requires broker_code"
        raise PaperLedgerRecoveryError(msg)
    if normalized_environment != "paper":
        msg = "Canonical paper ledger recovery is valid only for the paper environment"
        raise PaperLedgerRecoveryError(msg)
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        msg = "Paper ledger recovery requires a positive broker_account_id"
        raise PaperLedgerRecoveryError(msg)

    if session_factory is None:
        initial_equity = _positive_decimal(
            configured_equity,
            field="paper_initial_equity",
        )
        initial_cash = _nonnegative_decimal(
            configured_cash,
            field="paper_initial_cash",
        )
        if initial_cash > initial_equity:
            msg = "paper_initial_cash cannot exceed paper_initial_equity"
            raise PaperLedgerRecoveryError(msg)
        return PaperLedgerSeed(
            initial_equity=initial_equity,
            cash_balance=initial_cash,
            realized_pnl=Decimal("0"),
            currency=normalized_currency,
            positions=(),
            execution_count=0,
            last_execution_id=None,
        )

    with session_factory() as session:
        account_row = (
            session.query(LinkedBrokerAccount)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(
                LinkedBrokerAccount.account_id == account_id,
                LinkedBrokerAccount.user_id == normalized_user,
                LinkedBrokerAccount.environment == "paper",
                LinkedBrokerAccount.status == "connected",
                func.lower(Broker.code) == normalized_broker,
            )
            .one_or_none()
        )
        if account_row is None:
            msg = (
                f"Paper broker account {account_id} is not a connected "
                f"{normalized_broker} account owned by user {normalized_user}"
            )
            raise PaperLedgerRecoveryError(msg)
        persisted_currency = _normalize_currency(
            account_row.base_ccy,
            field=f"paper broker account {account_id} base currency",
        )
        if persisted_currency != normalized_currency:
            msg = (
                f"Paper broker account {account_id} base currency "
                f"{persisted_currency} conflicts with routed {normalized_currency}"
            )
            raise PaperLedgerRecoveryError(msg)
        initial_equity = _positive_decimal(
            account_row.paper_initial_equity,
            field="paper_initial_equity",
        )
        initial_cash = _nonnegative_decimal(
            account_row.paper_initial_cash,
            field="paper_initial_cash",
        )
        if initial_cash > initial_equity:
            msg = "paper_initial_cash cannot exceed paper_initial_equity"
            raise PaperLedgerRecoveryError(msg)
        _validate_configured_money(
            configured_equity,
            expected=initial_equity,
            field="paper_initial_equity",
        )
        _validate_configured_money(
            configured_cash,
            expected=initial_cash,
            field="paper_initial_cash",
        )
        fills = load_canonical_executions(
            session,
            user_id=normalized_user,
            account_id=account_id,
            mode="paper",
            broker=normalized_broker,
        )
        _validate_routed_instruments(session, fills)

    reducer = _PaperLedgerReducer(
        initial_cash=initial_cash,
        account_currency=persisted_currency,
    )
    for fill in fills:
        reducer.apply(fill)
    positions = reducer.finish()
    seed = PaperLedgerSeed(
        initial_equity=initial_equity,
        cash_balance=reducer.cash_balance,
        realized_pnl=reducer.realized_pnl,
        currency=persisted_currency,
        positions=positions,
        execution_count=len(fills),
        last_execution_id=fills[-1].exec_id if fills else None,
    )

    if position_store is not None:
        position_store.sync_positions(
            user_id=normalized_user,
            broker_code=normalized_broker,
            account_id=account_id,
            environment="paper",
            positions=seed.broker_positions(),
            allow_empty_prune=True,
        )
    return seed


__all__ = [
    "PaperLedgerPosition",
    "PaperLedgerRecoveryError",
    "PaperLedgerSeed",
    "recover_paper_ledger",
]
