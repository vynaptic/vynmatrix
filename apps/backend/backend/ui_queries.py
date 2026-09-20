"""Read models for the owner UI.

Every statement selects explicit columns. The backend database role holds
column-level ``SELECT`` grants for this surface (migration
``0107_backend_ui_read``), so loading a whole ORM entity would be refused by
PostgreSQL even though it works on the SQLite test schema.

Nothing here computes P&L. ``execution_metrics``, ``positions`` and
``daily_nav`` are projections written by the execution engine; they are
reported as recorded, with their as-of time, in the account currency the P&L
service normalised them to. Values in different currencies are never added.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    CanonicalSignal,
    DailyNav,
    Deployment,
    Execution,
    ExecutionMetric,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    Position,
    Strategy,
    StrategyVersion,
    User,
    UserStrategyBinding,
)
from lib_common.logging import get_logger

logger = get_logger(__name__)

_BLOCKED_MODE = "blocked"
_ACTIVITY_LIMIT = 10
_TRADE_POINT_LIMIT = 500
_TREND_POINTS = 30
_TREND_DAYS = 90
_FEED_WINDOW_DAYS = 14
MAX_FILLS_PAGE = 200
MAX_PNL_DAYS = 730


def iso_utc(value: datetime | date | None) -> str | None:
    """Serialise a timestamp as UTC; naive columns are UTC by convention."""
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _instant(value: datetime) -> datetime:
    """A comparable UTC instant; naive columns are UTC by convention."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def money(value: Any) -> str | None:
    """Serialise a numeric column as a plain decimal string, never a float."""
    if value is None:
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(number, "f")


def _section(session: Session, name: str, build: Callable[[], Any]) -> Any:
    """Build one page section inside a savepoint.

    A failing query degrades that section to ``None`` instead of failing the
    page; the savepoint keeps the transaction, and with it the transaction-local
    tenant setting, usable for the sections that follow.
    """
    try:
        with session.begin_nested():
            return build()
    except SQLAlchemyError:
        logger.warning("Owner UI section unavailable", section=name, exc_info=True)
        return None


def _accounts(session: Session, owner_id: str) -> list[Any]:
    return list(
        session.execute(
            select(
                LinkedBrokerAccount.account_id,
                LinkedBrokerAccount.display_name,
                LinkedBrokerAccount.environment,
                LinkedBrokerAccount.base_ccy,
                LinkedBrokerAccount.status,
                LinkedBrokerAccount.paper_initial_equity,
            )
            .where(LinkedBrokerAccount.user_id == owner_id)
            .order_by(LinkedBrokerAccount.account_id)
        ).all()
    )


def _latest_metric_rows(session: Session, owner_id: str) -> list[Any]:
    """Latest non-blocked snapshot per (account, strategy, symbol, mode).

    ``realized_pnl`` is a cumulative running total per partition, so summing
    snapshots would count it once per execution. The established reading (see
    ``soak_report._pnl_sections``) is the latest row of each partition.
    """
    rank = (
        func.row_number()
        .over(
            partition_by=[
                ExecutionMetric.account_id,
                ExecutionMetric.strategy_id,
                ExecutionMetric.symbol,
                ExecutionMetric.execution_mode,
            ],
            order_by=ExecutionMetric.created_at.desc(),
        )
        .label("rank")
    )
    latest = (
        select(
            ExecutionMetric.account_id.label("account_id"),
            ExecutionMetric.strategy_id.label("strategy_id"),
            ExecutionMetric.symbol.label("symbol"),
            ExecutionMetric.realized_pnl.label("realized_pnl"),
            ExecutionMetric.created_at.label("created_at"),
            rank,
        )
        .where(
            ExecutionMetric.user_id == owner_id,
            ExecutionMetric.execution_mode != _BLOCKED_MODE,
        )
        .subquery()
    )
    return list(
        session.execute(
            select(
                latest.c.account_id,
                latest.c.strategy_id,
                latest.c.symbol,
                latest.c.realized_pnl,
                latest.c.created_at,
            )
            .where(latest.c.rank == 1)
            .order_by(latest.c.account_id, latest.c.strategy_id, latest.c.symbol)
        ).all()
    )


def _realized_totals(rows: list[Any]) -> dict[int, dict[str, Any]]:
    totals: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.realized_pnl is None:
            continue
        entry = totals.setdefault(int(row.account_id), {"value": Decimal(0), "as_of": None})
        entry["value"] += Decimal(str(row.realized_pnl))
        if entry["as_of"] is None or row.created_at > entry["as_of"]:
            entry["as_of"] = row.created_at
    return totals


