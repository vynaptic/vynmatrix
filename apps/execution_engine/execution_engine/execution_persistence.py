"""Persistence and result-event emission for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
third step of the Phase-3 ExecutionEngine decomposition. This module owns the
write-side of execution: logging completed executions, recording metrics,
syncing positions, persisting daily NAV snapshots, and emitting the
``ExecutionResultEvent`` to the outbox.

It is intentionally narrow:

* It does not pick brokers (``BrokerResolver``).
* It does not fetch state (``StateProvider``).
* It does not orchestrate ``handle_signal``; the engine still owns the call
  site so persistence runs in the same dedup boundary as before.

``ExecutionEngine`` keeps the same private method names
(``_persist_execution``, ``_emit_execution_result_event``,
``_build_metric_position_state``, ``_update_daily_nav_snapshot``) as thin
delegates so existing tests that patch those names continue to work.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from lib_common.internal_events import (
    BrokerRouteSnapshot,
    ExecutionPolicySnapshot,
    ExecutionResultEvent,
    ScoreContextSnapshot,
)
from lib_common.logging import get_logger
from lib_common.observability import ensure_run_id
from lib_strategy.signals.signal import Signal

from .brokers.base import position_gross_notional
from .config import AccountState
from .execution_result import ExecutionResult, execution_log_status
from .metrics.normalization import normalize_position_side
from .metrics.pnl_service import PnLService
from .models import OptionsIntent, OrderIntent

logger = get_logger(__name__)

EnvironmentResolver = Callable[..., str]
"""Callable signature ``(mode: str, profile: Dict[str, Any]) -> str``.

