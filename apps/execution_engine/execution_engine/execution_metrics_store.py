"""Execution-metrics persistence store (performance + position-state snapshots)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lib_application.db.models import ExecutionMetric
from lib_data.market_data import normalize_product_symbol

from ._persistence_common import (
    _DECIMAL_ZERO,
    _ensure_strategy_exists,
    _require_positive_account_id,
    _resolve_session_factory,
)


class ExecutionMetricsStore:
    """Persist execution performance snapshots for monitoring."""

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

    def record(  # noqa: PLR0912, PLR0915
        self,
        *,
        user_id: str,
        account_id: int,
        strategy_id: str,
        symbol: str,
        execution_mode: str,
        broker: str,
        settlement_currency: str,
        signal_id: str | None,
        run_id: str | None,
        asset_class: str | None,
        equity: float | None,
        available_cash: float | None,
        margin_used: float | None,
        unrealized_pnl: float | None,
        realized_pnl: float | None,
        position_state_override: dict[str, Any] | None = None,
        signal_action: str | None = None,
        trade_price: float | None = None,
        trade_qty: float | None = None,
        reference_price: float | None = None,
        orders_submitted: int,
        orders_filled: int,
        total_commission: float,
        commission_currency: str | None,
        exposure: float | None = None,
        leverage: float | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        metric_id: str | None = None,
    ) -> str:
        account_id = _require_positive_account_id(account_id)
        resolved_metric_id = str(metric_id or uuid.uuid4()).strip()
        if not resolved_metric_id or len(resolved_metric_id) > 50:  # noqa: PLR2004
            msg = "Execution metric identity must contain 1-50 characters"
            raise ValueError(msg)
        normalized_settlement = str(settlement_currency or "").strip().upper()
        if not normalized_settlement or not normalized_settlement.isalnum():
            msg = "Execution metrics require settlement_currency"
            raise ValueError(msg)
        # Normalize the symbol so a per-(user,strategy,symbol,mode) partition is not
        # split across exchange spellings (M7): the raw signal.symbol could be
        # 'BTC-USD' on one fill and 'BTCUSD' on another, fragmenting the cumulative
        # realized-P&L carry-forward (the `previous` lookup below keys on symbol).
        symbol = normalize_product_symbol(symbol)
        _ensure_strategy_exists(
            self._session_factory,
            strategy_id=strategy_id,
            strategy_name=strategy_id,
            asset_class=asset_class,
        )
        with self._session_factory() as session:
            existing_metric = session.get(ExecutionMetric, resolved_metric_id)
            if existing_metric is not None:
                existing_metadata = dict(existing_metric.metadata_json or {})
                requested_identity = {
                    "canonical_execution_id": (metadata or {}).get("canonical_execution_id"),
                    "user_id": user_id,
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "execution_mode": execution_mode,
                    "broker": broker,
                    "signal_id": signal_id,
                    "run_id": run_id,
                }
                existing_identity = {
                    "canonical_execution_id": existing_metadata.get("canonical_execution_id"),
                    "user_id": existing_metric.user_id,
                    "account_id": int(existing_metric.account_id),
                    "strategy_id": existing_metric.strategy_id,
                    "symbol": existing_metric.symbol,
                    "execution_mode": existing_metric.execution_mode,
                    "broker": existing_metric.broker,
                    "signal_id": existing_metric.signal_id,
                    "run_id": existing_metric.run_id,
                }
                if requested_identity != existing_identity:
                    msg = f"Execution metric {resolved_metric_id} changed economic identity"
                    raise ValueError(msg)
                return str(existing_metric.metric_id)
            previous = (
                session.query(ExecutionMetric)
                .filter_by(
                    user_id=user_id,
                    account_id=account_id,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    execution_mode=execution_mode,
                )
                .order_by(ExecutionMetric.created_at.desc())
                .first()
            )
            previous_meta = dict(previous.metadata_json or {}) if previous else {}
            peak_equity = equity
            if previous and previous.peak_equity is not None:
                if equity is not None:
                    peak_equity = max(float(previous.peak_equity), float(equity))
                else:
                    peak_equity = float(previous.peak_equity)

            drawdown_pct = None
            if equity is not None and peak_equity:
                drawdown_pct = (float(equity) - float(peak_equity)) / float(peak_equity)

            timestamp = created_at or datetime.now(tz=UTC)
            window_start = (
                previous.created_at
                if previous and getattr(previous, "created_at", None) is not None
                else timestamp
            )
            window_end = timestamp
            payload_meta = dict(metadata or {})
            payload_meta["settlement_currency"] = normalized_settlement

            def _as_decimal(value: Any, default: Decimal | None = None) -> Decimal:
                if default is None:
                    default = _DECIMAL_ZERO
                if value is None:
                    return default
                try:
                    return Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    return default

            def _normalize_action(value: str | None) -> str | None:
                if not value:
                    return None
                return value.strip().lower()

            state = dict(previous_meta.get("position_state") or {})
            previous_position_qty = _as_decimal(state.get("quantity"))
            previous_avg_price = _as_decimal(state.get("avg_price"))
            previous_realized_total = _as_decimal(
                state.get("realized_pnl"),
                _as_decimal(previous.realized_pnl if previous else None),
            )
            override = dict(position_state_override or {})
            has_authoritative_position = position_state_override is not None
            position_qty = (
                _as_decimal(override.get("quantity"))
                if has_authoritative_position
                else previous_position_qty
            )
            avg_price = (
                _as_decimal(override.get("avg_price"))
                if has_authoritative_position
                else previous_avg_price
            )
            current_price = (
                _as_decimal(override.get("current_price"))
                if has_authoritative_position
                else _DECIMAL_ZERO
            )
            realized_total = (
                _as_decimal(realized_pnl) if realized_pnl is not None else previous_realized_total
            )
            wins = int(state.get("wins", 0))
            losses = int(state.get("losses", 0))
            breakevens = int(state.get("breakevens", 0))
            gross_profit = _as_decimal(state.get("gross_profit"))
            gross_loss = _as_decimal(state.get("gross_loss"))
            turnover = _as_decimal(state.get("turnover"))
            fees_total = _as_decimal(state.get("fees_total"))

            trade_action = _normalize_action(signal_action)
            trade_price_dec = _as_decimal(trade_price)
            trade_qty_dec = _as_decimal(trade_qty)
            last_trade_turnover = (
                abs(trade_price_dec * trade_qty_dec)
                if trade_price_dec > 0 and trade_qty_dec > 0
                else _DECIMAL_ZERO
            )
            trade_pnl: Decimal | None = None

            if has_authoritative_position:
                if last_trade_turnover > 0:
                    turnover += last_trade_turnover
                    fees_total += _as_decimal(total_commission)

                if realized_pnl is not None:
                    realized_delta = realized_total - previous_realized_total
                    if realized_delta != 0:
                        trade_pnl = realized_delta
                        if trade_pnl > 0:
                            wins += 1
                            gross_profit += trade_pnl
                        elif trade_pnl < 0:
                            losses += 1
                            gross_loss += abs(trade_pnl)
                        else:
                            breakevens += 1
            elif trade_action and trade_price_dec > 0 and trade_qty_dec > 0:
                turnover += last_trade_turnover
                fees_total += _as_decimal(total_commission)

                if trade_action in {"close", "close_signal"}:
                    close_qty = trade_qty_dec
                    if close_qty <= 0:
                        close_qty = abs(position_qty)
                    close_qty = min(close_qty, abs(position_qty))
                    if close_qty > 0:
                        if position_qty > 0:
                            trade_pnl = (trade_price_dec - avg_price) * close_qty
                        elif position_qty < 0:
                            trade_pnl = (avg_price - trade_price_dec) * close_qty
                        position_qty = position_qty - (
                            close_qty if position_qty > 0 else -close_qty
                        )
                        if position_qty == 0:
                            avg_price = Decimal("0")
                else:
                    signed_qty = (
                        trade_qty_dec if trade_action in {"long", "buy"} else -trade_qty_dec
                    )
                    if (
                        position_qty == 0
                        or (position_qty > 0 and signed_qty > 0)
                        or (position_qty < 0 and signed_qty < 0)
                    ):
                        new_qty = position_qty + signed_qty
                        if new_qty != 0:
                            avg_price = (
                                (avg_price * abs(position_qty))
                                + (trade_price_dec * abs(signed_qty))
                            ) / abs(new_qty)
                        position_qty = new_qty
                    else:
                        close_qty = min(abs(position_qty), abs(signed_qty))
                        if position_qty > 0:
                            trade_pnl = (trade_price_dec - avg_price) * close_qty
                            position_qty -= close_qty
                        else:
                            trade_pnl = (avg_price - trade_price_dec) * close_qty
                            position_qty += close_qty
                        remaining = abs(signed_qty) - close_qty
                        if remaining > 0:
                            position_qty = remaining if signed_qty > 0 else -remaining
                            avg_price = trade_price_dec
                        elif position_qty == 0:
                            avg_price = Decimal("0")

                if trade_pnl is not None:
                    realized_total += trade_pnl
                    if trade_pnl > 0:
                        wins += 1
                        gross_profit += trade_pnl
                    elif trade_pnl < 0:
                        losses += 1
                        gross_loss += abs(trade_pnl)
                    else:
                        breakevens += 1

            trade_count = wins + losses + breakevens
            win_rate = float(wins / trade_count) if trade_count else 0.0
            avg_win = float(gross_profit / wins) if wins else 0.0
            avg_loss = float(gross_loss / losses) if losses else 0.0
            profit_factor = float(gross_profit / gross_loss) if gross_loss else None

            slippage = None
            if reference_price is not None and trade_price is not None and trade_qty is not None:
                try:
                    slippage = (float(trade_price) - float(reference_price)) * float(trade_qty)
                except (TypeError, ValueError):
                    slippage = None

            drawdown_state = dict(previous_meta.get("drawdown_state") or {})
            drawdown_start = drawdown_state.get("start_ts")
            max_duration = float(drawdown_state.get("max_duration_sec", 0.0) or 0.0)
            current_duration = 0.0
            if drawdown_pct is not None and drawdown_pct < 0:
                if drawdown_start:
                    try:
                        started_at = datetime.fromisoformat(drawdown_start)
                    except ValueError:
                        started_at = timestamp
                else:
                    started_at = timestamp
                    drawdown_start = timestamp.isoformat()
                current_duration = (timestamp - started_at).total_seconds()
                max_duration = max(max_duration, current_duration)
            else:
                drawdown_start = None

            payload_meta.update(
                {
                    "position_state": {
                        "quantity": float(position_qty),
                        "avg_price": float(avg_price),
                        "current_price": float(current_price),
                        "realized_pnl": float(realized_total),
                        "wins": wins,
                        "losses": losses,
                        "breakevens": breakevens,
                        "gross_profit": float(gross_profit),
                        "gross_loss": float(gross_loss),
                        "turnover": float(turnover),
                        "last_trade_turnover": float(last_trade_turnover),
                        "fees_total": float(fees_total),
                        "last_trade_pnl": float(trade_pnl) if trade_pnl is not None else None,
                    },
                    "performance": {
                        "trade_count": trade_count,
                        "win_rate": win_rate,
                        "avg_win": avg_win,
                        "avg_loss": avg_loss,
                        "profit_factor": profit_factor,
                    },
                    "drawdown_state": {
                        "start_ts": drawdown_start,
                        "current_duration_sec": current_duration,
                        "max_duration_sec": max_duration,
                    },
                    "slippage_estimate": slippage,
                }
            )
            if exposure is not None:
                payload_meta["exposure"] = exposure
            if leverage is not None:
                payload_meta["leverage"] = leverage

            if realized_pnl is None:
                realized_pnl = float(realized_total)
            else:
                realized_pnl = float(_as_decimal(realized_pnl))

            metric = ExecutionMetric(
                metric_id=resolved_metric_id,
                user_id=user_id,
                account_id=account_id,
                strategy_id=strategy_id,
                symbol=symbol,
                execution_mode=execution_mode,
                broker=broker,
                signal_id=signal_id,
                run_id=run_id,
                asset_class=asset_class,
                equity=equity,
                available_cash=available_cash,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                orders_submitted=orders_submitted,
                orders_filled=orders_filled,
                total_commission=total_commission,
                commission_currency=(
                    commission_currency.strip().upper() if commission_currency else None
                ),
                peak_equity=peak_equity,
                drawdown_pct=drawdown_pct,
                drawdown_duration_sec=current_duration if drawdown_pct is not None else None,
                max_drawdown_duration_sec=max_duration if drawdown_pct is not None else None,
                window_start=window_start,
                window_end=window_end,
                exposure=exposure,
                leverage=leverage,
                metadata_json=payload_meta,
                created_at=timestamp,
            )
            session.add(metric)
            session.commit()
            session.refresh(metric)
            return str(metric.metric_id)