def _latest_equity(session: Session, owner_id: str, account: Any) -> dict[str, Any] | None:
    """Most recent recorded equity, with its source always named.

    An execution snapshot is timestamped, a daily NAV row is dated; the NAV row
    wins only when its day is later than the snapshot's, so a quiet account does
    not keep showing the equity of its last trade. With neither, the configured
    paper starting equity is reported as such.
    """
    snapshot = session.execute(
        select(ExecutionMetric.equity, ExecutionMetric.created_at)
        .where(
            ExecutionMetric.user_id == owner_id,
            ExecutionMetric.account_id == account.account_id,
            ExecutionMetric.equity.is_not(None),
        )
        .order_by(ExecutionMetric.created_at.desc())
        .limit(1)
    ).first()
    nav = session.execute(
        select(DailyNav.nav_value, DailyNav.date)
        .where(
            DailyNav.user_id == owner_id,
            DailyNav.account_id == account.account_id,
            DailyNav.nav_ccy == account.base_ccy,
        )
        .order_by(DailyNav.date.desc())
        .limit(1)
    ).first()
    if snapshot is not None:
        recorded = snapshot.created_at
        recorded_day = (
            recorded.astimezone(UTC).date() if recorded.tzinfo is not None else recorded.date()
        )
        if nav is None or nav.date <= recorded_day:
            return {
                "value": money(snapshot.equity),
                "as_of": iso_utc(recorded),
                "source": "execution_snapshot",
            }
    if nav is not None:
        return {"value": money(nav.nav_value), "as_of": iso_utc(nav.date), "source": "daily_nav"}
    if account.paper_initial_equity is not None:
        return {
            "value": money(account.paper_initial_equity),
            "as_of": None,
            "source": "starting_equity",
        }
    return None


def _fill_stats(session: Session, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not account_ids:
        return {}
    rows = session.execute(
        select(Order.account_id, func.count(Execution.exec_id), func.max(Execution.fill_ts))
        .join(Order, Order.order_id == Execution.order_id)
        .where(Order.account_id.in_(account_ids))
        .group_by(Order.account_id)
    ).all()
    return {int(acct): {"count": int(count), "last": last} for acct, count, last in rows}


def _open_positions(session: Session, account_ids: list[int]) -> list[Any]:
    if not account_ids:
        return []
    return list(
        session.execute(
            select(
                Position.account_id,
                Instrument.canonical,
                Position.qty,
                Position.avg_price,
                Position.last_mark,
                Position.gross_notional,
                Position.notional_currency,
                Position.updated_at,
            )
            .join(Instrument, Instrument.instr_id == Position.instr_id)
            .where(Position.account_id.in_(account_ids), Position.qty != 0)
            .order_by(Position.account_id, Instrument.canonical)
        ).all()
    )


def _recent_signals(session: Session, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            CanonicalSignal.ts,
            CanonicalSignal.strategy_id,
            CanonicalSignal.action,
            CanonicalSignal.confidence,
            Instrument.canonical,
        )
        .join(Instrument, Instrument.instr_id == CanonicalSignal.instr_id)
        .order_by(CanonicalSignal.ts.desc())
        .limit(limit)
    ).all()
    return [
        {
            "_at": _instant(row.ts),
            "kind": "signal",
            "at": iso_utc(row.ts),
            "strategy_id": row.strategy_id,
            "symbol": row.canonical,
            "action": row.action,
            "confidence": money(row.confidence),
        }
        for row in rows
    ]


def parse_fill_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    """Decode a blotter cursor of the form ``<fill time ISO>_<execution id>``."""
    if cursor is None:
        return None
    moment, separator, identifier = cursor.rpartition("_")
    if not separator or not identifier.isdigit():
        msg = "before must be a cursor returned by this endpoint"
        raise ValueError(msg)
    try:
        return datetime.fromisoformat(moment), int(identifier)
    except ValueError as exc:
        msg = "before must be a cursor returned by this endpoint"
        raise ValueError(msg) from exc


