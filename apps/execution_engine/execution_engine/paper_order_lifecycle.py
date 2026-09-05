"""Durable committed-candle lifecycle for local-paper resting orders."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.models import (
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    PendingOrder,
)
from lib_application.db.models import (
    OrderIntent as CanonicalOrderIntent,
)
from lib_common.logging import get_logger
from lib_common.metrics import counter

from .account_execution_serializer import (
    AccountExecutionBusyError,
    AccountExecutionSerializer,
)
from .brokers.paper import PaperBroker, PaperFillPlan, ReduceOnlyPositionUnavailableError
from .canonical_execution_store import CanonicalExecutionStore
from .execution_result import ExecutionResult
from .models import OrderCurrencyContext, OrderIntent

logger = get_logger(__name__)

TRIGGER_POLICY_VERSION = "ohlc-conservative-v1"
_WORKING_STATUSES = ("submitted", "working", "partially_filled")
_TERMINAL_STATUSES = ("filled", "cancelled", "expired", "rejected")
_ADVISORY_LOCK_ID = 704_267_310
_LIFECYCLE_ERROR_PREFIX = "paper_lifecycle_error:"
_LIFECYCLE_FAILURES_TOTAL = counter(
    "vm_execution_paper_order_lifecycle_failures_total",
    "Durable paper-order lifecycle failures by scope and exception type",
    ("scope", "exception_type"),
)


@dataclass(frozen=True)
class CommittedCandle:
    """A complete, provenance-bearing OHLCV observation."""

    price_id: int
    instr_id: int
    ts: datetime
    timeframe: str
    source: str
    content_revision: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None

    @classmethod
    def from_row(cls, row: InstrumentPrice) -> CommittedCandle:
        values = {
            name: Decimal(str(getattr(row, name)))
            for name in ("open", "high", "low", "close")
            if getattr(row, name) is not None
        }
        expected_fields = 4
        if len(values) != expected_fields or any(
            not value.is_finite() or value <= 0 for value in values.values()
        ):
            msg = f"Price row {row.price_id} is not a complete positive OHLC candle"
            raise ValueError(msg)
        if values["low"] > min(values["open"], values["close"], values["high"]):
            msg = f"Price row {row.price_id} has an invalid low"
            raise ValueError(msg)
        if values["high"] < max(values["open"], values["close"], values["low"]):
            msg = f"Price row {row.price_id} has an invalid high"
            raise ValueError(msg)
        revision = int(row.content_revision)
        if revision <= 0:
            msg = f"Price row {row.price_id} has an invalid content revision"
            raise ValueError(msg)
        timestamp = row.ts.replace(tzinfo=UTC) if row.ts.tzinfo is None else row.ts.astimezone(UTC)
        volume = Decimal(str(row.volume)) if row.volume is not None else None
        if volume is not None and (not volume.is_finite() or volume < 0):
            msg = f"Price row {row.price_id} has invalid volume"
            raise ValueError(msg)
        return cls(
            price_id=int(row.price_id),
            instr_id=int(row.instr_id),
            ts=timestamp,
            timeframe=str(row.timeframe),
            source=str(row.source),
            content_revision=revision,
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            volume=volume,
        )


@dataclass(frozen=True)
class TriggerDecision:
    """Conservative deterministic fill reference selected from one candle."""

    reason: str
    reference_price: Decimal
    apply_market_slippage: bool


def evaluate_trigger(
    *,
    side: str,
    order_type: str,
    trigger_price: Decimal | None,
    limit_price: Decimal | None,
    candle: CommittedCandle,
) -> TriggerDecision | None:
    """Evaluate stop/limit/bracket rules with adverse same-bar precedence."""
    normalized_side = str(side).strip().upper()
    normalized_type = str(order_type).strip().lower()
    if normalized_side not in {"BUY", "SELL"}:
        msg = f"Unsupported paper order side: {side!r}"
        raise ValueError(msg)

    stop: TriggerDecision | None = None
    if trigger_price is not None:
        trigger = Decimal(str(trigger_price))
        if normalized_side == "SELL":
            if candle.open <= trigger:
                stop = TriggerDecision("stop_gap", candle.open, True)
            elif candle.low <= trigger:
                stop = TriggerDecision("stop_cross", trigger, True)
        elif candle.open >= trigger:
            stop = TriggerDecision("stop_gap", candle.open, True)
        elif candle.high >= trigger:
            stop = TriggerDecision("stop_cross", trigger, True)

    target: TriggerDecision | None = None
    if limit_price is not None:
        limit = Decimal(str(limit_price))
        if (normalized_side == "SELL" and candle.high >= limit) or (
            normalized_side == "BUY" and candle.low <= limit
        ):
            target = TriggerDecision("limit_cross", limit, False)

    if normalized_type == "stop":
        return stop
    if normalized_type in {"limit", "take_profit"}:
        return target
    if normalized_type in {"bracket", "oco"}:
        # OHLC does not reveal intrabar sequencing. Selecting the stop is the
        # deterministic adverse result and prevents optimistic backfill.
        return stop or target
    msg = f"Unsupported durable paper order type: {order_type!r}"
    raise ValueError(msg)


def _currency_context(payload: dict[str, Any]) -> OrderCurrencyContext:
    raw = payload.get("currency_context")
    if not isinstance(raw, dict):
        msg = "Canonical paper order omitted currency context"
        raise TypeError(msg)

    def _timestamp(name: str) -> datetime:
        value = raw.get(name)
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    return OrderCurrencyContext(
        account_currency=str(raw.get("account_currency") or ""),
        settlement_currency=str(raw.get("settlement_currency") or ""),
        account_to_settlement_rate=Decimal(str(raw.get("account_to_settlement_rate"))),
        requested_at=_timestamp("requested_at"),
        observed_at=_timestamp("observed_at"),
        source=str(raw.get("source") or ""),
    )


class PaperOrderLifecycleWorker:
    """Consume committed candles and advance durable local-paper resting orders."""

    def __init__(
        self,
        *,
        engine: Any,
        session_factory: Any,
        interval_sec: float = 2.0,
        max_volume_participation: Decimal = Decimal("0.01"),
        account_serializer: AccountExecutionSerializer | None = None,
    ) -> None:
        if interval_sec <= 0:
            msg = "Paper order lifecycle interval must be positive"
            raise ValueError(msg)
        participation = Decimal(str(max_volume_participation))
        if not participation.is_finite() or participation <= 0 or participation > 1:
            msg = "Paper fill participation must be within (0, 1]"
            raise ValueError(msg)
        self._engine = engine
        self._session_factory = session_factory
        self._interval_sec = float(interval_sec)
        self._participation = participation
        self._canonical_store = CanonicalExecutionStore(session_factory=session_factory)
        self._account_serializer = (
            account_serializer
            or getattr(engine, "account_execution_serializer", None)
            or AccountExecutionSerializer(session_factory)
        )
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.process_once()
            except (OSError, SQLAlchemyError) as exc:
                self._mark_health(
                    healthy=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._record_failure_metric(scope="pass", exc=exc)
                logger.exception("Durable paper-order lifecycle pass failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_sec)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop_event.set()

    @property
    def is_stopping(self) -> bool:
        """Whether task completion is an expected part of graceful shutdown."""
        return self._stop_event.is_set()

    def is_healthy(self) -> bool:
        """Return parent-visible lifecycle health when the engine exposes it."""
        checker = getattr(self._engine, "is_paper_order_lifecycle_healthy", None)
        if callable(checker):
            return bool(checker())
        status = getattr(self._engine, "paper_order_lifecycle_health", None)
        return bool(status.get("healthy")) if isinstance(status, dict) else True

    def _mark_health(
        self,
        *,
        healthy: bool,
        error: str | None,
        failed_orders: int | None = None,
    ) -> None:
        marker = getattr(self._engine, "mark_paper_order_lifecycle_health", None)
        if callable(marker):
            marker(
                healthy=healthy,
                error=error,
                failed_orders=failed_orders,
            )

    @staticmethod
    def _record_failure_metric(*, scope: str, exc: Exception) -> None:
        if _LIFECYCLE_FAILURES_TOTAL is not None:
            _LIFECYCLE_FAILURES_TOTAL.labels(
                scope=scope,
                exception_type=type(exc).__name__,
            ).inc()

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        detail = " ".join(str(exc).split())
        if not detail:
            detail = "no exception detail"
        return f"{_LIFECYCLE_ERROR_PREFIX} {type(exc).__name__}: {detail}"[:1000]

    def _record_order_failure(self, order_id: str, exc: Exception) -> None:
        """Retain order-scoped failure evidence without terminalizing protection."""
        message = self._failure_message(exc)
        with self._session_factory() as session:
            pending = session.get(PendingOrder, order_id)
            if pending is not None:
                pending.error_message = message
                session.commit()
        self._record_failure_metric(scope="order", exc=exc)
        logger.exception(
            "Durable paper order failed; continuing remaining account partitions",
            extra={
                "order_id": order_id,
                "exception_type": type(exc).__name__,
            },
        )

    def _unresolved_failure_count(self) -> int:
        with self._session_factory() as session:
            return int(
                session.query(func.count(PendingOrder.order_id))
                .filter(
                    PendingOrder.broker == "paper",
                    PendingOrder.broker_environment == "paper",
                    or_(
                        PendingOrder.pending_projection_exec_id.is_not(None),
                        and_(
                            PendingOrder.status.in_(_WORKING_STATUSES),
                            PendingOrder.error_message.like(f"{_LIFECYCLE_ERROR_PREFIX}%"),
                        ),
                    ),
                )
                .scalar()
                or 0
            )

    async def process_once(self) -> int:
        """Process all currently available bars under one database-wide fence."""
        with self._session_factory() as lock_session:
            if lock_session.bind.dialect.name == "postgresql":
                acquired = bool(
                    lock_session.execute(
                        text("SELECT pg_try_advisory_lock(:lock_id)"),
                        {"lock_id": _ADVISORY_LOCK_ID},
                    ).scalar()
                )
                if not acquired:
                    self._mark_health(
                        healthy=False,
                        error="Paper-order lifecycle fence is held by another worker",
                    )
                    return 0
            try:
                order_ids = [
                    str(value)
                    for (value,) in (
                        lock_session.query(PendingOrder.order_id)
                        .filter(
                            or_(
                                PendingOrder.status.in_(_WORKING_STATUSES),
                                PendingOrder.pending_projection_exec_id.is_not(None),
                            ),
                            PendingOrder.broker == "paper",
                            PendingOrder.broker_environment == "paper",
                            PendingOrder.canonical_order_id.is_not(None),
                            PendingOrder.market_data_source.is_not(None),
                            PendingOrder.market_data_timeframe.is_not(None),
                            (
                                (PendingOrder.trigger_price.is_not(None))
                                | (PendingOrder.limit_price.is_not(None))
                            ),
                        )
                        .order_by(PendingOrder.created_at, PendingOrder.order_id)
                        .all()
                    )
                ]
                processed = 0
                failures: list[str] = []
                for order_id in order_ids:
                    try:
                        user_id, account_id = self._order_partition(order_id)
                        async with self._account_serializer.hold(
                            user_id=user_id,
                            broker_account_id=account_id,
                            writer_kind="paper_lifecycle",
                        ):
                            while await self._process_next_bar(order_id):
                                processed += 1
                                if self._is_terminal(order_id):
                                    break
                    except AccountExecutionBusyError:
                        continue
                    except (OSError, SQLAlchemyError):
                        raise
                    except (ArithmeticError, RuntimeError, TypeError, ValueError) as exc:
                        self._record_order_failure(order_id, exc)
                        # A failure after a terminal fill commit is not a
                        # malformed resting-order record that can be retried on
                        # its next candle. Let the task fail so readiness stays
                        # false until restart/reconciliation repairs projections.
                        if self._is_terminal(order_id):
                            raise
                        failures.append(f"{order_id} ({type(exc).__name__})")
                unresolved = self._unresolved_failure_count()
                healthy = not failures and unresolved == 0
                self._mark_health(
                    healthy=healthy,
                    error=(
                        "Paper-order lifecycle failures: " + ", ".join(failures)
                        if failures
                        else (
                            f"{unresolved} durable paper order(s) retain lifecycle errors"
                            if unresolved
                            else None
                        )
                    ),
                    failed_orders=unresolved,
                )
                return processed
            finally:
                if lock_session.bind.dialect.name == "postgresql":
                    lock_session.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _ADVISORY_LOCK_ID},
                    )

    def _order_partition(self, order_id: str) -> tuple[str, int]:
        with self._session_factory() as session:
            row = session.get(PendingOrder, order_id)
            if row is None:
                message = f"Pending paper order {order_id} disappeared"
                raise ValueError(message)
            user_id = str(row.user_id or "").strip()
            account_id = int(row.broker_account_id or 0)
            if not user_id or account_id <= 0:
                message = f"Pending paper order {order_id} has invalid account ownership"
                raise ValueError(message)
            return user_id, account_id

    def _is_terminal(self, order_id: str) -> bool:
        with self._session_factory() as session:
            status = session.query(PendingOrder.status).filter_by(order_id=order_id).scalar()
            return status in _TERMINAL_STATUSES

    async def _process_next_bar(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        order_id: str,
    ) -> bool:
        candle: CommittedCandle | None = None
        with self._session_factory() as read_session:
            pending = read_session.get(PendingOrder, order_id)
            if pending is None:
                return False
            broker_context = self._broker_context(read_session, pending)
            projection_pending = pending.pending_projection_exec_id is not None
            if not projection_pending and pending.status not in _WORKING_STATUSES:
                return False
            if projection_pending:
                price_row = None
            else:
                start_ts = pending.last_market_data_ts or pending.created_at
                start_ts = (
                    start_ts.replace(tzinfo=None)
                    if getattr(start_ts, "tzinfo", None) is not None
                    else start_ts
                )
                price_row = (
                    read_session.query(InstrumentPrice)
                    .filter(
                        InstrumentPrice.instr_id == pending.instr_id,
                        InstrumentPrice.source == pending.market_data_source,
                        InstrumentPrice.timeframe == pending.market_data_timeframe,
                        InstrumentPrice.ts > start_ts,
                    )
                    .order_by(InstrumentPrice.ts, InstrumentPrice.price_id)
                    .first()
                )
                if price_row is None:
                    return False
                candle = CommittedCandle.from_row(price_row)

        broker = await self._engine.get_reconciliation_broker(broker_context)
        if not isinstance(broker, PaperBroker):
            return False
        if not broker.is_connected:
            await broker.connect()
        if projection_pending:
            await self._drain_projection(order_id, broker, broker_context)
            return True
        assert candle is not None

        plan: PaperFillPlan | None = None
        with self._session_factory() as session:
            pending = (
                session.query(PendingOrder)
                .filter(PendingOrder.order_id == order_id)
                .with_for_update()
                .one_or_none()
            )
            if pending is None or pending.status not in _WORKING_STATUSES:
                return False
            watermark = pending.last_market_data_ts
            watermark_utc = (
                watermark.replace(tzinfo=UTC)
                if watermark is not None and watermark.tzinfo is None
                else watermark
            )
            if watermark_utc is not None and candle.ts <= watermark_utc.astimezone(UTC):
                return False
            decision = evaluate_trigger(
                side=pending.side,
                order_type=pending.order_type,
                trigger_price=pending.trigger_price,
                limit_price=pending.limit_price,
                candle=candle,
            )
            pending.last_market_data_ts = candle.ts.replace(tzinfo=None)
            pending.last_market_data_revision = candle.content_revision
            if str(pending.error_message or "").startswith(_LIFECYCLE_ERROR_PREFIX):
                pending.error_message = None
            if decision is None:
                session.commit()
                return True

            remaining = Decimal(str(pending.quantity)) - Decimal(
                str(pending.cumulative_filled_quantity or 0)
            )
            volume_cap = (
                remaining
                if candle.volume is None
                else max(Decimal("0"), candle.volume * self._participation)
            )
            fill_quantity = min(remaining, volume_cap)
            if fill_quantity <= 0:
                session.commit()
                return True

            canonical_order = session.get(Order, pending.canonical_order_id)
            if canonical_order is None:
                msg = f"Pending order {order_id} lost its canonical order"
                raise RuntimeError(msg)
            canonical_intent = session.get(CanonicalOrderIntent, canonical_order.intent_id)
            if canonical_intent is None or not isinstance(canonical_intent.payload, dict):
                msg = f"Canonical order {canonical_order.order_id} lost its intent"
                raise RuntimeError(msg)
            payload = dict(canonical_intent.payload)
            metadata = dict(payload.get("metadata") or {})
            symbol = str(metadata.get("broker_symbol") or payload.get("symbol") or pending.symbol)
            intent = OrderIntent(
                broker_code=str(payload.get("broker_code") or pending.broker),
                symbol=symbol,
                side=pending.side,
                quantity=float(remaining),
                order_type=pending.order_type,
                time_in_force=pending.time_in_force,
                limit_price=(
                    float(pending.limit_price) if pending.limit_price is not None else None
                ),
                stop_price=(
                    float(pending.trigger_price) if pending.trigger_price is not None else None
                ),
                metadata=metadata,
                currency_context=_currency_context(payload),
            )
            broker_order_id = str(
                pending.broker_order_id
                or canonical_order.broker_order_ref
                or pending.client_order_id
            )
            terminal = fill_quantity == remaining
            trade_id = (
                f"paper:{canonical_order.order_id}:price:{candle.price_id}:"
                f"rev:{candle.content_revision}"
            )
            provenance = {
                "source_price_id": candle.price_id,
                "source_content_revision": candle.content_revision,
                "trigger_policy_version": pending.trigger_policy_version,
                "market_data_source": candle.source,
                "market_data_timeframe": candle.timeframe,
                "trigger_reason": decision.reason,
            }
            try:
                plan = broker.prepare_resting_fill(
                    intent,
                    broker_order_id=broker_order_id,
                    trade_id=trade_id,
                    quantity=fill_quantity,
                    reference_price=decision.reference_price,
                    timestamp=candle.ts,
                    source_provenance=provenance,
                    apply_market_slippage=decision.apply_market_slippage,
                    is_terminal=terminal,
                )
            except ReduceOnlyPositionUnavailableError as exc:
                self._cancel_unprotectable_order(
                    session,
                    pending,
                    canonical_order,
                    candle,
                    reason=str(exc),
                )
                session.commit()
                return False
            inserted_execution_ids = self._canonical_store.apply_broker_result_on_session(
                session,
                order_id=int(canonical_order.order_id),
                result=plan.result,
                fills=[plan.fill],
                instr_id=int(pending.instr_id),
            )
            if len(inserted_execution_ids) != 1:
                msg = (
                    f"Paper fill {trade_id} persisted {len(inserted_execution_ids)} "
                    "new canonical executions"
                )
                raise RuntimeError(msg)
            pending.pending_projection_exec_id = inserted_execution_ids[0]
            previous_quantity = Decimal(str(pending.cumulative_filled_quantity or 0))
            previous_notional = previous_quantity * Decimal(str(pending.filled_price or 0))
            cumulative_quantity = previous_quantity + fill_quantity
            pending.cumulative_filled_quantity = cumulative_quantity
            pending.filled_quantity = cumulative_quantity
            pending.filled_price = (
                previous_notional + fill_quantity * plan.fill.price
            ) / cumulative_quantity
            pending.commission = Decimal(str(pending.commission or 0)) + plan.fill.commission
            pending.commission_currency = plan.fill.commission_currency
            pending.status = "filled" if terminal else "partially_filled"
            pending.resolved_at = candle.ts.replace(tzinfo=None) if terminal else None
            if terminal and pending.oco_group_id:
                self._cancel_oco_siblings(session, pending)
            elif pending.oco_group_id:
                self._resize_oco_siblings(session, pending)
            # Lineage for a lifecycle fill is the canonical ledger itself:
            # orders -> order_intents.canonical_signal_id -> canonical_signals.
            session.commit()

        assert plan is not None
        broker.apply_committed_fill(plan)
        await self._drain_projection(order_id, broker, broker_context)
        return True

    @staticmethod
    def _broker_context(session: Any, pending: PendingOrder) -> dict[str, Any]:
        account = session.get(LinkedBrokerAccount, pending.broker_account_id)
        broker = session.get(Broker, account.broker_id) if account is not None else None
        if account is None or broker is None:
            msg = f"Pending order {pending.order_id} lost its broker account"
            raise RuntimeError(msg)
        return {
            "user_id": pending.user_id,
            "strategy_id": pending.strategy_id,
            "broker_code": broker.code,
            "environment": account.environment,
            "credential_ref": pending.credential_ref or "",
            "broker_account_id": int(account.account_id),
            "settlement_currency": pending.settlement_currency,
            "profile": {
                "broker": broker.code,
                "broker_account_id": int(account.account_id),
                "broker_environment": account.environment,
                "base_ccy": account.base_ccy,
                "accounts": {
                    str(account.account_id): {
                        "account_id": int(account.account_id),
                        "broker": broker.code,
                        "environment": account.environment,
                        "status": account.status,
                        "base_ccy": account.base_ccy,
                        "paper_initial_equity": (
                            float(account.paper_initial_equity)
                            if account.paper_initial_equity is not None
                            else None
                        ),
                        "paper_initial_cash": (
                            float(account.paper_initial_cash)
                            if account.paper_initial_cash is not None
                            else None
                        ),
                    }
                },
            },
        }

    def _cancel_unprotectable_order(
        self,
        session: Any,
        pending: PendingOrder,
        canonical_order: Order,
        candle: CommittedCandle,
        *,
        reason: str,
    ) -> None:
        """Durably cancel a reduce-only resting order whose position is gone.

        The position it protected was already closed by another fill (a flatten
        or an OCO peer), so this order can never fill; leaving it working would
        replay the same failure on every later committed bar. Its OCO siblings
        protect the same vanished position and are cancelled with it.
        """
        resolved_at = candle.ts.replace(tzinfo=None)
        pending.status = "cancelled"
        pending.resolved_at = resolved_at
        pending.error_message = f"Cancelled: {reason}"
        if canonical_order.state not in {"filled", "rejected", "canceled"}:
            canonical_order.state = "canceled"
            canonical_order.last_update = resolved_at
        if pending.oco_group_id:
            self._cancel_oco_siblings(session, pending, reason=pending.error_message)
        logger.warning(
            "Durable paper order cancelled: its position no longer exists",
            order_id=pending.order_id,
            symbol=pending.symbol,
            reason=reason,
        )

    @staticmethod
    def _cancel_oco_siblings(
        session: Any, filled: PendingOrder, *, reason: str | None = None
    ) -> None:
        siblings = (
            session.query(PendingOrder)
            .filter(
                PendingOrder.oco_group_id == filled.oco_group_id,
                PendingOrder.order_id != filled.order_id,
                PendingOrder.status.in_(_WORKING_STATUSES),
            )
            .with_for_update()
            .all()
        )
        for sibling in siblings:
            sibling.status = "cancelled"
            sibling.resolved_at = filled.resolved_at
            sibling.error_message = reason or f"OCO peer {filled.order_id} filled"
            if sibling.canonical_order_id is not None:
                order = session.get(Order, sibling.canonical_order_id)
                if order is not None and order.state not in {"filled", "rejected", "canceled"}:
                    order.state = "canceled"
                    order.last_update = filled.resolved_at

    @staticmethod
    def _resize_oco_siblings(session: Any, partially_filled: PendingOrder) -> None:
        """Keep every OCO member limited to the shared unfilled allocation."""
        remaining = Decimal(str(partially_filled.quantity)) - Decimal(
            str(partially_filled.cumulative_filled_quantity)
        )
        siblings = (
            session.query(PendingOrder)
            .filter(
                PendingOrder.oco_group_id == partially_filled.oco_group_id,
                PendingOrder.order_id != partially_filled.order_id,
                PendingOrder.status.in_(_WORKING_STATUSES),
            )
            .with_for_update()
            .all()
        )
        for sibling in siblings:
            sibling.quantity = Decimal(str(sibling.cumulative_filled_quantity or 0)) + remaining

    async def _drain_projection(
        self,
        order_id: str,
        broker: PaperBroker,
        context: dict[str, Any],
    ) -> None:
        """Apply one exact fill's idempotent metrics/position/NAV projections."""
        with self._session_factory() as session:
            pending = session.get(PendingOrder, order_id)
            if pending is None:
                return
            canonical_execution_id = pending.pending_projection_exec_id
            if canonical_execution_id is None:
                return
            execution = session.get(Execution, canonical_execution_id)
            if execution is None or int(execution.order_id) != int(pending.canonical_order_id or 0):
                msg = (
                    f"Pending order {order_id} projection references invalid canonical "
                    f"execution {canonical_execution_id}"
                )
                raise RuntimeError(msg)
            canonical_order = session.get(Order, pending.canonical_order_id)
            canonical_intent = (
                session.get(CanonicalOrderIntent, canonical_order.intent_id)
                if canonical_order is not None
                else None
            )
            canonical_signal = (
                session.get(CanonicalSignal, canonical_intent.canonical_signal_id)
                if canonical_intent is not None
                else None
            )
            instrument = session.get(Instrument, execution.instr_id)
            if (
                canonical_order is None
                or canonical_intent is None
                or canonical_signal is None
                or instrument is None
            ):
                msg = f"Pending order {order_id} lost canonical projection lineage"
                raise RuntimeError(msg)
            if (
                int(canonical_signal.instr_id) != int(execution.instr_id)
                or str(canonical_signal.strategy_id) != str(pending.strategy_id)
                or str(canonical_signal.external_signal_id) != str(pending.signal_id)
            ):
                msg = f"Pending order {order_id} has conflicting canonical projection lineage"
                raise RuntimeError(msg)
            if execution.source_price_id is None:
                msg = f"Paper execution {canonical_execution_id} omitted source price lineage"
                raise RuntimeError(msg)
            source_row = session.get(InstrumentPrice, execution.source_price_id)
            if source_row is None:
                msg = (
                    f"Paper execution {canonical_execution_id} references missing "
                    f"price {execution.source_price_id}"
                )
                raise RuntimeError(msg)
            candle = CommittedCandle.from_row(source_row)
            if int(execution.source_content_revision or 0) != candle.content_revision or str(
                execution.trigger_policy_version or ""
            ) != str(pending.trigger_policy_version):
                msg = (
                    f"Paper execution {canonical_execution_id} changed source-bar "
                    "projection lineage"
                )
                raise RuntimeError(msg)
            decision = evaluate_trigger(
                side=pending.side,
                order_type=pending.order_type,
                trigger_price=pending.trigger_price,
                limit_price=pending.limit_price,
                candle=candle,
            )
            if decision is None:
                msg = (
                    f"Paper execution {canonical_execution_id} no longer matches its "
                    "committed trigger"
                )
                raise RuntimeError(msg)
            fill_ts = execution.fill_ts
            fill_ts = (
                fill_ts.replace(tzinfo=UTC) if fill_ts.tzinfo is None else fill_ts.astimezone(UTC)
            )
            signal_action = (
                "close"
                if pending.reduce_only
                else ("long" if str(pending.side).upper() == "BUY" else "short")
            )
            execution_result = ExecutionResult(
                success=True,
                signal_id=str(pending.signal_id),
                symbol=str(pending.symbol),
                execution_mode=str(pending.execution_mode),
                broker=str(pending.broker),
                orders_submitted=0,
                orders_filled=1,
                total_quantity=float(execution.qty),
                average_price=float(execution.price),
                total_commission=float(execution.fee_amount),
                broker_account_id=int(pending.broker_account_id),
                settlement_currency=str(pending.settlement_currency),
                executed_at=fill_ts,
                order_results=[
                    {
                        "canonical_order_id": int(canonical_order.order_id),
                        "canonical_execution_id": int(execution.exec_id),
                        "order_id": str(
                            pending.broker_order_id
                            or canonical_order.broker_order_ref
                            or pending.client_order_id
                        ),
                        "trade_id": str(execution.trade_id),
                        "status": str(pending.status),
                        "filled_quantity": float(execution.qty),
                        "average_price": float(execution.price),
                        "commission": float(execution.fee_amount),
                        "commission_currency": str(execution.fee_ccy),
                        "source_price_id": int(execution.source_price_id),
                        "source_content_revision": int(execution.source_content_revision),
                        "trigger_policy_version": str(execution.trigger_policy_version),
                    }
                ],
            )
            projection = {
                "user_id": str(pending.user_id),
                "strategy_id": str(pending.strategy_id),
                "symbol": str(pending.symbol),
                "asset_class": str(instrument.asset_class),
                "canonical_signal_id": int(canonical_signal.signal_id),
                "canonical_execution_id": int(execution.exec_id),
                "signal_id": str(pending.signal_id),
                "run_id": pending.run_id,
                "signal_action": signal_action,
                "reference_price": float(decision.reference_price),
                "result": execution_result,
                "profile": dict(context.get("profile") or {}),
            }

        account_state = await self._engine._get_account_state(
            str(context["user_id"]),
            broker,
            context.get("profile"),
            allow_profile_fallback=False,
        )
        self._engine._persist_paper_fill_projections(
            **projection,
            account_state=account_state,
        )
        await self._engine._update_daily_nav_snapshot(
            user_id=str(context["user_id"]),
            account_state=account_state,
            account_id=int(context["broker_account_id"]),
        )
        with self._session_factory() as session:
            pending = (
                session.query(PendingOrder)
                .filter(PendingOrder.order_id == order_id)
                .with_for_update()
                .one_or_none()
            )
            if pending is None:
                return
            if pending.pending_projection_exec_id != canonical_execution_id:
                msg = f"Pending order {order_id} projection checkpoint changed during delivery"
                raise RuntimeError(msg)
            pending.pending_projection_exec_id = None
            if str(pending.error_message or "").startswith(_LIFECYCLE_ERROR_PREFIX):
                pending.error_message = None
            session.commit()


__all__ = [
    "TRIGGER_POLICY_VERSION",
    "CommittedCandle",
    "PaperOrderLifecycleWorker",
    "TriggerDecision",
    "evaluate_trigger",
]