Injected by the engine so the persistence module does not need to depend on
``ExecutionEngine`` to compute the position-store environment label.
"""


class ExecutionPersistence:
    """Owns the write-side of execution: logs, metrics, positions, NAV, events."""

    def __init__(
        self,
        *,
        execution_log_store: Any | None = None,
        execution_metrics_store: Any | None = None,
        execution_position_store: Any | None = None,
        outbox_store: Any | None = None,
        pnl_service: PnLService | None = None,
        session_factory: Any | None = None,
        environment_resolver: EnvironmentResolver | None = None,
    ) -> None:
        self._execution_log_store = execution_log_store
        self._execution_metrics_store = execution_metrics_store
        self._execution_position_store = execution_position_store
        self._outbox_store = outbox_store
        self._pnl_service = pnl_service
        self._session_factory = session_factory
        self._environment_resolver = environment_resolver

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    @staticmethod
    def _commission_currency(result: ExecutionResult) -> str | None:
        """Return the single currency attached to broker fees or rebates.

        A numeric fee without its currency is not valid accounting data.  Mixed
        fee currencies likewise cannot be represented by the aggregate
        ``total_commission`` column, so both cases fail closed before metrics
        persistence instead of silently adding unlike monetary amounts.
        """
        currencies: set[str] = set()
        for item in result.order_results:
            try:
                commission = Decimal(str(item.get("commission") or 0))
            except (ArithmeticError, TypeError, ValueError):
                msg = "Broker result contains an invalid commission amount"
                raise ValueError(msg) from None
            currency = str(item.get("commission_currency") or "").strip().upper()
            if commission != 0:
                if not currency:
                    msg = "Non-zero broker fee or rebate is missing its currency"
                    raise ValueError(msg)
                currencies.add(currency)
        if len(currencies) > 1:
            msg = "Execution result contains commissions in multiple currencies"
            raise ValueError(msg)
        if result.total_commission != 0 and not currencies:
            msg = "Aggregate broker fee or rebate has no currency-bearing order result"
            raise ValueError(msg)
        return next(iter(currencies), None)

    @staticmethod
    def _positions_to_persist(
        snapshot: list[dict[str, Any]] | None,
        signal: Signal,
        result: ExecutionResult,
    ) -> tuple[list[dict[str, Any]], bool]:
        return ExecutionPersistence._positions_to_persist_context(
            snapshot,
            symbol=signal.symbol,
            signal_action=signal.action.value,
            result=result,
        )

    @staticmethod
    def _positions_to_persist_context(
        snapshot: list[dict[str, Any]] | None,
        *,
        symbol: str,
        signal_action: str,
        result: ExecutionResult,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(positions_to_sync, allow_empty_prune)`` for the post-fill sync.

        The broker book reflects a fill only after a settlement lag (seconds on
        Coinbase), while persist runs immediately post-trade — so a just-opened position
        is often absent from the snapshot. Record it from the deterministic FILL result
        (symbol/qty/avg price) when missing, and never let a transiently-empty snapshot
        prune a just-opened position. Otherwise reconciliation false-phantoms every live
        fill (→ breaker) and the position ledger (which max-open-positions relies on)
        stays empty. An empty snapshot legitimately means flat only when we did NOT open.
        """
        positions = list(snapshot or [])
        opened = (
            result.orders_filled > 0
            and result.total_quantity > 0
            and signal_action.upper() in {"LONG", "SHORT"}
        )
        if opened and not any(str(p.get("symbol")) == symbol for p in positions):
            if result.execution_mode not in {"paper", "spot"}:
                logger.error(
                    "Broker snapshot has not reflected a derivative fill; "
                    "refusing to infer contract valuation",
                    symbol=symbol,
                    execution_mode=result.execution_mode,
                )
            else:
                currency = str(result.settlement_currency or "").strip().upper()
                if not currency or not currency.isalnum():
                    msg = "Filled asset position has no settlement currency"
                    raise ValueError(msg)
                quantity = Decimal(str(result.total_quantity))
                price = Decimal(str(result.average_price))
                if quantity <= 0 or price <= 0:
                    msg = "Filled asset position has invalid fill economics"
                    raise ValueError(msg)
                positions.append(
                    {
                        "symbol": symbol,
                        "side": "long" if signal_action.upper() == "LONG" else "short",
                        "quantity": quantity,
                        "entry_price": price,
                        "current_price": price,
                        "quantity_unit": "asset",
                        "contract_multiplier": None,
                        "gross_notional": abs(quantity * price),
                        "notional_currency": currency,
                    }
                )
        return positions, not opened

    def persist(
        self,
        *,
        user_id: str,
        signal: Signal,
        result: ExecutionResult,
        score_context: dict[str, Any] | None,
        account_state: AccountState | None,
        profile: dict[str, Any] | None,
        intents: list[OrderIntent | OptionsIntent] | None,
    ) -> str | None:
        """Persist execution artifacts and return the committed execution-log ID."""
        profile = profile or {}
        user_strategy_config = dict(profile.get("_execution_user_strategy_config") or {})
        run_id = ensure_run_id(signal.run_id)
        details = result.to_dict()
        details["score_context"] = score_context or {}
        signal_payload = signal.to_dict()
        signal_payload.pop("run_id", None)
        details["signal"] = signal_payload
        details["execution_policy_snapshot"] = user_strategy_config.get(
            "_execution_policy_snapshot"
        ) or profile.get("_execution_policy_snapshot")
        details["broker_route_snapshot"] = profile.get("_broker_route_snapshot")
        details["risk_baseline"] = profile.get("_risk_baseline_audit")
        status = execution_log_status(result)
        canonical_signal_id = self._extract_canonical_signal_id(signal)

        # Build the result event once, then co-commit it with the execution_logs
        # row (transactional outbox) so a crash cannot persist the trade record
        # without its downstream result event, or vice-versa.
        event_message = self._build_result_event_message(
            user_id=user_id,
            signal=signal,
            result=result,
            score_context=score_context,
            profile=profile,
            user_strategy_config=user_strategy_config,
        )
        event_committed_with_log = False
        execution_log_id: str | None = None

        if self._execution_log_store is not None:
            try:
                broker_account_id = self._require_broker_account_id(
                    result,
                    artifact="Execution log",
                )
                execution_log_id = self._execution_log_store.log(
                    user_id=user_id,
                    account_id=broker_account_id,
                    strategy_id=signal.strategy_id,
                    canonical_signal_id=canonical_signal_id,
                    signal_type=signal.action.value.lower(),
                    execution_mode=result.execution_mode,
                    status=status,
                    details=details,
                    error_message=result.error_message,
                    run_id=run_id,
                    outbox_store=self._outbox_store,
                    event_message=event_message,
                )
                event_committed_with_log = event_message is not None
            except Exception:
                # Boundary catch: log persistence is best-effort. The signal
                # has already executed; failure here cannot unwind the broker
                # action, so we surface the traceback and continue.
                logger.exception(
                    "Failed to persist execution log",
                    signal_id=result.signal_id,
                    run_id=run_id,
                )

        # If the event was not co-committed with a log row (no log store, or the
        # atomic write rolled back), emit it standalone so downstream consumers
        # still receive it. enqueue is idempotent on event_key.
        if event_message is not None and not event_committed_with_log:
            self._emit_result_message_standalone(event_message, signal_id=result.signal_id)

        if self._execution_metrics_store is None:
            return execution_log_id

        try:
            if account_state is None:
                return execution_log_id
            reference_price = signal.entry_price
            if reference_price is None:
                reference_price = signal.metadata.get("reference_price")
            self._persist_metrics_and_positions(
                user_id=user_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                asset_class=signal.asset_class,
                canonical_signal_id=canonical_signal_id,
                signal_id=result.signal_id,
                run_id=run_id,
                signal_action=signal.action.value.lower(),
                reference_price=reference_price,
                result=result,
                account_state=account_state,
                profile=profile,
                intents=intents,
                score_context=score_context,
            )
        except Exception:
            # Boundary catch: any metrics/position-store failure must not
            # block the event emission below — the downstream feedback loop
            # depends on ExecutionResultEvent landing in the outbox.
            logger.exception(
                "Failed to persist execution metrics",
                signal_id=result.signal_id,
                run_id=run_id,
            )
        # NOTE: the ExecutionResultEvent was already enqueued atomically with the
        # execution_logs row above (transactional outbox), so it is not emitted
        # again here.
        return execution_log_id

    def persist_paper_fill_projections(
        self,
        *,
        user_id: str,
        strategy_id: str,
        symbol: str,
        asset_class: str | None,
        canonical_signal_id: int,
        canonical_execution_id: int,
        signal_id: str,
        run_id: str | None,
        signal_action: str,
        reference_price: float | None,
        result: ExecutionResult,
        account_state: AccountState,
        profile: dict[str, Any],
    ) -> str:
        """Persist one durable local-paper fill's rebuildable projections.

        The canonical fill and its result event are already committed by the
        lifecycle worker. This entry point deliberately writes no execution log
        or result event; it reuses the normal metrics/position implementation
        with a deterministic metric identity so checkpoint replay is harmless.
        """
        if self._execution_metrics_store is None:
            msg = "Durable paper fills require execution metrics persistence"
            raise RuntimeError(msg)
        if (
            isinstance(canonical_execution_id, bool)
            or not isinstance(canonical_execution_id, int)
            or canonical_execution_id <= 0
        ):
            msg = "Paper fill projection requires a positive canonical execution ID"
            raise ValueError(msg)
        if result.orders_filled != 1 or result.total_quantity <= 0:
            msg = "Paper fill projection requires exactly one positive exact fill"
            raise ValueError(msg)
        metric_id = f"paper-fill-exec:{canonical_execution_id}"
        persisted_metric_id = self._persist_metrics_and_positions(
            user_id=user_id,
            strategy_id=strategy_id,
            symbol=symbol,
            asset_class=asset_class,
            canonical_signal_id=canonical_signal_id,
            signal_id=signal_id,
            run_id=ensure_run_id(run_id),
            signal_action=signal_action,
            reference_price=reference_price,
            result=result,
            account_state=account_state,
            profile=profile,
            intents=None,
            score_context=None,
            canonical_execution_id=canonical_execution_id,
            metric_id=metric_id,
        )
        if persisted_metric_id is None:
            msg = f"Paper fill projection {canonical_execution_id} did not persist a metric"
            raise RuntimeError(msg)
        return persisted_metric_id

    def _persist_metrics_and_positions(  # noqa: PLR0912, PLR0915
        self,
        *,
        user_id: str,
        strategy_id: str,
        symbol: str,
        asset_class: str | None,
        canonical_signal_id: int | None,
        signal_id: str,
        run_id: str | None,
        signal_action: str,
        reference_price: float | None,
        result: ExecutionResult,
        account_state: AccountState,
        profile: dict[str, Any],
        intents: list[OrderIntent | OptionsIntent] | None,
        score_context: dict[str, Any] | None,
        canonical_execution_id: int | None = None,
        metric_id: str | None = None,
    ) -> str | None:
        """Persist the shared execution-metrics and position projections."""
        if self._execution_metrics_store is None:
            return None
        broker_account_id = self._require_broker_account_id(
            result,
            artifact="Execution metrics",
        )
        self._require_settlement_currency(result)

        # Each partition receives only its own cumulative FIFO realized P&L.
        # Lifecycle snapshots additionally retain contributions from the exact
        # canonical fill being projected, not every partial fill sharing the
        # parent signal identity.
        realized_contributions: list[Any] = []
        metrics_realized: float | None
        if self._pnl_service is not None and self._session_factory is not None:
            if result.orders_filled > 0 and canonical_signal_id is not None:
                partition_realized, realized_contributions = (
                    self._pnl_service.partition_realized_pnl_with_exit_contributions(
                        user_id,
                        symbol,
                        strategy_id=strategy_id,
                        account_id=broker_account_id,
                        exit_canonical_signal_id=canonical_signal_id,
                    )
                )
                if canonical_execution_id is not None:
                    realized_contributions = [
                        contribution
                        for contribution in realized_contributions
                        if contribution.exit_exec_id == canonical_execution_id
                    ]
            else:
                partition_realized = self._pnl_service.partition_realized_pnl(
                    user_id,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    account_id=broker_account_id,
                )
            metrics_realized = float(partition_realized)
        else:
            metrics_realized = account_state.realized_pnl

        position_state_override = self._build_metric_position_state(
            account_state=account_state,
            symbol=symbol,
            realized_pnl=metrics_realized,
        )
        trade_price = result.average_price if result.average_price > 0 else None
        trade_qty = result.total_quantity if result.total_quantity > 0 else None
        options_meta: dict[str, Any] = {}
        options_exposure = 0.0
        options_margin_used = 0.0
        options_spreads: list[dict[str, Any]] = []
        if intents:
            for intent in intents:
                if not isinstance(intent, OptionsIntent):
                    continue
                meta = dict(intent.metadata or {})
                net_debit = meta.get("net_debit")
                max_loss = intent.max_loss or meta.get("max_loss")
                max_profit = intent.max_profit or meta.get("max_profit")
                breakeven = intent.breakeven or meta.get("breakeven")
                quantity = float(intent.quantity or 0)

                if trade_price is None and net_debit is not None:
                    per_unit = abs(float(net_debit)) / (quantity if quantity > 0 else 1.0)
                    trade_price = per_unit if per_unit > 0 else trade_price
                    if trade_qty is None:
                        trade_qty = quantity if quantity > 0 else 1.0

                exposure_value = None
                if max_loss is not None:
                    exposure_value = abs(float(max_loss))
                elif net_debit is not None:
                    exposure_value = abs(float(net_debit))

                if exposure_value is not None:
                    options_exposure += exposure_value
                    options_margin_used += exposure_value

                options_spreads.append(
                    {
                        "spread_type": intent.spread_type,
                        "net_debit": net_debit,
                        "max_profit": max_profit,
                        "max_loss": max_loss,
                        "breakeven": breakeven,
                        "quantity": quantity,
                    }
                )

        exposure = None
        if account_state.positions:
            exposure = float(
                sum(
                    (position_gross_notional(position) for position in account_state.positions),
                    Decimal("0"),
                )
            )
        if options_exposure:
            exposure = (exposure or 0.0) + options_exposure

        leverage = None
        if exposure is not None and account_state.equity:
            try:
                leverage = float(exposure) / float(account_state.equity)
            except (TypeError, ValueError, ZeroDivisionError):
                leverage = None

        if options_spreads:
            options_meta.update(
                {
                    "spreads": options_spreads,
                    "exposure": options_exposure,
                    "margin_used": options_margin_used,
                }
            )
        metadata = {
            "score_context": score_context or {},
            "order_results": result.order_results,
            "open_positions": account_state.open_positions,
            "daily_trades": account_state.daily_trades,
            "options": options_meta or None,
            "realized_pnl_contributions": [
                {
                    "entry_exec_id": contribution.entry_exec_id,
                    "exit_exec_id": contribution.exit_exec_id,
                    "entry_canonical_signal_id": contribution.entry_canonical_signal_id,
                    "exit_canonical_signal_id": contribution.exit_canonical_signal_id,
                    "quantity": str(contribution.quantity),
                    "realized_pnl": str(contribution.net_realized_pnl),
                    "deployed_capital": str(contribution.deployed_entry_capital),
                    "account_currency": contribution.account_currency,
                    "exit_time": contribution.exit_time.isoformat(),
                }
                for contribution in realized_contributions
            ],
            "risk_baseline": profile.get("_risk_baseline_audit"),
        }
        if canonical_execution_id is not None:
            metadata["canonical_execution_id"] = canonical_execution_id

        raw_metric_id = self._execution_metrics_store.record(
            user_id=user_id,
            account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            execution_mode=result.execution_mode,
            broker=result.broker,
            settlement_currency=result.settlement_currency,
            signal_id=signal_id,
            run_id=run_id,
            asset_class=asset_class,
            equity=account_state.equity,
            available_cash=account_state.available_cash,
            margin_used=account_state.margin_used,
            unrealized_pnl=account_state.unrealized_pnl,
            realized_pnl=metrics_realized,
            position_state_override=position_state_override,
            signal_action=signal_action,
            trade_price=trade_price,
            trade_qty=trade_qty,
            reference_price=reference_price,
            orders_submitted=result.orders_submitted,
            orders_filled=result.orders_filled,
            total_commission=result.total_commission,
            commission_currency=self._commission_currency(result),
            exposure=exposure,
            leverage=leverage,
            metadata=metadata,
            created_at=result.executed_at,
            metric_id=metric_id,
        )
        if not isinstance(raw_metric_id, str) or not raw_metric_id.strip():
            msg = "Execution metrics persistence returned an invalid metric identity"
            raise RuntimeError(msg)
        persisted_metric_id = raw_metric_id.strip()
        if metric_id is not None and persisted_metric_id != metric_id:
            msg = f"Execution metric identity changed from {metric_id} to {persisted_metric_id}"
            raise RuntimeError(msg)
        if self._execution_position_store is not None:
            position_environment = self._resolve_environment(
                mode=result.execution_mode,
                profile=profile,
            )
            positions_to_sync, allow_prune = self._positions_to_persist_context(
                account_state.positions,
                symbol=symbol,
                signal_action=signal_action,
                result=result,
            )
            self._execution_position_store.sync_positions(
                user_id=user_id,
                broker_code=result.broker,
                positions=positions_to_sync,
                environment=position_environment,
                account_id=broker_account_id,
                allow_empty_prune=allow_prune,
            )
        return persisted_metric_id

    async def update_daily_nav_snapshot(
        self,
        *,
        user_id: str,
        account_state: AccountState | None,
        account_id: int,
    ) -> None:
        """Persist today's account-level NAV snapshot for ``user_id``."""
        if self._session_factory is None or account_state is None or self._pnl_service is None:
            return

        try:
            await self._pnl_service.persist_daily_nav(
                user_id=user_id,
                equity=Decimal(str(account_state.equity or 0.0)),
                account_id=account_id,
                nav_ccy=account_state.currency,
            )
        except Exception:
            # Boundary catch: NAV persistence is best-effort observability.
            logger.exception("Failed to persist daily NAV snapshot", user_id=user_id)

    # ------------------------------------------------------------------
    # Internals (kept public so engine delegates can forward 1-for-1).
    # ------------------------------------------------------------------

    @staticmethod
    def _require_account_id(value: int | None) -> int:
        if value is None:
            msg = "Position persistence requires broker_account_id"
            raise ValueError(msg)
        return value

    @staticmethod
    def _extract_canonical_signal_id(signal: Signal) -> int | None:
        meta = signal.metadata or {}
        if not isinstance(meta, dict):
            return None
        raw = meta.get("canonical_signal_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _resolve_environment(self, *, mode: str, profile: dict[str, Any]) -> str:
        if self._environment_resolver is None:
            # Defensive default mirrors the engine's old behaviour when no
            # resolver is wired (mostly: paper).
            return "paper"
        return self._environment_resolver(mode=mode, profile=profile)

    @staticmethod
    def _build_metric_position_state(
        *,
        account_state: AccountState,
        symbol: str,
        realized_pnl: float | None = None,
    ) -> dict[str, float]:
        """Aggregate per-symbol position state for the metrics store.

        ``realized_pnl`` is the per-(strategy, symbol) cumulative realized P&L the
        metrics store diffs across snapshots (PL-1); it defaults to the account
        figure for callers that do not compute a partition value.
        """
        signed_qty = 0.0
        entry_weight = 0.0
        current_weight = 0.0

        for position in account_state.positions:
            if str(position.get("symbol") or "") != symbol:
                continue
            qty = float(position.get("quantity") or 0.0)
            if qty <= 0:
                continue
            side = normalize_position_side(position.get("side"))
            signed_component = qty if side == "long" else -qty
            signed_qty += signed_component
            entry_weight += abs(signed_component) * float(position.get("entry_price") or 0.0)
            current_weight += abs(signed_component) * float(position.get("current_price") or 0.0)

        abs_qty = abs(signed_qty)
        avg_price = entry_weight / abs_qty if abs_qty > 0 else 0.0
        current_price = current_weight / abs_qty if abs_qty > 0 else 0.0
        effective_realized = (
            realized_pnl if realized_pnl is not None else account_state.realized_pnl
        )
        realized = float(effective_realized) if effective_realized is not None else 0.0

        return {
            "quantity": signed_qty,
            "avg_price": avg_price,
            "current_price": current_price,
            "realized_pnl": realized,
        }

    def _build_result_event_message(
        self,
        *,
        user_id: str,
        signal: Signal,
        result: ExecutionResult,
        score_context: dict[str, Any] | None,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build the outbox enqueue kwargs for the ExecutionResultEvent.

        Returns None when the outbox is unwired. The returned dict is suitable
        for both ``OutboxStore.enqueue`` and ``enqueue_on_session`` (the latter
        co-commits it with the execution_logs row — see persist()).
        """
        if self._outbox_store is None:
            return None

        score_snapshot = None
        if score_context:
            recommended_mode = score_context.get("recommended_mode")
            score_snapshot = ScoreContextSnapshot(
                asset_score=float(score_context.get("asset_score", 0.0)),
                sector_score=(
                    float(score_context["sector_score"])
                    if score_context.get("sector_score") is not None
                    else None
                ),
                market_score=(
                    float(score_context["market_score"])
                    if score_context.get("market_score") is not None
                    else None
                ),
                recommended_mode=(str(recommended_mode) if recommended_mode is not None else None),
                score_scope=str(score_context.get("score_scope") or "asset"),
                score_target=str(score_context.get("score_target") or signal.symbol),
            )

        policy_snapshot_payload = (
            user_strategy_config.get("_execution_policy_snapshot")
            or profile.get("_execution_policy_snapshot")
            or {}
        )
        route_snapshot_payload = profile.get("_broker_route_snapshot") or {
            "broker": result.broker,
            "broker_account_id": result.broker_account_id,
            "broker_environment": profile.get("broker_environment"),
            "allowed_brokers": (
                profile.get("linked_brokers") or profile.get("allowed_brokers") or []
            ),
            "route_source": "execution_engine",
            "live_enabled": bool(profile.get("live_enabled")),
            "sandbox": bool(profile.get("sandbox", result.execution_mode != "live")),
            "asset_class": signal.asset_class,
            "execution_mode": result.execution_mode,
        }

        event = ExecutionResultEvent(
            run_id=signal.run_id,
            correlation_id=signal.signal_id,
            # The execution command that produced this result (set by the API from
            # the inbound ExecutionCommandEvent.event_id) is the direct cause.
            causation_id=user_strategy_config.get("_causation_event_id"),
            producer="execution_engine",
            user_id=user_id,
            signal_id=result.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            success=result.success,
            status=execution_log_status(result),
            execution_mode=result.execution_mode,
            broker=result.broker,
            broker_account_id=self._require_broker_account_id(
                result,
                artifact="execution result event",
            ),
            orders_submitted=result.orders_submitted,
            orders_filled=result.orders_filled,
            total_quantity=result.total_quantity,
            average_price=result.average_price,
            total_commission=result.total_commission,
            error_message=result.error_message,
            executed_at=result.executed_at,
            score_context=score_snapshot,
            execution_policy=(
                ExecutionPolicySnapshot(**policy_snapshot_payload)
                if policy_snapshot_payload
                else None
            ),
            broker_route=BrokerRouteSnapshot(**route_snapshot_payload),
            order_results=[dict(item) for item in result.order_results],
        )
        return {
            "topic": event.topic,
            "event_type": event.event_type,
            "payload": event.model_dump(mode="json"),
            "schema_version": event.schema_version,
            "aggregate_type": "execution_result",
            "aggregate_id": f"{signal.signal_id}:{user_id}",
            "event_key": f"execution-result:{signal.signal_id}:{user_id}",
            "ordering_key": user_id,
            "headers": {"run_id": signal.run_id or ""},
        }

    def _emit_result_message_standalone(
        self, event_message: dict[str, Any], *, signal_id: str
    ) -> None:
        """Enqueue a prebuilt result-event message in its OWN transaction.

        Fallback used only when there is no execution_logs row to co-commit the
        event with (the atomic path lives in persist() via ExecutionLogStore.log).
        """
        if self._outbox_store is None:
            return
        try:
            self._outbox_store.enqueue(**event_message)
        except Exception:
            logger.exception("Failed to enqueue execution result event", signal_id=signal_id)

    @staticmethod
    def _require_broker_account_id(result: ExecutionResult, *, artifact: str) -> int:
        """Return the account identity required by an account-scoped artifact."""
        if result.broker_account_id is None:
            msg = f"{artifact} persistence requires broker_account_id"
            raise ValueError(msg)
        return result.broker_account_id

    @staticmethod
    def _require_settlement_currency(result: ExecutionResult) -> str:
        """Return the settlement currency required by execution metrics."""
        if result.settlement_currency is None:
            msg = "Execution metrics persistence requires settlement_currency"
            raise ValueError(msg)
        return result.settlement_currency