def _fill_rows(
    session: Session,
    account_ids: list[int],
    *,
    limit: int,
    before: tuple[datetime, int] | None,
) -> list[dict[str, Any]]:
    """Fills newest first by fill time; the execution id only breaks ties.

    Replayed history is inserted out of time order, so the id alone is not a
    chronology.
    """
    if not account_ids:
        return []
    statement = (
        select(
            Execution.exec_id,
            Execution.fill_ts,
            Execution.qty,
            Execution.price,
            Execution.fee_amount,
            Execution.fee_ccy,
            Execution.venue,
            Order.account_id,
            Order.settlement_currency,
            OrderIntent.side,
            OrderIntent.strategy_id,
            Instrument.canonical,
        )
        .join(Order, Order.order_id == Execution.order_id)
        .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .join(Instrument, Instrument.instr_id == Execution.instr_id)
        .where(Order.account_id.in_(account_ids))
    )
    if before is not None:
        moment, identifier = before
        statement = statement.where(
            or_(
                Execution.fill_ts < moment,
                and_(Execution.fill_ts == moment, Execution.exec_id < identifier),
            )
        )
    rows = session.execute(
        statement.order_by(Execution.fill_ts.desc(), Execution.exec_id.desc()).limit(limit)
    ).all()
    return [
        {
            "_at": _instant(row.fill_ts),
            "kind": "fill",
            "id": int(row.exec_id),
            "cursor": f"{row.fill_ts.isoformat()}_{int(row.exec_id)}",
            "at": iso_utc(row.fill_ts),
            "account_id": int(row.account_id),
            "strategy_id": row.strategy_id,
            "symbol": row.canonical,
            "side": str(row.side).lower(),
            "quantity": money(row.qty),
            "price": money(row.price),
            "currency": row.settlement_currency,
            "fee": money(row.fee_amount),
            "fee_currency": row.fee_ccy,
            "venue": row.venue,
        }
        for row in rows
    ]


def _price_feeds(session: Session) -> list[dict[str, Any]]:
    """Latest stored price per timeframe, from a bounded recent window.

    One overall maximum would let an hourly FX row hide a minute feed that is
    days behind, so each cadence is reported on its own. A feed with nothing in
    the window is simply absent, which the page reads as stale.
    """
    since = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=_FEED_WINDOW_DAYS)
    rows = session.execute(
        select(InstrumentPrice.timeframe, func.max(InstrumentPrice.ts))
        .where(InstrumentPrice.ts >= since)
        .group_by(InstrumentPrice.timeframe)
        .order_by(InstrumentPrice.timeframe)
    ).all()
    return [{"timeframe": timeframe, "last_at": iso_utc(latest)} for timeframe, latest in rows]


def _public(item: dict[str, Any]) -> dict[str, Any]:
    """Drop the private sort and paging helpers before an item leaves the API."""
    return {key: value for key, value in item.items() if key not in {"_at", "cursor"}}


def _strategy_counts(session: Session, owner_id: str) -> dict[str, int]:
    catalogue = session.execute(select(func.count(Strategy.strategy_id))).scalar_one()
    bindings = session.execute(
        select(UserStrategyBinding.is_active, func.count(UserStrategyBinding.binding_id))
        .where(
            UserStrategyBinding.user_id == owner_id,
            UserStrategyBinding.strategy_id.is_not(None),
        )
        .group_by(UserStrategyBinding.is_active)
    ).all()
    by_state = {bool(active): int(count) for active, count in bindings}
    released = session.execute(
        select(func.count(Strategy.strategy_id)).where(Strategy.is_active.is_(True))
    ).scalar_one()
    return {
        "catalogue": int(catalogue),
        "released": int(released),
        "bound": sum(by_state.values()),
        "active": by_state.get(True, 0),
    }


