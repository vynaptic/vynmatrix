"""Owned exact fills from the canonical OMS execution ledger.

``CanonicalExecutionStore`` persists broker trade resources as append-only
``executions`` without deriving economics from cumulative order status.
Accounting readers use this module to join those exact fills to immutable
relational attribution on
``order_intents``/``orders``; ``pending_orders`` remains reconciliation state
and is never an accounting input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy import func

from lib_application.db.models import (
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
)
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CanonicalExecutionLedgerError(RuntimeError):
    """Canonical execution rows cannot be read with complete attribution."""


class CanonicalExecutionCurrencyError(CanonicalExecutionLedgerError):
    """Canonical execution economics lack mandatory currency attribution."""


@dataclass(frozen=True)
class CanonicalExecutionFill:
    """One exact venue fill with immutable ownership and economics."""

    exec_id: int
    order_id: int
    instrument_id: int
    user_id: str
    account_id: int
    strategy_id: str
    canonical_signal_id: int
    symbol: str
    side: str
    execution_mode: str
    broker_environment: str
    broker: str
    order_method: str
    order_payload: dict[str, Any]
    settlement_currency: str
    fill_ts: datetime
    quantity: Decimal
    price: Decimal
    fee_currency: str
    fee_amount: Decimal
    venue: str
    trade_id: str


@dataclass(frozen=True)
class AccountCurrencyExecution:
    """One exact fill normalized to its account reporting currency."""

    exec_id: int
    order_id: int
    canonical_signal_id: int
    account_id: int
    symbol: str
    side: str
    fill_ts: datetime
    quantity: Decimal
    price: Decimal
    commission: Decimal
    account_currency: str
    settlement_currency: str
    commission_currency: str
    quantity_unit: str
    contract_multiplier: Decimal | None


def _as_utc(value: object, *, exec_id: int) -> datetime:
    if not isinstance(value, datetime):
        msg = f"Canonical execution {exec_id} has no valid fill timestamp"
        raise CanonicalExecutionLedgerError(msg)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _map_row(row: Any) -> CanonicalExecutionFill:
    try:
        exec_id = int(row.exec_id)
        order_id = int(row.order_id)
        instrument_id = int(row.instrument_id)
        account_id = int(row.account_id)
        order_broker_id = int(row.order_broker_id)
        account_broker_id = int(row.account_broker_id)
        quantity = Decimal(str(row.quantity))
        price = Decimal(str(row.price))
        fee_amount = Decimal(str(row.fee_amount))
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        msg = "Canonical execution row has malformed numeric identity or economics"
        raise CanonicalExecutionLedgerError(msg) from exc

    if (
        not quantity.is_finite()
        or quantity <= 0
        or not price.is_finite()
        or price <= 0
        or not fee_amount.is_finite()
    ):
        msg = f"Canonical execution {exec_id} has invalid exact-fill economics"
        raise CanonicalExecutionLedgerError(msg)

    user_id = str(row.user_id or "").strip()
    strategy_id = str(row.strategy_id or "").strip()
    symbol = str(row.symbol or "").strip()
    side = str(row.side or "").strip().upper()
    execution_mode = str(row.execution_mode or "").strip().lower()
    broker_environment = str(row.broker_environment or "").strip().lower()
    account_environment = str(row.account_environment or "").strip().lower()
    broker = str(row.broker or "").strip().lower()
    order_method = str(row.order_method or "").strip().upper()
    raw_payload = row.order_payload
    settlement_currency = str(row.settlement_currency or "").strip().upper()
    instrument_settlement_currency = str(row.instrument_settlement_currency or "").strip().upper()
    fee_currency_raw = str(row.fee_currency or "").strip().upper()
    venue = str(row.venue or "").strip().lower()
    if (
        not user_id
        or instrument_id <= 0
        or account_id <= 0
        or not strategy_id
        or not symbol
        or side not in {"BUY", "SELL"}
        or not execution_mode
        or broker_environment not in {"paper", "live"}
        or account_environment != broker_environment
        or account_broker_id != order_broker_id
        or not broker
        or order_method
        not in {
            "SPOT",
            "MARGIN",
            "PERP",
            "FUTURES",
            "OPTIONS_SINGLE",
            "OPTIONS_STRATEGY",
        }
        or not settlement_currency
        or settlement_currency != instrument_settlement_currency
        or not fee_currency_raw
        or not venue
    ):
        msg = f"Canonical execution {exec_id} has incomplete relational attribution"
        raise CanonicalExecutionLedgerError(msg)
    if not isinstance(raw_payload, Mapping):
        msg = f"Canonical execution {exec_id} has no structured order payload"
        raise CanonicalExecutionLedgerError(msg)
    try:
        canonical_signal_id = int(row.canonical_signal_id)
    except (TypeError, ValueError) as exc:
        msg = f"Canonical execution {exec_id} has no canonical signal attribution"
        raise CanonicalExecutionLedgerError(msg) from exc
    if canonical_signal_id <= 0:
        msg = f"Canonical execution {exec_id} has invalid canonical signal attribution"
        raise CanonicalExecutionLedgerError(msg)
    if str(row.signal_strategy_id or "") != strategy_id:
        msg = f"Canonical execution {exec_id} has mismatched signal strategy attribution"
        raise CanonicalExecutionLedgerError(msg)
    trade_id = str(row.trade_id or "").strip()
    if not trade_id:
        msg = f"Canonical execution {exec_id} has no stable venue trade identity"
        raise CanonicalExecutionLedgerError(msg)

    return CanonicalExecutionFill(
        exec_id=exec_id,
        order_id=order_id,
        instrument_id=instrument_id,
        user_id=user_id,
        account_id=account_id,
        strategy_id=strategy_id,
        canonical_signal_id=canonical_signal_id,
        symbol=symbol,
        side=side,
        execution_mode=execution_mode,
        broker_environment=broker_environment,
        broker=broker,
        order_method=order_method,
        order_payload={str(key): value for key, value in raw_payload.items()},
        settlement_currency=settlement_currency,
        fill_ts=_as_utc(row.fill_ts, exec_id=exec_id),
        quantity=quantity,
        price=price,
        fee_currency=fee_currency_raw,
        fee_amount=fee_amount,
        venue=venue,
        trade_id=trade_id,
    )


def load_canonical_executions(
    session: Session,
    *,
    user_id: str,
    account_id: int,
    symbol: str | None = None,
    strategy_id: str | None = None,
    mode: str | None = None,
    broker: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[CanonicalExecutionFill]:
    """Load exact fills for one owned account partition.

    Every filter is applied to relational attribution.  A requested symbol is
    resolved through the canonical instrument catalogue so broker aliases do
    not split one FIFO partition.
    """
    query = (
        session.query(
            Execution.exec_id.label("exec_id"),
            Execution.order_id.label("order_id"),
            Execution.instr_id.label("instrument_id"),
            Execution.fill_ts.label("fill_ts"),
            Execution.qty.label("quantity"),
            Execution.price.label("price"),
            Execution.fee_ccy.label("fee_currency"),
            Execution.fee_amount.label("fee_amount"),
            Execution.venue.label("venue"),
            Execution.trade_id.label("trade_id"),
            Order.account_id.label("account_id"),
            Order.broker_id.label("order_broker_id"),
            Order.settlement_currency.label("settlement_currency"),
            Instrument.settlement_currency.label("instrument_settlement_currency"),
            OrderIntent.user_id.label("user_id"),
            OrderIntent.strategy_id.label("strategy_id"),
            OrderIntent.canonical_signal_id.label("canonical_signal_id"),
            OrderIntent.side.label("side"),
            OrderIntent.execution_mode.label("execution_mode"),
            OrderIntent.broker_environment.label("broker_environment"),
            OrderIntent.method.label("order_method"),
            OrderIntent.payload.label("order_payload"),
            LinkedBrokerAccount.broker_id.label("account_broker_id"),
            LinkedBrokerAccount.environment.label("account_environment"),
            CanonicalSignal.strategy_id.label("signal_strategy_id"),
            Instrument.canonical.label("symbol"),
            Broker.code.label("broker"),
        )
        .join(Order, Order.order_id == Execution.order_id)
        .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .join(
            LinkedBrokerAccount,
            (LinkedBrokerAccount.account_id == Order.account_id)
            & (LinkedBrokerAccount.user_id == OrderIntent.user_id),
        )
        .join(Broker, Broker.broker_id == Order.broker_id)
        .join(
            CanonicalSignal,
            CanonicalSignal.signal_id == OrderIntent.canonical_signal_id,
        )
        .outerjoin(Instrument, Instrument.instr_id == Execution.instr_id)
        .filter(
            OrderIntent.user_id == str(user_id),
            OrderIntent.account_id == account_id,
            Order.account_id == account_id,
        )
    )
    if symbol is not None:
        try:
            instrument = resolve_instrument(session, symbol)
        except InstrumentResolutionError as exc:
            msg = f"Cannot resolve canonical execution symbol {symbol!r}"
            raise CanonicalExecutionLedgerError(msg) from exc
        if instrument is None:
            msg = f"Cannot resolve canonical execution symbol {symbol!r}"
            raise CanonicalExecutionLedgerError(msg)
        query = query.filter(Execution.instr_id == int(instrument.instr_id))
    if strategy_id is not None:
        normalized_strategy = str(strategy_id).strip()
        if not normalized_strategy:
            msg = "Canonical execution strategy filter must be non-empty"
            raise CanonicalExecutionLedgerError(msg)
        query = query.filter(OrderIntent.strategy_id == normalized_strategy)
    if mode is not None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"paper", "live"}:
            msg = f"Canonical execution environment is unsupported: {mode!r}"
            raise CanonicalExecutionLedgerError(msg)
        query = query.filter(OrderIntent.broker_environment == normalized_mode)
    if broker is not None:
        normalized_broker = str(broker).strip().lower()
        if not normalized_broker:
            msg = "Canonical execution broker filter must be non-empty"
            raise CanonicalExecutionLedgerError(msg)
        query = query.filter(func.lower(Broker.code) == normalized_broker)
    if start_date is not None:
        query = query.filter(Execution.fill_ts >= start_date)
    if end_date is not None:
        query = query.filter(Execution.fill_ts <= end_date)

    rows = query.order_by(Execution.fill_ts.asc(), Execution.exec_id.asc()).all()
    return [_map_row(row) for row in rows]


def convert_canonical_executions(
    rows: list[CanonicalExecutionFill],
    *,
    account_currency: str,
    convert_money: Callable[..., Decimal],
) -> list[AccountCurrencyExecution]:
    """Convert canonical exact fills once for all accounting consumers."""
    from .fx_rates import normalize_currency  # noqa: PLC0415

    try:
        normalized_account_currency = normalize_currency(account_currency)
    except ValueError as exc:
        msg = f"Invalid account reporting currency: {account_currency!r}"
        raise CanonicalExecutionCurrencyError(msg) from exc

    converted: list[AccountCurrencyExecution] = []
    for row in rows:
        try:
            settlement_currency = normalize_currency(row.settlement_currency)
            commission_currency = normalize_currency(row.fee_currency)
        except ValueError as exc:
            msg = f"Canonical execution {row.exec_id} has invalid currency attribution"
            raise CanonicalExecutionCurrencyError(msg) from exc

        contract_multiplier: Decimal | None = None
        quantity_unit = "asset"
        unit_price = row.price
        if row.order_method in {"PERP", "FUTURES"}:
            metadata = row.order_payload.get("metadata")
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("contract_value_model") != "linear"
            ):
                msg = (
                    f"Canonical futures execution {row.exec_id} omitted "
                    "explicit linear contract economics"
                )
                raise CanonicalExecutionLedgerError(msg)
            try:
                contract_multiplier = Decimal(str(metadata["contract_multiplier"]))
                leverage = Decimal(str(metadata["leverage"]))
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                msg = f"Canonical futures execution {row.exec_id} has incomplete contract terms"
                raise CanonicalExecutionLedgerError(msg) from exc
            execution_mode = str(metadata.get("execution_mode") or "").strip().lower()
            contract_type = str(metadata.get("contract_type") or "").strip().lower()
            expected_mode = "perpetual" if row.order_method == "PERP" else "futures"
            expected_contract_type = "perpetual" if expected_mode == "perpetual" else "dated"
            if (
                not contract_multiplier.is_finite()
                or contract_multiplier <= 0
                or not leverage.is_finite()
                or leverage <= 0
                or execution_mode != expected_mode
                or contract_type != expected_contract_type
            ):
                msg = f"Canonical futures execution {row.exec_id} has invalid contract terms"
                raise CanonicalExecutionLedgerError(msg)
            quantity_unit = "contracts"
            unit_price *= contract_multiplier

        price = convert_money(
            unit_price,
            from_currency=settlement_currency,
            to_currency=normalized_account_currency,
            as_of=row.fill_ts,
        )
        commission = convert_money(
            row.fee_amount,
            from_currency=commission_currency,
            to_currency=normalized_account_currency,
            as_of=row.fill_ts,
        )
        if not price.is_finite() or price <= 0 or not commission.is_finite():
            msg = f"Canonical execution {row.exec_id} produced invalid converted economics"
            raise CanonicalExecutionCurrencyError(msg)

        execution = AccountCurrencyExecution(
            exec_id=row.exec_id,
            order_id=row.order_id,
            canonical_signal_id=row.canonical_signal_id,
            account_id=row.account_id,
            symbol=row.symbol,
            side=row.side.lower(),
            fill_ts=row.fill_ts,
            quantity=row.quantity,
            price=price,
            commission=commission,
            account_currency=normalized_account_currency,
            settlement_currency=settlement_currency,
            commission_currency=commission_currency,
            quantity_unit=quantity_unit,
            contract_multiplier=contract_multiplier,
        )
        converted.append(execution)
    return converted


__all__ = [
    "AccountCurrencyExecution",
    "CanonicalExecutionCurrencyError",
    "CanonicalExecutionFill",
    "CanonicalExecutionLedgerError",
    "convert_canonical_executions",
    "load_canonical_executions",
]
