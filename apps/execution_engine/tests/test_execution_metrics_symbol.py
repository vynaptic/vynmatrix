"""execution_metrics persists a normalized symbol (M7).

The raw signal.symbol could be 'BTC-USD' on one fill and 'BTCUSD' on another,
splitting the per-(user,strategy,symbol,mode) partition and fragmenting the
cumulative realized-P&L carry-forward. The store normalizes the symbol so one
asset maps to one partition.
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from execution_engine.execution_metrics_store import ExecutionMetricsStore
from lib_application.db.models import Base, ExecutionMetric


def _store() -> tuple[ExecutionMetricsStore, sessionmaker]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    return ExecutionMetricsStore(session_factory=session_local), session_local


def _record(store: ExecutionMetricsStore, symbol: str) -> None:
    store.record(
        user_id="u1",
        account_id=1,
        strategy_id="s1",
        symbol=symbol,
        execution_mode="spot",
        broker="paper",
        settlement_currency="USD",
        signal_id="sig",
        run_id=None,
        asset_class="crypto",
        equity=1000.0,
        available_cash=1000.0,
        margin_used=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        orders_submitted=1,
        orders_filled=1,
        total_commission=0.0,
        commission_currency=None,
    )


def test_metrics_symbol_normalized_to_single_partition() -> None:
    store, session_local = _store()
    _record(store, "BTC-USD")  # exchange spelling
    _record(store, "BTCUSD")  # canonical no-slash spelling
    with session_local() as s:
        symbols = s.execute(select(ExecutionMetric.symbol)).scalars().all()
    # Both spellings collapse to one normalized partition label.
    assert set(symbols) == {"BTCUSD"}