def overview(session: Session, owner_id: str, *, safety: dict[str, Any]) -> dict[str, Any]:
    """Dashboard payload: safety, accounts with KPIs, freshness and activity."""
    owner = session.execute(
        select(User.full_name, User.base_ccy, User.tz).where(User.user_id == owner_id)
    ).one()
    accounts = _accounts(session, owner_id)
    account_ids = [int(account.account_id) for account in accounts]

    metric_rows = _section(session, "realized", lambda: _latest_metric_rows(session, owner_id))
    realized = _realized_totals(metric_rows) if metric_rows is not None else None
    fill_stats = _section(session, "fills", lambda: _fill_stats(session, account_ids))
    positions = _section(session, "positions", lambda: _open_positions(session, account_ids))

    trend_since = datetime.now(tz=UTC) - timedelta(days=_TREND_DAYS)
    account_payload = []
    for account in accounts:
        account_id = int(account.account_id)
        realized_entry = (realized or {}).get(account_id)
        trend = _section(
            session,
            "equity_trend",
            partial(_equity_by_trade, session, owner_id, account, trend_since),
        )
        account_payload.append(
            {
                "account_id": account_id,
                "name": account.display_name,
                "environment": account.environment,
                "currency": account.base_ccy,
                "status": account.status,
                "starting_equity": money(account.paper_initial_equity),
                "equity": _section(
                    session,
                    "equity",
                    partial(_latest_equity, session, owner_id, account),
                ),
                "equity_trend": None
                if trend is None
                else [point["value"] for point in trend[-_TREND_POINTS:]],
                "realized_pnl": None
                if realized is None
                else {
                    "value": money(realized_entry["value"] if realized_entry else Decimal(0)),
                    "as_of": iso_utc(realized_entry["as_of"]) if realized_entry else None,
                },
                "open_positions": None
                if positions is None
                else sum(1 for row in positions if int(row.account_id) == account_id),
                "fills": None
                if fill_stats is None
                else fill_stats.get(account_id, {"count": 0})["count"],
            }
        )

    last_fill = None
    if fill_stats:
        last_fill = max((entry["last"] for entry in fill_stats.values()), default=None)
    signals = _section(session, "signals", lambda: _recent_signals(session, _ACTIVITY_LIMIT))
    fills = _section(
        session,
        "recent_fills",
        lambda: _fill_rows(session, account_ids, limit=_ACTIVITY_LIMIT, before=None),
    )
    # Order by the instant, never by its text: "…:00Z" sorts after "…:00.250Z".
    activity = sorted(
        [*(signals or []), *(fills or [])], key=lambda item: item["_at"], reverse=True
    )[:_ACTIVITY_LIMIT]
    last_signal_at = signals[0]["at"] if signals else None
    activity = [_public(item) for item in activity]

    return {
        "as_of": iso_utc(datetime.now(tz=UTC)),
        "safety": safety,
        "owner": {
            "display_name": owner.full_name or "Owner",
            "base_currency": owner.base_ccy,
            "timezone": owner.tz,
        },
        "accounts": account_payload,
        "strategies": _section(session, "strategies", lambda: _strategy_counts(session, owner_id)),
        "freshness": {
            "price_feeds": _section(session, "prices", partial(_price_feeds, session)),
            "last_signal_at": last_signal_at,
            "last_fill_at": iso_utc(last_fill),
        },
        "activity": activity,
    }


def _latest_versions(session: Session) -> dict[str, Any]:
    """The version an owner cares about: the active one, else the newest release."""
    rank = (
        func.row_number()
        .over(
            partition_by=StrategyVersion.strategy_id,
            order_by=(
                case((StrategyVersion.status == "active", 0), else_=1),
                StrategyVersion.released_at.desc(),
                StrategyVersion.strat_ver_id.desc(),
            ),
        )
        .label("rank")
    )
    ranked = select(
        StrategyVersion.strategy_id.label("strategy_id"),
        StrategyVersion.semver.label("semver"),
        StrategyVersion.status.label("status"),
        rank,
    ).subquery()
    rows = session.execute(
        select(ranked.c.strategy_id, ranked.c.semver, ranked.c.status).where(ranked.c.rank == 1)
    ).all()
    return {row.strategy_id: row for row in rows}


def _last_signals(session: Session) -> dict[str, Any]:
    rank = (
        func.row_number()
        .over(partition_by=CanonicalSignal.strategy_id, order_by=CanonicalSignal.ts.desc())
        .label("rank")
    )
    ranked = select(
        CanonicalSignal.strategy_id.label("strategy_id"),
        CanonicalSignal.ts.label("ts"),
        CanonicalSignal.action.label("action"),
        CanonicalSignal.instr_id.label("instr_id"),
        rank,
    ).subquery()
    rows = session.execute(
        select(ranked.c.strategy_id, ranked.c.ts, ranked.c.action, Instrument.canonical)
        .join(Instrument, Instrument.instr_id == ranked.c.instr_id)
        .where(ranked.c.rank == 1)
    ).all()
    return {row.strategy_id: row for row in rows}


