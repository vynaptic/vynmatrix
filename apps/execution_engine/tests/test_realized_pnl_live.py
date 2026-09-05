"""PL-1: live realized P&L is computed PER (strategy, symbol) partition.

ExecutionMetricsStore partitions metrics by (user, strategy, symbol) and diffs the
realized P&L it is handed against that partition's previous row. So the value fed
in must be that partition's OWN cumulative FIFO realized — an account-wide figure
would book one symbol's P&L against another (cross-contamination) and inflate the
first snapshot. These tests lock the per-partition computation + its isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from execution_engine.metrics.fx_rates import SqlFXRateProvider
from execution_engine.metrics.pnl_service import (
    CurrencyConversionRequiredError,
    PnLLedgerUnavailableError,
    PnLService,
)


def _session_local(*, account_currency: str = "USD"):  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import (
        Base,
        Broker,
        LinkedBrokerAccount,
        User,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    with session_local() as session:
        session.add(User(user_id="u1", email="u1@example.com", base_ccy=account_currency))
        broker = Broker(code="paper", name="Paper", capabilities={})
        session.add(broker)
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=1,
                    user_id="u1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="U1 paper 1",
                    base_ccy=account_currency,
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="u1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="U1 paper 2",
                    base_ccy=account_currency,
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        session.commit()
    return session_local


def _fill(  # type: ignore[no-untyped-def]
    session_local,
    *,
    order_id,
    symbol,
    side,
    price,
    strategy_id="strat1",
    fee="1",
    account_id=1,
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
):
    from lib_application.db.models import (
        Broker,
        CanonicalSignal,
        Execution,
        Instrument,
        Order,
        Strategy,
    )
    from lib_application.db.models import OrderIntent as PersistedOrderIntent

    with session_local() as s:
        if s.get(Strategy, strategy_id) is None:
            s.add(Strategy(strategy_id=strategy_id, strategy_name=strategy_id))
        instrument = s.query(Instrument).filter_by(canonical=symbol).one_or_none()
        if instrument is None:
            instrument = Instrument(
                asset_class="crypto",
                canonical=symbol,
                settlement_currency="USD",
            )
            s.add(instrument)
        broker = s.query(Broker).filter_by(code="paper").one()
        s.flush()
        signal = CanonicalSignal(
            strategy_id=strategy_id,
            instr_id=instrument.instr_id,
            action="long" if side == "BUY" else "short",
            external_signal_id=f"realized:{order_id}",
            ts=created_at,
        )
        s.add(signal)
        s.flush()
        intent = PersistedOrderIntent(
            user_id="u1",
            account_id=account_id,
            strategy_id=strategy_id,
            canonical_signal_id=signal.signal_id,
            side=side,
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={"local_order_id": order_id},
            status="routed",
            created_at=created_at,
        )
        s.add(intent)
        s.flush()
        order = Order(
            intent_id=intent.intent_id,
            broker_id=broker.broker_id,
            account_id=account_id,
            settlement_currency="USD",
            broker_order_ref=order_id,
            state="filled",
            routed_at=created_at,
            last_update=created_at,
        )
        s.add(order)
        s.flush()
        s.add(
            Execution(
                order_id=order.order_id,
                instr_id=instrument.instr_id,
                fill_ts=created_at,
                qty=Decimal("1"),
                price=Decimal(price),
                fee_ccy="USD",
                fee_amount=Decimal(fee),
                venue="paper",
                trade_id=f"{order_id}:1",
            )
        )
        s.commit()


def test_partition_realized_pnl_closed_round_trip_net_of_fees() -> None:
    session_local = _session_local()
    _fill(session_local, order_id="o1", symbol="BTCUSD", side="BUY", price="100")
    _fill(session_local, order_id="o2", symbol="BTCUSD", side="SELL", price="110")
    service = PnLService(session_factory=session_local)
    # buy 1@100 (fee 1) + sell 1@110 (fee 1) -> +10 gross - 2 fees = +8.
    assert service.partition_realized_pnl(
        "u1",
        symbol="BTCUSD",
        account_id=1,
        strategy_id="strat1",
    ) == Decimal("8")


def test_partition_realized_pnl_is_isolated_per_symbol() -> None:
    # The cross-contamination regression: a closed win on BTCUSD must NOT appear in
    # ETHUSD's realized P&L (each partition diffs its own cumulative total).
    session_local = _session_local()
    _fill(session_local, order_id="o1", symbol="BTCUSD", side="BUY", price="100", fee="0")
    _fill(session_local, order_id="o2", symbol="BTCUSD", side="SELL", price="150", fee="0")  # +50
    _fill(
        session_local, order_id="o3", symbol="ETHUSD", side="BUY", price="20", fee="0"
    )  # open, no close
    service = PnLService(session_factory=session_local)
    assert service.partition_realized_pnl(
        "u1", symbol="BTCUSD", account_id=1, strategy_id="strat1"
    ) == Decimal("50")
    assert service.partition_realized_pnl(
        "u1", symbol="ETHUSD", account_id=1, strategy_id="strat1"
    ) == Decimal("0")


def test_partition_realized_pnl_isolated_per_strategy() -> None:
    session_local = _session_local()
    _fill(
        session_local,
        order_id="a1",
        symbol="BTCUSD",
        side="BUY",
        price="100",
        fee="0",
        strategy_id="A",
    )
    _fill(
        session_local,
        order_id="a2",
        symbol="BTCUSD",
        side="SELL",
        price="130",
        fee="0",
        strategy_id="A",
    )  # +30
    _fill(
        session_local,
        order_id="b1",
        symbol="BTCUSD",
        side="BUY",
        price="100",
        fee="0",
        strategy_id="B",
    )  # open
    service = PnLService(session_factory=session_local)
    assert service.partition_realized_pnl(
        "u1", symbol="BTCUSD", account_id=1, strategy_id="A"
    ) == Decimal("30")
    assert service.partition_realized_pnl(
        "u1", symbol="BTCUSD", account_id=1, strategy_id="B"
    ) == Decimal("0")


def test_partition_realized_pnl_isolated_per_broker_account() -> None:
    session_local = _session_local()
    _fill(
        session_local,
        order_id="account-1-buy",
        symbol="BTCUSD",
        side="BUY",
        price="100",
        fee="0",
        account_id=1,
    )
    _fill(
        session_local,
        order_id="account-2-sell",
        symbol="BTCUSD",
        side="SELL",
        price="150",
        fee="0",
        account_id=2,
    )
    service = PnLService(session_factory=session_local)

    assert service.partition_realized_pnl(
        "u1",
        symbol="BTCUSD",
        strategy_id="strat1",
        account_id=1,
    ) == Decimal("0")
    assert service.partition_realized_pnl(
        "u1",
        symbol="BTCUSD",
        strategy_id="strat1",
        account_id=2,
    ) == Decimal("0")


def test_partition_realized_pnl_unknown_instrument_fails_closed() -> None:
    service = PnLService(session_factory=_session_local())
    with pytest.raises(PnLLedgerUnavailableError, match="complete PnL ledger"):
        service.partition_realized_pnl("u1", symbol="MISSING", account_id=1)


def test_partition_realized_pnl_no_db_fails_closed() -> None:
    service = PnLService(session_factory=None)
    with pytest.raises(PnLLedgerUnavailableError, match="session is not configured"):
        service.partition_realized_pnl("u1", symbol="BTCUSD", account_id=1)


@pytest.mark.parametrize(
    ("account_currency", "pair", "observed_rate"),
    [
        ("EUR", "USD/EUR", Decimal("0.96255655")),
        ("INR", "USD/INR", Decimal("85.60352296")),
    ],
)
def test_partition_realized_pnl_converts_to_account_currency_with_observed_rates(
    account_currency: str,
    pair: str,
    observed_rate: Decimal,
) -> None:
    # Rates are derived from the ECB 2024-12-31 EUR reference observations:
    # EUR/USD=1.0389 and EUR/INR=88.9335.
    observed_at = datetime(2024, 12, 31, 14, 10, tzinfo=UTC)
    session_local = _session_local(account_currency=account_currency)
    from lib_application.db.models import Instrument, InstrumentPrice

    with session_local() as session:
        instrument = Instrument(
            asset_class="fx",
            canonical=pair,
            settlement_currency=account_currency,
        )
        session.add(instrument)
        session.flush()
        session.add(
            InstrumentPrice(
                instr_id=instrument.instr_id,
                ts=observed_at.replace(tzinfo=None),
                timeframe="1d",
                close=observed_rate,
                source="ecb_eurofxref_2024_12_31",
            )
        )
        session.commit()

    fill_time = observed_at + timedelta(hours=1)
    _fill(
        session_local,
        order_id=f"{account_currency}-buy",
        symbol="BTCUSD",
        side="BUY",
        price="100",
        created_at=fill_time,
    )
    _fill(
        session_local,
        order_id=f"{account_currency}-sell",
        symbol="BTCUSD",
        side="SELL",
        price="110",
        created_at=fill_time,
    )
    service = PnLService(
        session_factory=session_local,
        fx_rate_provider=SqlFXRateProvider(
            session_local,
            max_age=timedelta(days=1),
        ),
    )

    assert (
        service.partition_realized_pnl(
            "u1",
            symbol="BTCUSD",
            account_id=1,
            strategy_id="strat1",
        )
        == Decimal("8") * observed_rate
    )


def test_partition_realized_pnl_rejects_implicit_currency_parity() -> None:
    session_local = _session_local(account_currency="EUR")
    _fill(session_local, order_id="eur-buy", symbol="BTCUSD", side="BUY", price="100")
    _fill(session_local, order_id="eur-sell", symbol="BTCUSD", side="SELL", price="110")

    with pytest.raises(CurrencyConversionRequiredError, match="observed FX-rate provider"):
        PnLService(session_factory=session_local).partition_realized_pnl(
            "u1",
            symbol="BTCUSD",
            account_id=1,
            strategy_id="strat1",
        )
