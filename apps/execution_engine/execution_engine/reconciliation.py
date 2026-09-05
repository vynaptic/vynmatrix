"""Broker/DB reconciliation for positions and open orders."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast

from lib_common.logging import get_logger
from lib_data.market_data import normalize_product_symbol

from .account_execution_serializer import (
    AccountExecutionBusyError,
    AccountExecutionSerializer,
)
from .brokers.base import BrokerFill, BrokerOrderResult, OrderStatus
from .metrics.normalization import normalize_position_side
from .metrics.pnl_service import (
    CurrencyConversionRequiredError,
    PnLLedgerUnavailableError,
)

logger = get_logger(__name__)

# Broker order states after which a pending_orders row can never become open again.
_TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)
# Broker responses definitively saying the order does not exist in its book
# (e.g. PaperBroker's ``Order not found: <id>``) — distinct from a transient
# API failure, which must NOT terminal-sync a live order.
_ORDER_UNKNOWN_MARKERS = ("not found", "not_found", "unknown order")
_PAPER_ENVIRONMENT = "paper"
_DURABLE_LOCAL_PAPER_STATUSES = frozenset({"submitted", "working", "partially_filled"})


def _order_unknown_at_broker(result: Any) -> bool:
    """True when the broker definitively reports it does not know the order."""
    error_message = getattr(result, "error_message", None) or ""
    error_code = getattr(result, "error_code", None) or ""
    text = f"{error_message} {error_code}".lower()
    return any(marker in text for marker in _ORDER_UNKNOWN_MARKERS)


def _is_durable_local_paper_order(
    payload: dict[str, Any],
    *,
    environment: str,
) -> bool:
    """Return whether the database lifecycle, not PaperBroker memory, owns the order."""
    return (
        str(environment).strip().lower() == _PAPER_ENVIRONMENT
        and str(payload.get("broker") or "").strip().lower() == "paper"
        and str(payload.get("status") or "").strip().lower() in _DURABLE_LOCAL_PAPER_STATUSES
        and payload.get("canonical_order_id") is not None
        and payload.get("instr_id") is not None
        and bool(str(payload.get("market_data_source") or "").strip())
        and bool(str(payload.get("market_data_timeframe") or "").strip())
        and (payload.get("trigger_price") is not None or payload.get("limit_price") is not None)
    )


def _symbol_key(symbol: str) -> str:
    """Canonical comparison key so ``ETH/USD`` / ``ETHUSD`` / ``ETH-USD`` collide.

    Reconciliation compares the persisted ledger (``Instrument.canonical``, slash
    form ``ETH/USD``) against the broker book (``signal``/``intent`` symbol, no
    separator ``ETHUSD``; live Coinbase reports dash ``ETH-USD``). Without a
    common key the SAME held position is double-flagged as BOTH
    ``missing_broker_position`` AND ``phantom_broker_position`` — a block-severity
    pair that opens the circuit breaker on a pure formatting difference. Reuses
    the platform-canonical ``normalize_product_symbol`` (strips ``/-_``,
    uppercases) used by market_data_ingestor / scoring / execution_metrics.
    """
    return normalize_product_symbol(symbol)


@dataclass
class ReconciliationFinding:
    """A single reconciliation mismatch or warning."""

    severity: str
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _normalize_position(position: Any) -> dict[str, Any]:
    if isinstance(position, dict):
        symbol = str(position.get("symbol") or "")
        side = normalize_position_side(position.get("side"))
        quantity = float(position.get("quantity") or 0.0)
        entry_price = float(position.get("entry_price") or 0.0)
        current_price = float(position.get("current_price") or 0.0)
        quantity_unit = position.get("quantity_unit")
        contract_multiplier = position.get("contract_multiplier")
        gross_notional = position.get("gross_notional")
        notional_currency = position.get("notional_currency")
    else:
        symbol = str(getattr(position, "symbol", "") or "")
        side = normalize_position_side(getattr(position, "side", None))
        quantity = float(getattr(position, "quantity", 0.0) or 0.0)
        entry_price = float(getattr(position, "entry_price", 0.0) or 0.0)
        current_price = float(getattr(position, "current_price", 0.0) or 0.0)
        quantity_unit = getattr(position, "quantity_unit", None)
        contract_multiplier = getattr(position, "contract_multiplier", None)
        gross_notional = getattr(position, "gross_notional", None)
        notional_currency = getattr(position, "notional_currency", None)
    return {
        "symbol": symbol,
        "side": side,
        "quantity": abs(quantity),
        "entry_price": entry_price,
        "current_price": current_price,
        "quantity_unit": quantity_unit,
        "contract_multiplier": contract_multiplier,
        "gross_notional": gross_notional,
        "notional_currency": notional_currency,
    }


def _positions_by_symbol(positions: Iterable[Any]) -> dict[str, dict[str, Any]]:
    # Key by the canonical symbol so the ledger (``ETH/USD``) and the broker
    # (``ETHUSD``) collapse to one entry; the VALUE keeps the original symbol so
    # finding context and the sync_positions write-back stay in their native
    # format. Last-write-wins on a collision is fine — each side reports at most
    # one row per instrument.
    by_key: dict[str, dict[str, Any]] = {}
    for normalized in (_normalize_position(position) for position in positions):
        if normalized["symbol"]:
            by_key[_symbol_key(normalized["symbol"])] = normalized
    return by_key


def _broker_open_order_ids(open_orders: Iterable[Any]) -> set[str]:
    order_ids: set[str] = set()
    for order in open_orders:
        if isinstance(order, dict):
            candidates = (
                order.get("broker_order_id"),
                order.get("order_id"),
                order.get("client_order_id"),
            )
        else:
            candidates = (
                getattr(order, "order_id", None),
                getattr(order, "broker_order_id", None),
                getattr(order, "client_order_id", None),
            )
        order_ids.update(str(value) for value in candidates if value)
    return order_ids


def _raise_broker_unavailable(broker_code: str) -> NoReturn:
    message = f"Broker unavailable for reconciliation: {broker_code}"
    raise RuntimeError(message)


def _normalize_reconciliation_account_id(value: Any) -> int:
    if value is None:
        message = "Reconciliation requires broker_account_id"
        raise ValueError(message)
    if isinstance(value, bool):
        message = "Reconciliation broker_account_id must be a positive integer"
        raise TypeError(message)
    try:
        account_id = int(value)
    except (TypeError, ValueError) as exc:
        message = "Reconciliation broker_account_id must be a positive integer"
        raise ValueError(message) from exc
    if account_id <= 0:
        message = "Reconciliation broker_account_id must be a positive integer"
        raise ValueError(message)
    return account_id


def classify_reconciliation(
    *,
    local_positions: Iterable[Any],
    broker_positions: Iterable[Any],
    pending_orders: dict[str, dict[str, Any]],
    quantity_tolerance: float = 1e-8,
    price_tolerance_pct: float = 0.02,
    broker_open_orders: Iterable[Any] | None = None,
) -> list[ReconciliationFinding]:
    """Compare broker state to persisted state and classify mismatches."""
    findings: list[ReconciliationFinding] = []
    local_by_symbol = _positions_by_symbol(local_positions)
    broker_by_symbol = _positions_by_symbol(broker_positions)
    pending_symbols = {
        _symbol_key(str(payload.get("symbol") or ""))
        for payload in pending_orders.values()
        if payload.get("symbol")
    }

    for symbol, local_pos in local_by_symbol.items():
        broker_pos = broker_by_symbol.get(symbol)
        if broker_pos is None:
            findings.append(
                ReconciliationFinding(
                    severity="block",
                    code="missing_broker_position",
                    message=f"Persisted position missing at broker for {local_pos['symbol']}",
                    context={"symbol": local_pos["symbol"], "local_position": local_pos},
                )
            )
            continue

        if broker_pos["side"] != local_pos["side"]:
            findings.append(
                ReconciliationFinding(
                    severity="block",
                    code="position_side_mismatch",
                    message=f"Position side mismatch for {symbol}",
                    context={
                        "symbol": symbol,
                        "local_side": local_pos["side"],
                        "broker_side": broker_pos["side"],
                    },
                )
            )

        quantity_diff = abs(broker_pos["quantity"] - local_pos["quantity"])
        if quantity_diff > quantity_tolerance:
            severity = "warn" if symbol in pending_symbols else "block"
            code = (
                "position_quantity_pending" if severity == "warn" else "position_quantity_mismatch"
            )
            findings.append(
                ReconciliationFinding(
                    severity=severity,
                    code=code,
                    message=f"Position quantity mismatch for {symbol}",
                    context={
                        "symbol": symbol,
                        "local_quantity": local_pos["quantity"],
                        "broker_quantity": broker_pos["quantity"],
                    },
                )
            )

        local_mark = local_pos["current_price"] or local_pos["entry_price"]
        broker_mark = broker_pos["current_price"] or broker_pos["entry_price"]
        if local_mark > 0 and broker_mark > 0:
            drift_pct = abs(local_mark - broker_mark) / broker_mark
            if drift_pct > price_tolerance_pct:
                findings.append(
                    ReconciliationFinding(
                        severity="warn",
                        code="mark_price_drift",
                        message=f"Mark price drift for {symbol}",
                        context={
                            "symbol": symbol,
                            "local_price": local_mark,
                            "broker_price": broker_mark,
                            "drift_pct": drift_pct,
                        },
                    )
                )

    for symbol, broker_pos in broker_by_symbol.items():
        if symbol not in local_by_symbol:
            findings.append(
                ReconciliationFinding(
                    severity="block",
                    code="phantom_broker_position",
                    message=(
                        "Broker reports position not present in persistence for "
                        f"{broker_pos['symbol']}"
                    ),
                    context={"symbol": broker_pos["symbol"], "broker_position": broker_pos},
                )
            )

    if broker_open_orders is not None:
        broker_order_ids = _broker_open_order_ids(broker_open_orders)
        matched_broker_ids: set[str] = set()
        for order_id, payload in sorted(pending_orders.items()):
            local_identities = {
                str(value)
                for value in (
                    order_id,
                    payload.get("order_id"),
                    payload.get("broker_order_id"),
                    payload.get("client_order_id"),
                )
                if value
            }
            matching = local_identities & broker_order_ids
            if matching:
                matched_broker_ids.update(matching)
                continue
            findings.append(
                ReconciliationFinding(
                    severity="warn",
                    code="missing_broker_open_order",
                    message=f"Pending local order missing at broker: {order_id}",
                    context={
                        "order_id": order_id,
                        "symbol": payload.get("symbol"),
                        "side": payload.get("side"),
                    },
                )
            )

        for order_id in sorted(broker_order_ids - matched_broker_ids):
            findings.append(
                ReconciliationFinding(
                    severity="warn",
                    code="unexpected_broker_open_order",
                    message=f"Broker has open order not tracked locally: {order_id}",
                    context={"order_id": order_id},
                )
            )

    return findings


def classify_fifo_position_drift(
    *,
    fifo_positions: dict[str, float],
    broker_positions: Iterable[Any],
    quantity_tolerance: float = 1e-6,
) -> list[ReconciliationFinding]:
    """Compare our INDEPENDENT FIFO-ledger positions against the broker (PL-2).

    ``classify_reconciliation`` compares the persisted ``positions`` table — which
    is itself overwritten with the broker snapshot each cycle — so it cannot catch
    our own books diverging from the broker. This compares the position derived
    from the append-only canonical ledger (``executions`` → FIFO) against the
    broker's reported position, surfacing drift the mirror check is blind to.

    Quantities are compared SIGNED (+ long, - short) so a side flip or magnitude
    gap both register. All findings are ``warn`` — drift is recorded/alerted for a
    human, not auto-blocking. Broker realized P&L is not reconciled here because no
    live broker reports it today (it would only ever be computed-vs-0).
    """
    broker_by_symbol = _positions_by_symbol(broker_positions)
    broker_signed = {
        symbol: (pos["quantity"] if pos["side"] == "long" else -pos["quantity"])
        for symbol, pos in broker_by_symbol.items()
    }
    # Canonicalize the FIFO-ledger keys too. They come from the instrument
    # catalogue, but normalization keeps reconciliation robust to external
    # broker separator spelling.
    fifo_signed: dict[str, float] = {}
    for raw_symbol, qty in fifo_positions.items():
        key = _symbol_key(raw_symbol)
        fifo_signed[key] = fifo_signed.get(key, 0.0) + float(qty)
    findings: list[ReconciliationFinding] = []
    for symbol in sorted(set(fifo_signed) | set(broker_signed)):
        fifo_qty = float(fifo_signed.get(symbol, 0.0))
        broker_qty = float(broker_signed.get(symbol, 0.0))
        if abs(fifo_qty - broker_qty) > quantity_tolerance:
            findings.append(
                ReconciliationFinding(
                    severity="warn",
                    code="fifo_position_quantity_drift",
                    message=f"FIFO ledger position disagrees with broker for {symbol}",
                    context={
                        "symbol": symbol,
                        "fifo_quantity": fifo_qty,
                        "broker_quantity": broker_qty,
                        "drift": fifo_qty - broker_qty,
                    },
                )
            )
    return findings


class ReconciliationWorker:
    """Periodic worker that reconciles broker state with persisted execution state."""

    def __init__(
        self,
        *,
        engine: Any,
        position_store: Any,
        risk_breach_store: Any | None,
        interval_sec: int,
        pnl_service: Any | None = None,
        position_drift_tolerance: float = 1e-6,
        pending_order_repo: Any | None = None,
        canonical_execution_store: Any | None = None,
        account_serializer: AccountExecutionSerializer | None = None,
    ) -> None:
        self._engine = engine
        self._position_store = position_store
        self._risk_breach_store = risk_breach_store
        self._interval_sec = max(1, interval_sec)
        # Independent FIFO-ledger-vs-broker position drift check (PL-2). Optional:
        # when no pnl_service is wired the worker behaves exactly as before.
        self._pnl_service = pnl_service
        self._position_drift_tolerance = position_drift_tolerance
        # Terminal-sync for stale pending_orders rows (warn-flood fix): the
        # submit path is the ONLY other status writer, so a row stranded in
        # 'submitted' re-flags missing_broker_open_order every cycle forever.
        # Falls back to the engine's repository so main.py wiring is unchanged
        # (same getattr pattern main.py uses for the position/breach stores).
        self._pending_order_repo = (
            pending_order_repo
            if pending_order_repo is not None
            else getattr(engine, "_pending_order_repo", None)
        )
        tracker = getattr(engine, "_reconciliation_tracker", None)
        self._canonical_execution_store = (
            canonical_execution_store
            if canonical_execution_store is not None
            else (
                getattr(engine, "_canonical_execution_store", None)
                or getattr(tracker, "canonical_execution_store", None)
            )
        )
        # Order ids already terminal-synced this process: suppresses re-emission
        # when a stale in-memory pending-order cache still echoes the row.
        self._terminal_synced_order_ids: set[str] = set()
        self._account_serializer = (
            account_serializer
            or getattr(engine, "account_execution_serializer", None)
            or AccountExecutionSerializer(getattr(engine, "_session_factory", None))
        )
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_sec)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop_event.set()

    async def _fifo_position_findings(
        self,
        *,
        user_id: str,
        broker_code: str,
        environment: str,
        account_id: int | None,
        broker_positions: Iterable[Any],
    ) -> list[ReconciliationFinding]:
        """FIFO-ledger-vs-broker position drift (PL-2); [] when disabled or on error.

        The FIFO ledger is scoped to the broker + run-environment being reconciled
        so fills on another broker (or in paper) don't pollute the comparison.
        Failure-tolerant: a P&L-compute failure must NOT abort the reconciliation
        cycle (which would open the circuit breaker) — drift visibility is
        best-effort, so the whole computation (including the classify call) is
        guarded and degrades to no findings.
        """
        if self._pnl_service is None:
            return []
        try:
            fifo_positions = await self._pnl_service.get_fifo_positions(
                user_id=user_id,
                broker=broker_code,
                mode=environment,
                account_id=account_id,
            )
            return classify_fifo_position_drift(
                fifo_positions={symbol: float(qty) for symbol, qty in fifo_positions.items()},
                broker_positions=broker_positions,
                quantity_tolerance=self._position_drift_tolerance,
            )
        except (
            ArithmeticError,
            CurrencyConversionRequiredError,
            PnLLedgerUnavailableError,
            TypeError,
            ValueError,
        ):
            # Best-effort boundary: this optional drift check must NEVER abort the
            # reconciliation cycle (the caller's except would open the circuit
            # breaker and halt trading). Known ledger, currency, and normalization
            # failures degrade to no findings; programmer defects still fail fast.
            logger.exception("FIFO position drift check failed", user_id=user_id)
            return []

    async def _stale_order_terminal_result(  # noqa: PLR0911
        self,
        *,
        broker: Any,
        payload: dict[str, Any],
        order_key: str,
        environment: str,
    ) -> BrokerOrderResult | None:
        """Fetch the terminal result for a tracked order missing from the broker book.

        Returns the full broker result to persist, or ``None`` when the
        disappearance is NOT definitive (live transient API error) and the warn
        must keep firing. Live safety: an order is only terminal-synced on a
        definitive broker response — terminal state or order-unknown; unknown maps
        to ``expired``. Legacy paper orders outside the durable committed-candle
        lifecycle still use the process-local PaperBroker book and expire when
        that broker definitively does not know them. Lifecycle-owned paper orders
        bypass this method because their durable ``pending_orders`` row remains
        authoritative across a process restart.
        """
        is_paper = str(environment).lower() == _PAPER_ENVIRONMENT
        if str(payload.get("status") or "") == "submission_unknown":
            client_order_id = str(payload.get("client_order_id") or "").strip()
            lookup = getattr(broker, "get_order_by_client_order_id", None)
            if not client_order_id or not callable(lookup):
                # Absence of a certified client-identity lookup is ambiguity,
                # never evidence that the broker rejected the order.
                return None
            lookup_succeeded, result = await self._safe_order_lookup(
                lookup,
                client_order_id,
                log_message="Client-order identity lookup failed during reconciliation",
                log_context={"client_order_id": client_order_id},
            )
            if not lookup_succeeded or result is None:
                return None
            if result.status in _TERMINAL_ORDER_STATUSES or result.success:
                return result
            if _order_unknown_at_broker(result):
                return None
            return None
        broker_order_id = str(payload.get("broker_order_id") or order_key)
        get_order_status = getattr(broker, "get_order_status", None)
        if not callable(get_order_status):
            if not is_paper:
                return None
            return BrokerOrderResult(
                success=False,
                order_id=broker_order_id,
                status=OrderStatus.EXPIRED,
                error_message="Paper order is absent after broker state restart",
            )
        lookup_succeeded, result = await self._safe_order_lookup(
            get_order_status,
            broker_order_id,
            log_message="Pending-order status lookup failed during reconciliation",
            log_context={"order_id": order_key},
        )
        if not lookup_succeeded:
            # Transient API error — never terminal-sync a live order on it; the
            # recurring warn is the real signal. Lifecycle-owned paper orders
            # bypass this lookup; legacy paper orders still depend on this book.
            if not is_paper:
                return None
            return BrokerOrderResult(
                success=False,
                order_id=broker_order_id,
                status=OrderStatus.EXPIRED,
                error_message="Paper order status is unavailable after broker state restart",
            )
        if result is not None and result.success and result.status in _TERMINAL_ORDER_STATUSES:
            return result
        if _order_unknown_at_broker(result):
            return BrokerOrderResult(
                success=False,
                order_id=broker_order_id,
                status=OrderStatus.EXPIRED,
                error_message=getattr(result, "error_message", None)
                or "Order is unknown at broker",
                error_code=getattr(result, "error_code", None),
                raw_response=getattr(result, "raw_response", None),
            )
        if is_paper:
            return BrokerOrderResult(
                success=False,
                order_id=broker_order_id,
                status=OrderStatus.EXPIRED,
                error_message="Paper order is absent from the in-memory broker book",
            )
        return None

    @staticmethod
    async def _safe_order_lookup(
        lookup: Any,
        order_identity: str,
        *,
        log_message: str,
        log_context: dict[str, str],
    ) -> tuple[bool, BrokerOrderResult | None]:
        """Isolate the one open-ended broker/API exception boundary."""
        try:
            candidate = await lookup(order_identity)
        except Exception:
            logger.exception(log_message, **log_context)
            return False, None
        return True, cast(BrokerOrderResult | None, candidate)

    def _read_durable_paper_result(
        self,
        *,
        canonical_order_id: Any,
        environment: str,
    ) -> tuple[BrokerOrderResult, list[BrokerFill]] | None:
        """Return a canonical terminal paper result when one already committed."""
        if (
            str(environment).lower() != _PAPER_ENVIRONMENT
            or self._canonical_execution_store is None
            or canonical_order_id is None
        ):
            return None
        read_durable_result = getattr(
            self._canonical_execution_store,
            "read_durable_result",
            None,
        )
        if not callable(read_durable_result):
            msg = "Canonical execution store cannot read durable paper order state"
            raise TypeError(msg)
        candidate = cast(
            tuple[BrokerOrderResult, list[BrokerFill]] | None,
            read_durable_result(order_id=int(canonical_order_id)),
        )
        if candidate is not None and candidate[0].status in _TERMINAL_ORDER_STATUSES:
            return candidate
        return None

    async def _sync_stale_pending_orders(
        self,
        *,
        findings: list[ReconciliationFinding],
        broker: Any,
        pending_orders: dict[str, dict[str, Any]],
        environment: str,
    ) -> list[ReconciliationFinding]:
        """Terminal-sync stale pending orders behind ``missing_broker_open_order``.

        The submit path (``PendingOrderRepository.resolve``) is the only other
        status writer, so a row stranded in ``submitted``/``pending`` re-flags
        ``missing_broker_open_order`` — and writes a risk_breaches row — every
        cycle forever. When the broker definitively reports the order terminal
        (or does not know it at all), mark the row terminal so the finding is
        emitted exactly once, on the transition. Indeterminate lookups (live
        transient API errors) keep the warn untouched. No-op when no repository
        is wired (behavior identical to before).
        """
        if self._pending_order_repo is None:
            return findings
        kept: list[ReconciliationFinding] = []
        for finding in findings:
            if finding.code != "missing_broker_open_order":
                kept.append(finding)
                continue
            order_key = str(finding.context.get("order_id") or "")
            if order_key in self._terminal_synced_order_ids:
                # Already terminal-synced (stale in-memory cache echo) — the
                # transition was reported; do not re-warn or re-record.
                continue
            payload = pending_orders.get(order_key, {})
            canonical_order_id = payload.get("canonical_order_id")
            durable_result = self._read_durable_paper_result(
                canonical_order_id=canonical_order_id,
                environment=environment,
            )

            terminal_result: BrokerOrderResult | None
            exact_fills: list[BrokerFill]
            if durable_result is not None:
                terminal_result, exact_fills = durable_result
                if terminal_result.has_fill and not exact_fills:
                    msg = (
                        f"Canonical paper order {canonical_order_id} returned "
                        "filled durable recovery state without exact fills"
                    )
                    raise RuntimeError(msg)
            elif _is_durable_local_paper_order(
                payload,
                environment=environment,
            ):
                # The durable committed-candle worker owns this order. A fresh
                # PaperBroker intentionally rehydrates positions from canonical
                # fills, not its old process-local resting-order dictionary, so
                # absence from get_open_orders() is expected after restart.
                # Suppress the false drift finding and leave the recoverable row
                # untouched for stop/limit/OCO progression.
                logger.debug(
                    "Durable local-paper order remains lifecycle-owned",
                    order_id=order_key,
                    canonical_order_id=canonical_order_id,
                )
                continue
            else:
                terminal_result = await self._stale_order_terminal_result(
                    broker=broker,
                    payload=payload,
                    order_key=order_key,
                    environment=environment,
                )
                exact_fills = []
            if terminal_result is None:
                kept.append(finding)
                continue
            row_order_id = str(payload.get("order_id") or order_key)
            if self._canonical_execution_store is not None:
                if canonical_order_id is None:
                    msg = (
                        "Reconciled fill cannot be persisted without pending_orders."
                        "canonical_order_id"
                    )
                    raise RuntimeError(msg)
                if terminal_result.has_fill and not exact_fills:
                    broker_order_id = str(
                        terminal_result.order_id or payload.get("broker_order_id") or order_key
                    ).strip()
                    if not broker_order_id:
                        msg = "Reconciled broker fill omitted broker order identity"
                        raise RuntimeError(msg)
                    exact_fills = await broker.get_fills(broker_order_id)
                self._canonical_execution_store.apply_broker_result(
                    order_id=int(canonical_order_id),
                    result=terminal_result,
                    fills=exact_fills,
                    instr_id=(
                        int(payload["instr_id"]) if payload.get("instr_id") is not None else None
                    ),
                )
            # Close the recovery row only after the canonical fill/state write.
            # If canonical persistence fails, the row remains recoverable for a
            # later reconciliation attempt; repeated venue trade IDs are
            # idempotent in CanonicalExecutionStore.
            self._pending_order_repo.resolve(row_order_id, terminal_result)
            self._terminal_synced_order_ids.add(order_key)
            terminal_status = terminal_result.status.value
            finding.context["terminal_status"] = terminal_status
            finding.context["filled_quantity"] = terminal_result.filled_quantity
            finding.context["filled_price"] = terminal_result.filled_price
            finding.context["commission"] = str(terminal_result.commission)
            logger.info(
                "Terminal-synced stale pending order",
                order_id=order_key,
                status=terminal_status,
                environment=environment,
            )
            # First detection still emits the finding (transition visibility).
            kept.append(finding)
        return kept

    def _emit_finding(
        self,
        finding: ReconciliationFinding,
        *,
        user_id: str,
        strategy_id: str,
        broker_code: str,
        strategy_breaker_key: str,
        environment: str,
        broker_account_id: int | None,
    ) -> str | None:
        """Log, alert, and record one finding; return its message if it is a block.

        Blocks log at error + alert (``reconciliation_block``); FIFO position drift
        logs at warning + alerts (``reconciliation_position_drift``); other warns
        only log. Every finding is recorded to the risk-breach store when wired.
        """
        log_payload = {
            "severity": finding.severity,
            "code": finding.code,
            "breaker_key": strategy_breaker_key,
            **finding.context,
        }
        payload = {
            **finding.context,
            "strategy_id": strategy_id,
            "broker": broker_code,
            "breaker_key": strategy_breaker_key,
            "environment": environment,
            "broker_account_id": broker_account_id,
        }
        if finding.severity == "block":
            logger.error("Reconciliation finding", **log_payload)
            self._engine.emit_alert(
                event_type="reconciliation_block",
                severity="error",
                message=finding.message,
                payload=payload,
            )
        else:
            logger.warning("Reconciliation finding", **log_payload)
            # FIFO-ledger-vs-broker drift means our books disagree with the broker —
            # alert a human (warn) even though it does not open the breaker (PL-2).
            if finding.code == "fifo_position_quantity_drift":
                self._engine.emit_alert(
                    event_type="reconciliation_position_drift",
                    severity="warning",
                    message=finding.message,
                    payload=payload,
                )
        if self._risk_breach_store is not None:
            self._risk_breach_store.record(
                user_id=user_id,
                rule_code=finding.code,
                severity=finding.severity,
                context=payload,
                broker_account_id=broker_account_id,
            )
        return finding.message if finding.severity == "block" else None

    async def reconcile_once(self) -> None:  # noqa: PLR0912, PLR0915
        contexts = self._engine.iter_reconciliation_contexts()
        if not contexts:
            self._engine.mark_reconciliation_health(healthy=True, error=None)
            return

        errors: list[str] = []
        # FIFO drift is an account-level check; run it once per (user, broker,
        # environment) even when a user has several strategy contexts this cycle (PL-2).
        fifo_checked: set[tuple[str, int | None, str, str]] = set()
        for context in contexts:
            strategy_breaker_key = str(context["strategy_breaker_key"])
            broker_breaker_key = str(context["broker_breaker_key"])
            user_id = str(context["user_id"])
            strategy_id = str(context["strategy_id"])
            broker_code = str(context["broker_code"])
            environment = str(context.get("environment") or "").strip().lower()
            if environment not in {"paper", "live"}:
                msg = "Reconciliation requires an explicit paper or live environment"
                raise ValueError(msg)
            broker_account_id: int | None = None
            account_fence: Any | None = None
            account_fence_acquired = False
            try:
                broker_account_id = _normalize_reconciliation_account_id(
                    context.get("broker_account_id")
                )
                account_fence = self._account_serializer.hold(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    writer_kind="reconciliation",
                )
                await account_fence.__aenter__()
                account_fence_acquired = True
                broker = await self._engine.get_reconciliation_broker(context)
                if broker is None:
                    _raise_broker_unavailable(broker_code)
                if not broker.is_connected:
                    connected = await broker.connect()
                    if not connected:
                        _raise_broker_unavailable(broker_code)

                broker_positions = [
                    _normalize_position(position) for position in await broker.get_positions()
                ]
                broker_open_orders = []
                get_open_orders = getattr(broker, "get_open_orders", None)
                if callable(get_open_orders):
                    broker_open_orders = list(await get_open_orders())

                local_positions = self._position_store.list_positions(
                    user_id=user_id,
                    broker_code=broker_code,
                    environment=environment,
                    account_id=broker_account_id,
                )
                pending_orders_for_account = getattr(
                    self._engine,
                    "pending_orders_for_account",
                    None,
                )
                pending_orders = (
                    pending_orders_for_account(broker_account_id)
                    if callable(pending_orders_for_account)
                    else self._engine.pending_orders_for(strategy_breaker_key)
                )
                findings = classify_reconciliation(
                    local_positions=local_positions,
                    broker_positions=broker_positions,
                    pending_orders=pending_orders,
                    broker_open_orders=broker_open_orders,
                )
                # Terminal-sync stale pending orders so missing_broker_open_order
                # fires once on the transition instead of every cycle forever.
                findings = await self._sync_stale_pending_orders(
                    findings=findings,
                    broker=broker,
                    pending_orders=pending_orders,
                    environment=environment,
                )
                # PL-2: independent FIFO-ledger-vs-broker position drift. Captured
                # BEFORE sync_positions overwrites the local mirror with the broker
                # snapshot — otherwise the comparison degrades to broker-vs-broker.
                # The broker reports ACCOUNT-level positions, so compare against
                # user-level FIFO (all strategies) scoped to THIS broker+environment,
                # once per (user, broker, environment) cycle — a per-strategy FIFO
                # would false-drift on a symbol two strategies share, and an
                # unscoped ledger would mix in other brokers'/paper fills.
                fifo_key = (user_id, broker_account_id, broker_code, environment)
                if fifo_key not in fifo_checked:
                    fifo_checked.add(fifo_key)
                    findings.extend(
                        await self._fifo_position_findings(
                            user_id=user_id,
                            broker_code=broker_code,
                            environment=environment,
                            account_id=broker_account_id,
                            broker_positions=broker_positions,
                        )
                    )

                self._position_store.sync_positions(
                    user_id=user_id,
                    broker_code=broker_code,
                    positions=broker_positions,
                    environment=environment,
                    account_id=broker_account_id,
                )

                block_messages: list[str] = []
                for finding in findings:
                    block_msg = self._emit_finding(
                        finding,
                        user_id=user_id,
                        strategy_id=strategy_id,
                        broker_code=broker_code,
                        strategy_breaker_key=strategy_breaker_key,
                        environment=environment,
                        broker_account_id=broker_account_id,
                    )
                    if block_msg is not None:
                        block_messages.append(block_msg)

                if block_messages:
                    reason = "; ".join(block_messages)
                    self._engine.open_circuit_breaker(
                        scope="strategy",
                        breaker_key=strategy_breaker_key,
                        user_id=user_id,
                        strategy_id=strategy_id,
                        broker=broker_code,
                        environment=environment,
                        reason=reason,
                        broker_account_id=broker_account_id,
                    )
                    errors.append(f"{strategy_breaker_key}: {reason}")
            except AccountExecutionBusyError:
                continue
            except Exception as exc:
                logger.exception(
                    "Reconciliation cycle failed",
                    breaker_key=strategy_breaker_key,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    broker=broker_code,
                )
                if self._risk_breach_store is not None:
                    self._risk_breach_store.record(
                        user_id=user_id,
                        rule_code="reconciliation_error",
                        severity="block",
                        broker_account_id=broker_account_id,
                        context={
                            "strategy_id": strategy_id,
                            "broker": broker_code,
                            "breaker_key": strategy_breaker_key,
                            "environment": environment,
                            "error": str(exc),
                        },
                    )
                scope = (
                    "broker"
                    if self._engine.is_broker_system_error(error_message=str(exc))
                    else "strategy"
                )
                self._engine.open_circuit_breaker(
                    scope=scope,
                    breaker_key=broker_breaker_key if scope == "broker" else strategy_breaker_key,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    broker=broker_code,
                    environment=environment,
                    reason=f"reconciliation_error: {exc}",
                    broker_account_id=broker_account_id,
                )
                errors.append(f"{strategy_breaker_key}: {exc}")
            finally:
                if account_fence_acquired and account_fence is not None:
                    await account_fence.__aexit__(None, None, None)

        self._engine.mark_reconciliation_health(
            healthy=not errors,
            error="; ".join(errors) if errors else None,
        )