def strategies(session: Session, owner_id: str) -> dict[str, Any]:
    """Catalogue rows with the owner's binding, last signal and recorded P&L."""
    accounts = {int(account.account_id): account for account in _accounts(session, owner_id)}
    catalogue = session.execute(
        select(
            Strategy.strategy_id,
            Strategy.strategy_name,
            Strategy.asset_class,
            Strategy.description,
            Strategy.is_active,
        ).order_by(Strategy.strategy_name)
    ).all()
    versions = _section(session, "versions", lambda: _latest_versions(session)) or {}
    last_signals = _section(session, "signals", lambda: _last_signals(session))
    metric_rows = _section(session, "realized", lambda: _latest_metric_rows(session, owner_id))
    bindings = session.execute(
        select(
            UserStrategyBinding.strategy_id,
            UserStrategyBinding.broker_account_id,
            UserStrategyBinding.is_active,
            UserStrategyBinding.autopilot,
            UserStrategyBinding.entries_enabled,
            UserStrategyBinding.exits_enabled,
        )
        .where(
            UserStrategyBinding.user_id == owner_id,
            UserStrategyBinding.strategy_id.is_not(None),
        )
        .order_by(UserStrategyBinding.binding_id)
    ).all()
    bindings_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in bindings:
        account = accounts.get(int(binding.broker_account_id))
        bindings_by_strategy[str(binding.strategy_id)].append(
            {
                "account_id": int(binding.broker_account_id),
                "account_name": account.display_name if account else None,
                "active": bool(binding.is_active),
                "autopilot": bool(binding.autopilot),
                "entries_enabled": bool(binding.entries_enabled),
                "exits_enabled": bool(binding.exits_enabled),
            }
        )

    realized: dict[str, dict[int, Decimal]] | None = None
    if metric_rows is not None:
        realized = defaultdict(lambda: defaultdict(lambda: Decimal(0)))
        for row in metric_rows:
            if row.realized_pnl is not None:
                realized[row.strategy_id][int(row.account_id)] += Decimal(str(row.realized_pnl))

    items = []
    for row in catalogue:
        version = versions.get(row.strategy_id)
        signal = (last_signals or {}).get(row.strategy_id)
        items.append(
            {
                "strategy_id": row.strategy_id,
                "name": row.strategy_name,
                "asset_class": row.asset_class,
                "description": row.description,
                # Registered strategies stay fail-closed until maintenance
                # releases them; a binding is refused while this is false.
                "released": bool(row.is_active),
                "version": version.semver if version else None,
                "status": version.status if version else None,
                "bindings": bindings_by_strategy.get(row.strategy_id, []),
                "last_signal": None
                if last_signals is None or signal is None
                else {
                    "at": iso_utc(signal.ts),
                    "action": signal.action,
                    "symbol": signal.canonical,
                },
                "realized_pnl": None
                if realized is None
                else [
                    {
                        "account_id": account_id,
                        "currency": accounts[account_id].base_ccy,
                        "value": money(value),
                    }
                    for account_id, value in sorted(realized.get(row.strategy_id, {}).items())
                    if account_id in accounts
                ],
            }
        )
    return {"as_of": iso_utc(datetime.now(tz=UTC)), "strategies": items}


def _equity_daily(session: Session, owner_id: str, account: Any, since: date) -> list[Any]:
    rows = session.execute(
        select(DailyNav.date, DailyNav.nav_value, DailyNav.nav_ccy)
        .where(
            DailyNav.user_id == owner_id,
            DailyNav.account_id == account.account_id,
            DailyNav.date >= since,
        )
        .order_by(DailyNav.date)
    ).all()
    # A NAV row in another currency is not comparable with the account series.
    return [
        {"date": iso_utc(row.date), "value": money(row.nav_value)}
        for row in rows
        if row.nav_ccy == account.base_ccy
    ]


def _equity_by_trade(
    session: Session, owner_id: str, account: Any, since: datetime
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ExecutionMetric.created_at, ExecutionMetric.equity)
        .where(
            ExecutionMetric.user_id == owner_id,
            ExecutionMetric.account_id == account.account_id,
            ExecutionMetric.execution_mode != _BLOCKED_MODE,
            ExecutionMetric.equity.is_not(None),
            ExecutionMetric.created_at >= since,
        )
        .order_by(ExecutionMetric.created_at.desc())
        .limit(_TRADE_POINT_LIMIT)
    ).all()
    ordered = list(reversed(rows))
    return [
        {"trade": index + 1, "at": iso_utc(row.created_at), "value": money(row.equity)}
        for index, row in enumerate(ordered)
    ]


def _fees(session: Session, account_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Execution.fee_ccy, func.sum(Execution.fee_amount))
        .join(Order, Order.order_id == Execution.order_id)
        .where(Order.account_id == account_id)
        .group_by(Execution.fee_ccy)
        .order_by(Execution.fee_ccy)
    ).all()
    return [{"currency": currency, "value": money(total)} for currency, total in rows]


