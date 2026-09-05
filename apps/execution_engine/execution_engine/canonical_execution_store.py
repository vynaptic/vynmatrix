"""Canonical OMS order and exact-fill persistence.

The signal-driven execution path owns a distinct runtime ``OrderIntent`` DTO.
This store is the explicit conversion boundary into the existing OMS
``order_intents`` → ``orders`` → ``executions`` persistence model.  It requires
the caller to provide the linked broker account identity; account inference or
placeholder account creation is intentionally outside this boundary.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from lib_application.db.models import (
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    LinkedBrokerAccount,
    Strategy,
)
from lib_application.db.models import (
    Order as CanonicalOrder,
)
from lib_application.db.models import (
    OrderIntent as CanonicalOrderIntent,
)
from lib_common.config_validation import normalize_broker_code
from lib_data.market_data import normalize_product_symbol

from ._persistence_common import _resolve_session_factory
from .brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from .models import OptionsIntent, OrderIntent, client_order_id_for


class CanonicalOrderIdentityError(RuntimeError):
    """Raised when an order cannot be bound to an owned broker account."""


class CanonicalFillConflictError(RuntimeError):
    """Raised when exact broker economics contradict persisted fills."""


@dataclass(frozen=True)
class CanonicalOrderRef:
    """Database identifiers for a canonical intent and routed order."""

    intent_id: int
    order_id: int
    settlement_currency: str
    local_order_id: str = ""
    state: str = "new"
    broker_order_ref: str | None = None
    reused: bool = False


_ORDER_STATE: dict[OrderStatus, str] = {
    OrderStatus.PENDING: "new",
    OrderStatus.SUBMITTED: "new",
    OrderStatus.WORKING: "working",
    OrderStatus.PARTIALLY_FILLED: "partially_filled",
    OrderStatus.FILLED: "filled",
    OrderStatus.CANCELLED: "canceled",
    OrderStatus.REJECTED: "rejected",
    # The OMS order-state constraint has no separate expired state.  Expiry is
    # economically equivalent to terminal cancellation at this layer.
    OrderStatus.EXPIRED: "canceled",
}

_DURABLE_ORDER_STATUS: dict[str, OrderStatus] = {
    "new": OrderStatus.PENDING,
    "submission_unknown": OrderStatus.PENDING,
    "working": OrderStatus.WORKING,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}

_IDEMPOTENCY_KEY_MAX_LENGTH = 64
_EXECUTION_NUMERIC_QUANTUM = Decimal("0.00000001")
_EXECUTION_NUMERIC_MAX = Decimal("999999999999.99999999")


def _canonical_execution_numeric(
    value: Decimal,
    *,
    field_name: str,
    positive: bool,
) -> Decimal:
    """Return the exact representation accepted by ``NUMERIC(20, 8)``.

    PostgreSQL rounds ``NUMERIC`` ties away from zero. Canonicalizing before
    both insert and replay comparison prevents a driver/database scale round
    trip from looking like an economic mutation while retaining strict
    equality at every representable ledger quantum.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        msg = f"Canonical fill has invalid {field_name}"
        raise CanonicalFillConflictError(msg) from exc
    if not parsed.is_finite():
        msg = f"Canonical fill has invalid {field_name}"
        raise CanonicalFillConflictError(msg)
    try:
        canonical = parsed.quantize(
            _EXECUTION_NUMERIC_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation as exc:
        msg = f"Canonical fill {field_name} exceeds NUMERIC(20, 8)"
        raise CanonicalFillConflictError(msg) from exc
    if abs(canonical) > _EXECUTION_NUMERIC_MAX:
        msg = f"Canonical fill {field_name} exceeds NUMERIC(20, 8)"
        raise CanonicalFillConflictError(msg)
    if positive and canonical <= 0:
        msg = f"Canonical fill requires positive {field_name} at NUMERIC(20, 8) precision"
        raise CanonicalFillConflictError(msg)
    return canonical


def _economics_match(observed: Decimal, reported: Decimal) -> bool:
    """Compare exact fills with a float-backed status using a narrow tolerance."""
    tolerance = max(Decimal("0.00000001"), abs(reported) * Decimal("0.00000001"))
    return abs(observed - reported) <= tolerance


def _validated_account_and_broker(
    session: Any,
    *,
    user_id: str,
    account_id: int,
    broker_code: str,
    broker_environment: str | None,
) -> tuple[LinkedBrokerAccount, Broker]:
    account = session.get(LinkedBrokerAccount, account_id)
    if account is None:
        msg = f"Linked broker account {account_id} does not exist"
        raise CanonicalOrderIdentityError(msg)
    if str(account.user_id) != str(user_id):
        msg = f"Linked broker account {account_id} is not owned by user {user_id}"
        raise CanonicalOrderIdentityError(msg)
    if account.status != "connected":
        msg = f"Linked broker account {account_id} is not connected"
        raise CanonicalOrderIdentityError(msg)
    broker = (
        session.query(Broker)
        .filter(func.lower(Broker.code) == broker_code.strip().lower())
        .one_or_none()
    )
    if broker is None:
        msg = f"Unknown broker code: {broker_code}"
        raise CanonicalOrderIdentityError(msg)
    if int(account.broker_id) != int(broker.broker_id):
        msg = f"Linked broker account {account_id} does not belong to broker {broker_code}"
        raise CanonicalOrderIdentityError(msg)
    normalized_environment = str(broker_environment or "").strip().lower()
    if normalized_environment not in {"paper", "live"}:
        msg = "Canonical order persistence requires broker_environment=paper or live"
        raise CanonicalOrderIdentityError(msg)
    if str(account.environment).lower() != normalized_environment:
        msg = (
            f"Linked broker account {account_id} environment {account.environment} "
            f"does not match route {normalized_environment}"
        )
        raise CanonicalOrderIdentityError(msg)
    return account, broker


def validate_broker_account_identity(
    session_factory: Any,
    *,
    user_id: str,
    account_id: int,
    broker_code: str,
    broker_environment: str,
) -> None:
    """Validate the explicit route identity without inferring an account."""
    with session_factory() as session:
        _validated_account_and_broker(
            session,
            user_id=user_id,
            account_id=account_id,
            broker_code=broker_code,
            broker_environment=broker_environment,
        )


def _json_safe(value: Any) -> Any:
    converted: Any
    if isinstance(value, Decimal):
        converted = format(value, "f")
    elif isinstance(value, (datetime, date)):
        converted = value.isoformat()
    elif isinstance(value, Enum):
        converted = value.value
    elif is_dataclass(value) and not isinstance(value, type):
        converted = _json_safe(asdict(value))
    elif isinstance(value, dict):
        converted = {str(key): _json_safe(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        converted = [_json_safe(item) for item in value]
    else:
        converted = value
    return converted


def _intent_method(intent: OrderIntent | OptionsIntent, execution_mode: str) -> str:
    if isinstance(intent, OptionsIntent):
        return "OPTIONS_STRATEGY" if len(intent.legs or []) > 1 else "OPTIONS_SINGLE"
    normalized = execution_mode.strip().lower()
    return {
        "spot": "SPOT",
        "paper": "SPOT",
        "margin": "MARGIN",
        "perpetual": "PERP",
        "perp": "PERP",
        "futures": "FUTURES",
    }.get(normalized, "SPOT")


def _intent_payload(
    intent: OrderIntent | OptionsIntent,
    *,
    local_order_id: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "local_order_id": local_order_id,
        "broker_code": intent.broker_code,
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": str(intent.quantity),
        "order_type": intent.order_type,
        "time_in_force": intent.time_in_force,
        "limit_price": None if intent.limit_price is None else str(intent.limit_price),
        "stop_price": None if intent.stop_price is None else str(intent.stop_price),
        "metadata": _json_safe(intent.metadata or {}),
        "currency_context": _json_safe(intent.currency_context),
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    if isinstance(intent, OptionsIntent):
        payload.update(
            {
                "legs": _json_safe(intent.legs or []),
                "spread_type": intent.spread_type,
                "underlying_price": intent.underlying_price,
                "max_profit": intent.max_profit,
                "max_loss": intent.max_loss,
                "breakeven": intent.breakeven,
            }
        )
    return {str(key): _json_safe(value) for key, value in payload.items()}


def _routed_symbol(payload: object, *, order_id: int) -> str:
    """Return the exact venue symbol captured at the routing boundary."""
    if not isinstance(payload, dict):
        msg = f"Canonical order {order_id} has no structured intent payload"
        raise CanonicalFillConflictError(msg)
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        msg = f"Canonical order {order_id} has malformed intent metadata"
        raise CanonicalFillConflictError(msg)
    symbol = (metadata or {}).get("broker_symbol") or payload.get("symbol")
    normalized = str(symbol or "").strip()
    if not normalized:
        msg = f"Canonical order {order_id} has no routed broker symbol"
        raise CanonicalFillConflictError(msg)
    return normalized


def _normalized_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        msg = "Canonical order idempotency_key must contain 1-64 characters"
        raise CanonicalOrderIdentityError(msg)
    return normalized


def _identity_payload(payload: object) -> dict[str, Any]:
    """Return immutable order economics, excluding local recovery metadata."""
    if not isinstance(payload, dict):
        return {}
    identity = dict(payload)
    identity.pop("local_order_id", None)
    identity.pop("pre_submission_abort", None)
    return identity


class CanonicalExecutionStore:
    """Persist owned OMS orders and exact broker executions."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        session_factory: Any | None = None,
    ) -> None:
        self._session_factory = _resolve_session_factory(
            database_url=database_url,
            session_factory=session_factory,
        )

    def resolve_settlement_currency(self, instrument_id: int | None) -> str:
        """Read the immutable settlement currency from the instrument catalogue."""
        if instrument_id is None or instrument_id <= 0:
            msg = "Canonical execution requires a persisted instrument_id"
            raise CanonicalOrderIdentityError(msg)
        with self._session_factory() as session:
            instrument = session.get(Instrument, instrument_id)
            if instrument is None:
                msg = f"Instrument {instrument_id} does not exist"
                raise CanonicalOrderIdentityError(msg)
            currency = str(instrument.settlement_currency or "").strip().upper()
            if not currency or not currency.isalnum():
                msg = f"Instrument {instrument_id} has invalid settlement currency"
                raise CanonicalOrderIdentityError(msg)
            return currency

    @staticmethod
    def _existing_order_ref(
        session: Any,
        *,
        canonical_intent: CanonicalOrderIntent,
        local_order_id: str,
        account_id: int,
        broker_id: int,
        strategy_id: str,
        canonical_signal_id: int,
        side: str,
        execution_mode: str,
        broker_environment: str,
        method: str,
        payload: dict[str, Any],
        settlement_currency: str,
    ) -> CanonicalOrderRef:
        """Validate and return the one canonical attempt for an idempotency key."""
        order = (
            session.query(CanonicalOrder)
            .filter(CanonicalOrder.intent_id == canonical_intent.intent_id)
            .one_or_none()
        )
        if order is None:
            msg = (
                f"Canonical intent {canonical_intent.intent_id} has no routed order; "
                "idempotent replay is unsafe"
            )
            raise CanonicalOrderIdentityError(msg)
        expected_columns = (
            int(account_id),
            str(strategy_id),
            int(canonical_signal_id),
            str(side),
            str(execution_mode),
            str(broker_environment),
            str(method),
        )
        persisted_columns = (
            int(canonical_intent.account_id),
            str(canonical_intent.strategy_id),
            int(canonical_intent.canonical_signal_id),
            str(canonical_intent.side),
            str(canonical_intent.execution_mode),
            str(canonical_intent.broker_environment),
            str(canonical_intent.method),
        )
        order_identity = (
            int(order.account_id),
            int(order.broker_id),
            str(order.settlement_currency or "").strip().upper(),
        )
        expected_order_identity = (
            int(account_id),
            int(broker_id),
            settlement_currency,
        )
        if (
            persisted_columns != expected_columns
            or order_identity != expected_order_identity
            or _identity_payload(canonical_intent.payload) != _identity_payload(payload)
        ):
            msg = (
                "Canonical order idempotency collision: the existing attempt has "
                "different ownership, routing, or economics"
            )
            raise CanonicalOrderIdentityError(msg)

        persisted_payload = (
            dict(canonical_intent.payload) if isinstance(canonical_intent.payload, dict) else {}
        )
        persisted_local_order_id = str(persisted_payload.get("local_order_id") or "").strip()
        if not persisted_local_order_id:
            msg = f"Canonical intent {canonical_intent.intent_id} has no local recovery identity"
            raise CanonicalOrderIdentityError(msg)

        # A pre-submission abort proves that no broker I/O occurred. It is safe
        # to reuse the same canonical identity for a later persistence retry.
        pre_submission_abort = persisted_payload.get("pre_submission_abort")
        has_execution = (
            session.query(Execution.exec_id).filter(Execution.order_id == order.order_id).first()
            is not None
        )
        if (
            canonical_intent.status == "rejected"
            and order.state == "rejected"
            and isinstance(pre_submission_abort, dict)
            and not order.broker_order_ref
            and not has_execution
        ):
            revived_payload = dict(payload)
            revived_payload["local_order_id"] = local_order_id
            canonical_intent.payload = revived_payload
            canonical_intent.status = "routed"
            order.state = "new"
            order.last_update = datetime.now(tz=UTC)
            session.commit()
            return CanonicalOrderRef(
                intent_id=int(canonical_intent.intent_id),
                order_id=int(order.order_id),
                settlement_currency=settlement_currency,
                local_order_id=local_order_id,
                state="new",
                broker_order_ref=None,
                reused=False,
            )

        return CanonicalOrderRef(
            intent_id=int(canonical_intent.intent_id),
            order_id=int(order.order_id),
            settlement_currency=settlement_currency,
            local_order_id=persisted_local_order_id,
            state=str(order.state),
            broker_order_ref=order.broker_order_ref,
            reused=True,
        )

    def create_order(  # noqa: PLR0912, PLR0915
        self,
        *,
        local_order_id: str,
        user_id: str,
        account_id: int | None,
        broker_code: str,
        execution_mode: str,
        strategy_id: str,
        canonical_signal_id: int,
        instr_id: int | None,
        intent: OrderIntent | OptionsIntent,
        settlement_currency: str,
        idempotency_key: str | None = None,
        broker_environment: str | None = None,
    ) -> CanonicalOrderRef:
        """Create the canonical intent/order pair before broker submission.

        The explicit account is validated against both the user and broker.  A
        missing or mismatched identity raises before any OMS row is written.
        """
        if account_id is None or account_id <= 0:
            msg = "Canonical order persistence requires an explicit linked broker account_id"
            raise CanonicalOrderIdentityError(msg)
        normalized_broker_code = normalize_broker_code(broker_code, default="")
        normalized_intent_broker = normalize_broker_code(intent.broker_code, default="")
        if not normalized_broker_code or normalized_intent_broker != normalized_broker_code:
            msg = (
                f"Canonical order route broker {broker_code!r} does not match "
                f"intent broker {intent.broker_code!r}"
            )
            raise CanonicalOrderIdentityError(msg)
        normalized_settlement = str(settlement_currency or "").strip().upper()
        if not normalized_settlement or not normalized_settlement.isalnum():
            msg = "Canonical order persistence requires settlement_currency"
            raise CanonicalOrderIdentityError(msg)
        normalized_strategy = str(strategy_id or "").strip()
        if not normalized_strategy:
            msg = "Canonical order persistence requires strategy_id"
            raise CanonicalOrderIdentityError(msg)
        normalized_side = str(intent.side or "").strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            msg = f"Canonical order persistence received invalid side: {intent.side!r}"
            raise CanonicalOrderIdentityError(msg)
        normalized_mode = str(execution_mode or "").strip().lower()
        if not normalized_mode:
            msg = "Canonical order persistence requires execution_mode"
            raise CanonicalOrderIdentityError(msg)
        normalized_environment = str(broker_environment or "").strip().lower()
        normalized_idempotency_key = _normalized_idempotency_key(idempotency_key)
        if (
            isinstance(canonical_signal_id, bool)
            or not isinstance(canonical_signal_id, int)
            or canonical_signal_id <= 0
        ):
            msg = "Canonical order persistence requires a positive canonical_signal_id"
            raise CanonicalOrderIdentityError(msg)
        if (
            instr_id is None
            or isinstance(instr_id, bool)
            or not isinstance(instr_id, int)
            or instr_id <= 0
        ):
            msg = "Canonical order persistence requires a positive instrument_id"
            raise CanonicalOrderIdentityError(msg)
        with self._session_factory() as session:
            _account, broker = _validated_account_and_broker(
                session,
                user_id=user_id,
                account_id=account_id,
                broker_code=normalized_broker_code,
                broker_environment=broker_environment,
            )
            strategy = session.get(Strategy, normalized_strategy)
            if strategy is None:
                msg = f"Canonical order strategy {normalized_strategy} does not exist"
                raise CanonicalOrderIdentityError(msg)
            canonical_signal = session.get(CanonicalSignal, canonical_signal_id)
            if canonical_signal is None:
                msg = f"Canonical signal {canonical_signal_id} does not exist"
                raise CanonicalOrderIdentityError(msg)
            if str(canonical_signal.strategy_id) != normalized_strategy:
                msg = (
                    f"Canonical signal {canonical_signal_id} belongs to strategy "
                    f"{canonical_signal.strategy_id}, not {normalized_strategy}"
                )
                raise CanonicalOrderIdentityError(msg)
            if int(canonical_signal.instr_id) != instr_id:
                msg = (
                    f"Canonical signal {canonical_signal_id} belongs to instrument "
                    f"{canonical_signal.instr_id}, not {instr_id}"
                )
                raise CanonicalOrderIdentityError(msg)
            instrument = session.get(Instrument, instr_id)
            if instrument is None:
                msg = f"Canonical order instrument {instr_id} does not exist"
                raise CanonicalOrderIdentityError(msg)
            instrument_settlement = str(instrument.settlement_currency or "").strip().upper()
            if instrument_settlement != normalized_settlement:
                msg = (
                    f"Canonical order instrument {instr_id} settles in "
                    f"{instrument_settlement}, not {normalized_settlement}"
                )
                raise CanonicalOrderIdentityError(msg)

            method = _intent_method(intent, execution_mode)
            payload = _intent_payload(
                intent,
                local_order_id=local_order_id,
                idempotency_key=normalized_idempotency_key,
            )
            client_order_id = (
                client_order_id_for(dict(intent.metadata or {})) or str(local_order_id).strip()
            )
            if not client_order_id:
                msg = "Canonical order persistence requires client_order_id"
                raise CanonicalOrderIdentityError(msg)
            if normalized_idempotency_key is not None:
                existing = (
                    session.query(CanonicalOrderIntent)
                    .filter(
                        CanonicalOrderIntent.account_id == account_id,
                        CanonicalOrderIntent.idempotency_key == normalized_idempotency_key,
                    )
                    .one_or_none()
                )
                if existing is not None:
                    return self._existing_order_ref(
                        session,
                        canonical_intent=existing,
                        local_order_id=local_order_id,
                        account_id=account_id,
                        broker_id=int(broker.broker_id),
                        strategy_id=normalized_strategy,
                        canonical_signal_id=canonical_signal_id,
                        side=normalized_side,
                        execution_mode=normalized_mode,
                        broker_environment=normalized_environment,
                        method=method,
                        payload=payload,
                        settlement_currency=normalized_settlement,
                    )

            canonical_intent = CanonicalOrderIntent(
                user_id=user_id,
                account_id=account_id,
                strategy_id=normalized_strategy,
                canonical_signal_id=canonical_signal_id,
                idempotency_key=normalized_idempotency_key,
                side=normalized_side,
                execution_mode=normalized_mode,
                broker_environment=normalized_environment,
                method=method,
                payload=payload,
                status="created",
            )
            try:
                session.add(canonical_intent)
                session.flush()
                order = CanonicalOrder(
                    intent_id=canonical_intent.intent_id,
                    broker_id=broker.broker_id,
                    account_id=account_id,
                    settlement_currency=normalized_settlement,
                    client_order_id=client_order_id,
                    state="new",
                )
                session.add(order)
                session.flush()
                canonical_intent.status = "routed"
                ref = CanonicalOrderRef(
                    intent_id=int(canonical_intent.intent_id),
                    order_id=int(order.order_id),
                    settlement_currency=normalized_settlement,
                    local_order_id=local_order_id,
                )
                session.commit()
                return ref  # noqa: TRY300
            except IntegrityError:
                session.rollback()
                if normalized_idempotency_key is None:
                    raise
                existing = (
                    session.query(CanonicalOrderIntent)
                    .filter(
                        CanonicalOrderIntent.account_id == account_id,
                        CanonicalOrderIntent.idempotency_key == normalized_idempotency_key,
                    )
                    .one_or_none()
                )
                if existing is None:
                    raise
                return self._existing_order_ref(
                    session,
                    canonical_intent=existing,
                    local_order_id=local_order_id,
                    account_id=account_id,
                    broker_id=int(broker.broker_id),
                    strategy_id=normalized_strategy,
                    canonical_signal_id=canonical_signal_id,
                    side=normalized_side,
                    execution_mode=normalized_mode,
                    broker_environment=normalized_environment,
                    method=method,
                    payload=payload,
                    settlement_currency=normalized_settlement,
                )

    def read_durable_result(
        self,
        order_id: int,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]] | None:
        """Reconstruct broker status and exact fills from the canonical ledger.

        This is the authoritative recovery read used after a process crash or
        idempotent dispatch replay. It never infers a fill from an order status:
        filled and partially-filled states must have persisted executions.
        """
        with self._session_factory() as session:
            order = session.get(CanonicalOrder, order_id)
            if order is None:
                return None
            intent = session.get(CanonicalOrderIntent, order.intent_id)
            if intent is None:
                msg = f"Canonical order {order_id} has no persisted order intent"
                raise CanonicalFillConflictError(msg)
            try:
                status = _DURABLE_ORDER_STATUS[str(order.state)]
            except KeyError as exc:
                msg = f"Canonical order {order_id} has unknown state {order.state!r}"
                raise CanonicalFillConflictError(msg) from exc

            rows = (
                session.query(Execution)
                .filter(Execution.order_id == order_id)
                .order_by(Execution.fill_ts.asc(), Execution.exec_id.asc())
                .all()
            )
            if status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED} and not rows:
                msg = f"Canonical order {order_id} is {order.state} without exact executions"
                raise CanonicalFillConflictError(msg)
            broker_order_ref = str(order.broker_order_ref or "").strip()
            if rows and not broker_order_ref:
                msg = f"Canonical order {order_id} has executions without a broker reference"
                raise CanonicalFillConflictError(msg)

            symbol = _routed_symbol(intent.payload, order_id=order_id)
            side = str(intent.side or "").strip().lower()
            fills: list[BrokerFill] = []
            for row in rows:
                fill_ts = row.fill_ts
                if fill_ts.tzinfo is None:
                    fill_ts = fill_ts.replace(tzinfo=UTC)
                else:
                    fill_ts = fill_ts.astimezone(UTC)
                provenance_values = (
                    row.source_price_id,
                    row.source_content_revision,
                    row.trigger_policy_version,
                )
                has_provenance = any(value is not None for value in provenance_values)
                if has_provenance and any(value is None for value in provenance_values):
                    msg = f"Canonical execution {row.exec_id} has incomplete source provenance"
                    raise CanonicalFillConflictError(msg)
                provenance = (
                    {
                        "source_price_id": int(row.source_price_id),
                        "source_content_revision": int(row.source_content_revision),
                        "trigger_policy_version": str(row.trigger_policy_version),
                    }
                    if has_provenance
                    else None
                )
                fills.append(
                    BrokerFill(
                        trade_id=str(row.trade_id),
                        order_id=broker_order_ref,
                        symbol=symbol,
                        side=side,
                        quantity=Decimal(str(row.qty)),
                        price=Decimal(str(row.price)),
                        commission=Decimal(str(row.fee_amount)),
                        commission_currency=str(row.fee_ccy),
                        timestamp=fill_ts,
                        venue=str(row.venue),
                        raw_response=provenance,
                    )
                )

            total_quantity = sum((fill.quantity for fill in fills), Decimal("0"))
            average_price = (
                sum((fill.quantity * fill.price for fill in fills), Decimal("0")) / total_quantity
                if total_quantity
                else Decimal("0")
            )
            fee_currencies = {fill.commission_currency for fill in fills}
            commission_currency = next(iter(fee_currencies)) if len(fee_currencies) == 1 else None
            commission = (
                sum((fill.commission for fill in fills), Decimal("0"))
                if commission_currency
                else Decimal("0")
            )
            result = BrokerOrderResult(
                success=status != OrderStatus.REJECTED,
                order_id=broker_order_ref or None,
                status=status,
                filled_quantity=float(total_quantity),
                filled_price=float(average_price) if total_quantity else None,
                commission=commission,
                commission_currency=commission_currency,
                timestamp=(fills[-1].timestamp if fills else datetime.now(tz=UTC)),
                error_message=(
                    "Canonical order was rejected before or by broker"
                    if status == OrderStatus.REJECTED
                    else None
                ),
                raw_response={
                    "source": "canonical_execution_ledger",
                    "canonical_order_id": order_id,
                    "exact_fill_count": len(fills),
                },
            )
            return result, fills

    def abort_pre_submission(
        self,
        *,
        order_id: int,
        reason: str,
        code: str,
    ) -> None:
        """Close an OMS attempt after persistence fails before broker I/O.

        The audit row is retained, but it cannot remain indistinguishable from
        a routed order. This transition is valid only before a broker reference
        or exact execution exists.
        """
        normalized_reason = str(reason or "").strip()
        normalized_code = str(code or "").strip()
        if not normalized_reason or not normalized_code:
            msg = "Pre-submission abort requires a reason and code"
            raise CanonicalOrderIdentityError(msg)
        with self._session_factory() as session:
            order = session.get(CanonicalOrder, order_id)
            if order is None:
                msg = f"Canonical order {order_id} does not exist"
                raise CanonicalOrderIdentityError(msg)
            intent = session.get(CanonicalOrderIntent, order.intent_id)
            if intent is None:
                msg = f"Canonical order {order_id} has no persisted order intent"
                raise CanonicalOrderIdentityError(msg)
            has_execution = (
                session.query(Execution.exec_id).filter(Execution.order_id == order_id).first()
                is not None
            )
            if order.broker_order_ref or has_execution or order.state != "new":
                msg = (
                    f"Canonical order {order_id} cannot be aborted before submission "
                    "after broker activity"
                )
                raise CanonicalOrderIdentityError(msg)
            payload = dict(intent.payload) if isinstance(intent.payload, dict) else {}
            payload["pre_submission_abort"] = {
                "code": normalized_code,
                "reason": normalized_reason,
                "recorded_at": datetime.now(tz=UTC).isoformat(),
            }
            intent.payload = payload
            intent.status = "rejected"
            order.state = "rejected"
            order.last_update = datetime.now(tz=UTC)
            session.commit()

    def mark_submission_unknown(
        self,
        *,
        order_id: int,
        reason: str,
    ) -> None:
        """Record that broker acceptance is unknown after network dispatch."""
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            msg = "Unknown submission state requires a reason"
            raise CanonicalOrderIdentityError(msg)
        with self._session_factory() as session:
            order = session.get(CanonicalOrder, order_id)
            if order is None:
                msg = f"Canonical order {order_id} does not exist"
                raise CanonicalOrderIdentityError(msg)
            intent = session.get(CanonicalOrderIntent, order.intent_id)
            if intent is None:
                msg = f"Canonical order {order_id} has no persisted order intent"
                raise CanonicalOrderIdentityError(msg)
            if order.state not in {"new", "submission_unknown"}:
                msg = (
                    f"Canonical order {order_id} cannot become submission_unknown "
                    f"from state {order.state!r}"
                )
                raise CanonicalOrderIdentityError(msg)
            payload = dict(intent.payload) if isinstance(intent.payload, dict) else {}
            payload["submission_unknown"] = {
                "reason": normalized_reason,
                "recorded_at": datetime.now(tz=UTC).isoformat(),
            }
            intent.payload = payload
            order.state = "submission_unknown"
            order.last_update = datetime.now(tz=UTC)
            session.commit()

    def apply_broker_result(  # noqa: PLR0912, PLR0915
        self,
        *,
        order_id: int,
        result: BrokerOrderResult,
        fills: list[BrokerFill],
        instr_id: int | None,
        _session: Any | None = None,
    ) -> list[int]:
        """Atomically update order state and append exact venue fill rows.

        ``BrokerOrderResult`` remains an order-status observation only.  It
        never manufactures ledger economics.  Every execution comes from
        ``fills`` and carries the broker's stable trade ID, actual timestamp,
        exact quantity/price, and explicit fee amount/currency.
        """
        with nullcontext(_session) if _session is not None else self._session_factory() as session:
            order = session.get(CanonicalOrder, order_id)
            if order is None:
                msg = f"Canonical order {order_id} does not exist"
                raise CanonicalFillConflictError(msg)

            target_state = _ORDER_STATE[result.status]
            has_persisted_execution = (
                session.query(Execution.exec_id).filter(Execution.order_id == order_id).first()
                is not None
            )
            if order.state == "filled" and target_state != "filled":
                if not fills:
                    msg = (
                        f"Canonical order {order_id} is already filled and cannot "
                        f"transition to {target_state}"
                    )
                    raise CanonicalFillConflictError(msg)
                # A stale partial observation may replay an already persisted
                # trade. Validate its exact economics below without regressing
                # the terminal order state.
                target_state = "filled"
            if (
                has_persisted_execution
                and not fills
                and target_state not in {"partially_filled", "filled"}
            ):
                msg = (
                    f"Canonical order {order_id} has durable executions and cannot "
                    f"transition to {target_state} without exact fills"
                )
                raise CanonicalFillConflictError(msg)
            order.state = target_state
            order.last_update = datetime.now(tz=UTC)
            if result.order_id:
                if order.broker_order_ref and order.broker_order_ref != result.order_id:
                    msg = (
                        f"Canonical order {order_id} changed broker reference from "
                        f"{order.broker_order_ref} to {result.order_id}"
                    )
                    raise CanonicalFillConflictError(msg)
                order.broker_order_ref = result.order_id

            if result.has_fill and not fills:
                msg = (
                    f"Broker reported filled quantity for canonical order {order_id} "
                    "without any exact fills"
                )
                raise CanonicalFillConflictError(msg)
            if fills and result.has_fill:
                total_quantity = sum((fill.quantity for fill in fills), Decimal("0"))
                reported_quantity = Decimal(str(result.filled_quantity))
                if not _economics_match(total_quantity, reported_quantity):
                    msg = (
                        f"Exact fills for canonical order {order_id} total "
                        f"{total_quantity}, but broker status reports "
                        f"{reported_quantity}; refusing an incomplete fill set"
                    )
                    raise CanonicalFillConflictError(msg)
                weighted_price = (
                    sum(
                        (fill.quantity * fill.price for fill in fills),
                        Decimal("0"),
                    )
                    / total_quantity
                )
                reported_price = Decimal(str(result.average_price))
                if not _economics_match(weighted_price, reported_price):
                    msg = (
                        f"Exact fills for canonical order {order_id} have weighted "
                        f"price {weighted_price}, but broker status reports "
                        f"{reported_price}; refusing contradictory economics"
                    )
                    raise CanonicalFillConflictError(msg)
            if not fills:
                if _session is None:
                    session.commit()
                return []
            if (
                instr_id is None
                or isinstance(instr_id, bool)
                or not isinstance(instr_id, int)
                or instr_id <= 0
            ):
                msg = f"Canonical fill for order {order_id} requires an instrument_id"
                raise CanonicalFillConflictError(msg)
            intent = session.get(CanonicalOrderIntent, order.intent_id)
            if intent is None:
                msg = f"Canonical order {order_id} has no persisted order intent"
                raise CanonicalFillConflictError(msg)
            expected_side = str(intent.side or "").strip().lower()
            expected_symbol = _routed_symbol(intent.payload, order_id=order_id)
            canonical_signal = session.get(CanonicalSignal, intent.canonical_signal_id)
            if canonical_signal is None:
                msg = (
                    f"Canonical order {order_id} references missing signal "
                    f"{intent.canonical_signal_id}"
                )
                raise CanonicalFillConflictError(msg)
            if int(canonical_signal.instr_id) != instr_id:
                msg = (
                    f"Canonical fill instrument {instr_id} does not match signal "
                    f"{canonical_signal.signal_id} instrument {canonical_signal.instr_id}"
                )
                raise CanonicalFillConflictError(msg)
            instrument = session.get(Instrument, instr_id)
            if instrument is None:
                msg = f"Canonical fill instrument {instr_id} does not exist"
                raise CanonicalFillConflictError(msg)
            if (
                str(instrument.settlement_currency).strip().upper()
                != str(order.settlement_currency).strip().upper()
            ):
                msg = (
                    f"Canonical fill instrument {instr_id} settles in "
                    f"{instrument.settlement_currency}, not {order.settlement_currency}"
                )
                raise CanonicalFillConflictError(msg)

            inserted_ids: list[int] = []
            seen_trade_ids: set[str] = set()
            for fill in fills:
                provenance = dict(fill.raw_response) if isinstance(fill.raw_response, dict) else {}
                source_price_id = provenance.get("source_price_id")
                source_revision = provenance.get("source_content_revision")
                trigger_policy = provenance.get("trigger_policy_version")
                has_any_provenance = any(
                    value is not None
                    for value in (source_price_id, source_revision, trigger_policy)
                )
                if has_any_provenance and (
                    not isinstance(source_price_id, int)
                    or isinstance(source_price_id, bool)
                    or source_price_id <= 0
                    or not isinstance(source_revision, int)
                    or isinstance(source_revision, bool)
                    or source_revision <= 0
                    or not str(trigger_policy or "").strip()
                ):
                    msg = f"Broker fill {fill.trade_id!r} has incomplete source-bar provenance"
                    raise CanonicalFillConflictError(msg)
                if fill.side != expected_side:
                    msg = (
                        f"Broker fill {fill.trade_id!r} side {fill.side!r} does not "
                        f"match canonical order {order_id} side {expected_side!r}"
                    )
                    raise CanonicalFillConflictError(msg)
                if normalize_product_symbol(fill.symbol) != normalize_product_symbol(
                    expected_symbol
                ):
                    msg = (
                        f"Broker fill {fill.trade_id!r} symbol {fill.symbol!r} does not "
                        f"match canonical order {order_id} routed symbol {expected_symbol!r}"
                    )
                    raise CanonicalFillConflictError(msg)
                if fill.trade_id in seen_trade_ids:
                    msg = (
                        f"Broker returned duplicate trade ID {fill.trade_id!r} "
                        f"for canonical order {order_id}"
                    )
                    raise CanonicalFillConflictError(msg)
                seen_trade_ids.add(fill.trade_id)

                broker_ref = str(order.broker_order_ref or "").strip()
                if not broker_ref:
                    order.broker_order_ref = fill.order_id
                    broker_ref = fill.order_id
                if fill.order_id != broker_ref:
                    msg = (
                        f"Broker fill {fill.trade_id!r} belongs to order "
                        f"{fill.order_id!r}, not {broker_ref!r}"
                    )
                    raise CanonicalFillConflictError(msg)

                fill_ts = fill.timestamp.astimezone(UTC).replace(tzinfo=None)
                fill_quantity = _canonical_execution_numeric(
                    fill.quantity,
                    field_name="quantity",
                    positive=True,
                )
                fill_price = _canonical_execution_numeric(
                    fill.price,
                    field_name="price",
                    positive=True,
                )
                fill_commission = _canonical_execution_numeric(
                    fill.commission,
                    field_name="commission",
                    positive=False,
                )
                existing = (
                    session.query(Execution)
                    .filter(
                        Execution.order_id == order_id,
                        Execution.trade_id == fill.trade_id,
                    )
                    .one_or_none()
                )
                if existing is not None:
                    persisted = (
                        int(existing.instr_id),
                        existing.fill_ts,
                        Decimal(str(existing.qty)),
                        Decimal(str(existing.price)),
                        str(existing.fee_ccy or "").upper(),
                        Decimal(str(existing.fee_amount)),
                        str(existing.venue or ""),
                        existing.source_price_id,
                        existing.source_content_revision,
                        existing.trigger_policy_version,
                    )
                    observed = (
                        instr_id,
                        fill_ts,
                        fill_quantity,
                        fill_price,
                        fill.commission_currency,
                        fill_commission,
                        fill.venue,
                        source_price_id,
                        source_revision,
                        trigger_policy,
                    )
                    if persisted != observed:
                        msg = (
                            f"Broker fill {fill.trade_id!r} changed after persistence "
                            f"for canonical order {order_id}"
                        )
                        raise CanonicalFillConflictError(msg)
                    continue

                execution = Execution(
                    order_id=order_id,
                    instr_id=instr_id,
                    fill_ts=fill_ts,
                    qty=fill_quantity,
                    price=fill_price,
                    fee_ccy=fill.commission_currency,
                    fee_amount=fill_commission,
                    venue=fill.venue,
                    trade_id=fill.trade_id,
                    source_price_id=source_price_id,
                    source_content_revision=source_revision,
                    trigger_policy_version=trigger_policy,
                )
                session.add(execution)
                session.flush()
                inserted_ids.append(int(execution.exec_id))
            if _session is None:
                session.commit()
            return inserted_ids

    def apply_broker_result_on_session(
        self,
        session: Any,
        *,
        order_id: int,
        result: BrokerOrderResult,
        fills: list[BrokerFill],
        instr_id: int | None,
    ) -> list[int]:
        """Apply an exact fill inside a caller-owned database transaction."""
        return self.apply_broker_result(
            order_id=order_id,
            result=result,
            fills=fills,
            instr_id=instr_id,
            _session=session,
        )


__all__ = [
    "CanonicalExecutionStore",
    "CanonicalFillConflictError",
    "CanonicalOrderIdentityError",
    "CanonicalOrderRef",
    "validate_broker_account_identity",
]