def pnl(session: Session, owner_id: str, *, days: int) -> dict[str, Any]:
    """P&L page payload, one block per account in that account's currency."""
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=days)
    accounts = _accounts(session, owner_id)
    account_ids = [int(account.account_id) for account in accounts]
    metric_rows = _section(session, "realized", lambda: _latest_metric_rows(session, owner_id))
    positions = _section(session, "positions", lambda: _open_positions(session, account_ids))

    payload = []
    for account in accounts:
        account_id = int(account.account_id)
        payload.append(
            {
                "account_id": account_id,
                "name": account.display_name,
                "currency": account.base_ccy,
                "starting_equity": money(account.paper_initial_equity),
                "equity": _section(
                    session,
                    "equity",
                    partial(_latest_equity, session, owner_id, account),
                ),
                "equity_daily": _section(
                    session,
                    "equity_daily",
                    partial(_equity_daily, session, owner_id, account, since.date()),
                ),
                "equity_by_trade": _section(
                    session,
                    "equity_by_trade",
                    partial(_equity_by_trade, session, owner_id, account, since),
                ),
                "realized_total": None
                if metric_rows is None
                else money(
                    sum(
                        (
                            Decimal(str(row.realized_pnl))
                            for row in metric_rows
                            if int(row.account_id) == account_id and row.realized_pnl is not None
                        ),
                        Decimal(0),
                    )
                ),
                "realized": None
                if metric_rows is None
                else [
                    {
                        "strategy_id": row.strategy_id,
                        "symbol": row.symbol,
                        "value": money(row.realized_pnl),
                        "as_of": iso_utc(row.created_at),
                    }
                    for row in metric_rows
                    if int(row.account_id) == account_id and row.realized_pnl is not None
                ],
                "positions": None
                if positions is None
                else [
                    {
                        "symbol": row.canonical,
                        "quantity": money(row.qty),
                        "average_price": money(row.avg_price),
                        "last_mark": money(row.last_mark),
                        "notional": money(row.gross_notional),
                        "notional_currency": row.notional_currency,
                        "as_of": iso_utc(row.updated_at),
                    }
                    for row in positions
                    if int(row.account_id) == account_id
                ],
                "fees": _section(session, "fees", partial(_fees, session, account_id)),
            }
        )
    return {"as_of": iso_utc(now), "days": days, "accounts": payload}


def version(session: Session, *, image: dict[str, Any] | None) -> dict[str, Any]:
    """What is installed, and what the running container says it is.

    The record is written by ``vmdev deploy`` under migration authority; the
    image facts are stamped into the container at build time. Reporting both
    is what makes a deployment performed by other means visible instead of
    silently trusted.
    """
    deployment = _section(session, "version", partial(_latest_deployment, session))
    drift = None
    if deployment is not None and image is not None and image.get("source_commit"):
        drift = deployment["source_commit"] != image["source_commit"]
    return {
        "as_of": iso_utc(datetime.now(tz=UTC)),
        "deployment": deployment,
        "image": image,
        "drift": drift,
    }


def _latest_deployment(session: Session) -> dict[str, Any] | None:
    """The newest recorded deployment, successful or not."""
    row = session.execute(
        select(
            Deployment.deployment_id,
            Deployment.mode,
            Deployment.image_tag,
            Deployment.source_commit,
            Deployment.source_dirty,
            Deployment.alembic_head,
            Deployment.started_at,
            Deployment.finished_at,
            Deployment.outcome,
        )
        .order_by(Deployment.deployment_id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "deployment_id": int(row.deployment_id),
        "mode": row.mode,
        "image_tag": row.image_tag,
        "source_commit": row.source_commit,
        "source_dirty": bool(row.source_dirty),
        "alembic_head": row.alembic_head,
        "started_at": iso_utc(row.started_at),
        "finished_at": iso_utc(row.finished_at),
        "outcome": row.outcome,
    }


def fills(
    session: Session, owner_id: str, *, limit: int, before: tuple[datetime, int] | None
) -> dict[str, Any]:
    """Fills blotter, newest first, keyset-paginated on (fill time, execution id)."""
    account_ids = [int(account.account_id) for account in _accounts(session, owner_id)]
    rows = _fill_rows(session, account_ids, limit=limit + 1, before=before)
    page = rows[:limit]
    next_before = page[-1]["cursor"] if len(rows) > limit and page else None
    return {
        "as_of": iso_utc(datetime.now(tz=UTC)),
        "fills": [_public(row) for row in page],
        "next_before": next_before,
    }
