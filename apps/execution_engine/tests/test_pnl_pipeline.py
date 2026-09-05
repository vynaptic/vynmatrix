from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution_engine.metrics.normalization import normalize_side
from execution_engine.metrics.pnl_service import (
    PnLLedgerUnavailableError,
    PnLService,
    TradeRecord,
)


def _seed_account(
    session_local,  # type: ignore[no-untyped-def]
    *,
    user_id: str = "u1",
    account_id: int = 1,
    base_ccy: str = "USD",
) -> None:
    from lib_application.db.models import (
        Broker,
        LinkedBrokerAccount,
        User,
    )

    with session_local() as session:
        if session.get(User, user_id) is None:
            session.add(User(user_id=user_id, email=f"{user_id}@example.com", base_ccy=base_ccy))
        broker = session.query(Broker).filter_by(code="paper").first()
        if broker is None:
            broker = Broker(code="paper", name="Paper", capabilities={})
            session.add(broker)
            session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=account_id,
                user_id=user_id,
                broker_id=broker.broker_id,
                environment="paper",
                display_name=f"{user_id} paper {account_id}",
                base_ccy=base_ccy,
                status="connected",
                paper_initial_equity=Decimal("10000"),
                paper_initial_cash=Decimal("10000"),
            )
        )
        session.commit()


def _add_canonical_fill(
    session_local,  # type: ignore[no-untyped-def]
    *,
    local_order_id: str,
    symbol: str,
    side: str,
    price: Decimal,
    at: datetime,
    quantity: Decimal = Decimal("1"),
    commission: Decimal = Decimal("0"),
    commission_currency: str | None = None,
    strategy_id: str = "strat1",
    account_id: int = 1,
    settlement_currency: str = "USD",
    order_state: str = "filled",
    include_execution: bool = True,
    execution_mode: str = "spot",
    contract_multiplier: Decimal | None = None,
) -> int:
    from lib_application.db.models import (
        Broker,
        CanonicalSignal,
        Execution,
        Instrument,
        Order,
        Strategy,
    )
    from lib_application.db.models import OrderIntent as PersistedOrderIntent

    with session_local() as session:
        strategy = session.get(Strategy, strategy_id)
        if strategy is None:
            session.add(Strategy(strategy_id=strategy_id, strategy_name=strategy_id))
        instrument = session.query(Instrument).filter_by(canonical=symbol).one_or_none()
        if instrument is None:
            instrument = Instrument(
                asset_class="crypto",
                canonical=symbol,
                settlement_currency=settlement_currency,
            )
            session.add(instrument)
        broker = session.query(Broker).filter_by(code="paper").one()
        session.flush()
        signal = CanonicalSignal(
            strategy_id=strategy_id,
            instr_id=instrument.instr_id,
            action="long" if side == "BUY" else "short",
            external_signal_id=f"pnl:{local_order_id}",
            ts=at,
        )
        session.add(signal)
        session.flush()
        intent = PersistedOrderIntent(
            user_id="u1",
            account_id=account_id,
            strategy_id=strategy_id,
            canonical_signal_id=signal.signal_id,
            side=side,
            execution_mode=execution_mode,
            broker_environment="paper",
            method=(
                "PERP"
                if execution_mode == "perpetual"
                else "FUTURES"
                if execution_mode == "futures"
                else "SPOT"
            ),
            payload={
                "local_order_id": local_order_id,
                "metadata": (
                    {
                        "execution_mode": execution_mode,
                        "contract_value_model": "linear",
                        "contract_multiplier": str(contract_multiplier),
                        "leverage": "2",
                        "contract_type": (
                            "perpetual" if execution_mode == "perpetual" else "dated"
                        ),
                    }
                    if contract_multiplier is not None
                    else {}
                ),
            },
            status="routed",
            created_at=at,
        )
        session.add(intent)
        session.flush()
        order = Order(
            intent_id=intent.intent_id,
            broker_id=broker.broker_id,
            account_id=account_id,
            settlement_currency=settlement_currency,
            broker_order_ref=local_order_id,
            state=order_state,
            routed_at=at,
            last_update=at,
        )
        session.add(order)
        session.flush()
        if include_execution:
            session.add(
                Execution(
                    order_id=order.order_id,
                    instr_id=instrument.instr_id,
                    fill_ts=at,
                    qty=quantity,
                    price=price,
                    fee_ccy=commission_currency or settlement_currency,
                    fee_amount=commission,
                    venue="paper",
                    trade_id=f"{local_order_id}:1",
                )
            )
        session.commit()
        return int(intent.intent_id)


def test_get_aggregate_pnl_reads_exact_canonical_executions() -> None:
    """Canonical fills drive P&L; an unfilled resting order has no economics."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)

    _add_canonical_fill(
        session_local,
        local_order_id="o1",
        symbol="BTCUSD",
        side="BUY",
        price=Decimal("100"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _add_canonical_fill(
        session_local,
        local_order_id="o2",
        symbol="BTCUSD",
        side="SELL",
        price=Decimal("110"),
        at=datetime(2026, 1, 2, tzinfo=UTC),
        order_state="canceled",
    )
    _add_canonical_fill(
        session_local,
        local_order_id="o3",
        symbol="BTCUSD",
        side="SELL",
        price=Decimal("105"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
        order_state="working",
        include_execution=False,
    )

    service = PnLService(session_factory=session_local)
    agg = asyncio.run(service.get_aggregate_pnl(user_id="u1", account_id=1))
    assert agg["total_realized_pnl"] == 10.0
    # Strategy filtering uses relational order-intent attribution.
    agg_other = asyncio.run(
        service.get_aggregate_pnl(
            user_id="u1",
            account_id=1,
            strategy_id="missing",
        )
    )
    assert agg_other["total_realized_pnl"] == 0.0


@pytest.mark.parametrize("execution_mode", ["perpetual", "futures"])
def test_canonical_futures_pnl_values_contracts_with_persisted_multiplier(
    execution_mode: str,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)
    _add_canonical_fill(
        session_local,
        local_order_id=f"{execution_mode}-open",
        symbol="BTC-USD",
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("100"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
        execution_mode=execution_mode,
        contract_multiplier=Decimal("5"),
    )
    _add_canonical_fill(
        session_local,
        local_order_id=f"{execution_mode}-close",
        symbol="BTC-USD",
        side="SELL",
        quantity=Decimal("2"),
        price=Decimal("110"),
        at=datetime(2026, 1, 2, tzinfo=UTC),
        execution_mode=execution_mode,
        contract_multiplier=Decimal("5"),
    )

    service = PnLService(session_factory=session_local)
    snapshot = asyncio.run(
        service.get_position_pnl(
            user_id="u1",
            account_id=1,
            symbol="BTC-USD",
        )
    )
    assert snapshot.quantity == 0
    assert snapshot.realized_pnl == Decimal("100")
    assert snapshot.total_deployed == Decimal("1000")


def test_fifo_long_only_round_trip_realizes_expected_pnl() -> None:
    service = PnLService()
    trades = [
        TradeRecord(
            trade_id="t1",
            exec_id=1,
            canonical_signal_id=101,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("100"),
            commission=Decimal("1"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 10, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="t2",
            exec_id=2,
            canonical_signal_id=102,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("110"),
            commission=Decimal("1"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 11, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="t3",
            exec_id=3,
            canonical_signal_id=103,
            symbol="BTC-USD",
            side="sell",
            quantity=Decimal("1.5"),
            price=Decimal("130"),
            commission=Decimal("1"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
        ),
    ]

    realized, qty, avg_cost, lots, contributions = service._calculate_fifo_pnl(trades)

    assert realized == Decimal("37.5")
    assert qty == Decimal("0.5")
    assert avg_cost == Decimal("111")
    assert len(lots) == 1
    assert lots[0].remaining == Decimal("0.5")
    assert [
        (
            contribution.entry_exec_id,
            contribution.exit_exec_id,
            contribution.quantity,
            contribution.net_realized_pnl,
            contribution.deployed_entry_capital,
        )
        for contribution in contributions
    ] == [
        (1, 3, Decimal("1"), Decimal("28.33333333333333333333333333"), Decimal("101")),
        (2, 3, Decimal("0.5"), Decimal("9.166666666666666666666666667"), Decimal("55.5")),
    ]


def test_fifo_contributions_cover_partial_long_short_and_flip_arithmetic() -> None:
    service = PnLService()
    trades = [
        TradeRecord(
            trade_id="open-long",
            exec_id=1,
            canonical_signal_id=101,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("2"),
            price=Decimal("100"),
            commission=Decimal("2"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 10, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="close-long-open-short",
            exec_id=2,
            canonical_signal_id=102,
            symbol="BTC-USD",
            side="sell",
            quantity=Decimal("3"),
            price=Decimal("110"),
            commission=Decimal("3"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 11, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="partial-short-close",
            exec_id=3,
            canonical_signal_id=103,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("0.4"),
            price=Decimal("90"),
            commission=Decimal("0.4"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="close-short-open-long",
            exec_id=4,
            canonical_signal_id=104,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1.6"),
            price=Decimal("80"),
            commission=Decimal("1.6"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 13, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="partial-long-close",
            exec_id=5,
            canonical_signal_id=105,
            symbol="BTC-USD",
            side="sell",
            quantity=Decimal("0.25"),
            price=Decimal("120"),
            commission=Decimal("0.25"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
        ),
    ]

    realized, qty, avg_cost, lots, contributions = service._calculate_fifo_pnl(trades)

    assert realized == Decimal("49.5")
    assert qty == Decimal("0.75")
    assert avg_cost == Decimal("81")
    assert len(lots) == 1
    assert [
        (
            contribution.entry_exec_id,
            contribution.exit_exec_id,
            contribution.entry_canonical_signal_id,
            contribution.exit_canonical_signal_id,
            contribution.quantity,
            contribution.net_realized_pnl,
            contribution.deployed_entry_capital,
            contribution.account_currency,
            contribution.exit_time,
        )
        for contribution in contributions
    ] == [
        (
            1,
            2,
            101,
            102,
            Decimal("2"),
            Decimal("16"),
            Decimal("202"),
            "USD",
            datetime(2026, 3, 6, 11, 0, tzinfo=UTC),
        ),
        (
            2,
            3,
            102,
            103,
            Decimal("0.4"),
            Decimal("7.2"),
            Decimal("44.4"),
            "USD",
            datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
        ),
        (
            2,
            4,
            102,
            104,
            Decimal("0.6"),
            Decimal("16.8"),
            Decimal("66.6"),
            "USD",
            datetime(2026, 3, 6, 13, 0, tzinfo=UTC),
        ),
        (
            4,
            5,
            104,
            105,
            Decimal("0.25"),
            Decimal("9.5"),
            Decimal("20.25"),
            "USD",
            datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
        ),
    ]


def test_exact_exit_signal_contribution_lookup_is_partition_scoped() -> None:
    from dataclasses import FrozenInstanceError

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base, CanonicalSignal

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)
    _add_canonical_fill(
        session_local,
        local_order_id="entry",
        symbol="BTCUSD",
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("100"),
        commission=Decimal("2"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _add_canonical_fill(
        session_local,
        local_order_id="exit",
        symbol="BTCUSD",
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("110"),
        commission=Decimal("1"),
        at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    with session_local() as session:
        entry_signal_id = int(
            session.query(CanonicalSignal.signal_id)
            .filter(CanonicalSignal.external_signal_id == "pnl:entry")
            .scalar()
        )
        exit_signal_id = int(
            session.query(CanonicalSignal.signal_id)
            .filter(CanonicalSignal.external_signal_id == "pnl:exit")
            .scalar()
        )

    service = PnLService(session_factory=session_local)
    realized, contributions = service.partition_realized_pnl_with_exit_contributions(
        "u1",
        "BTCUSD",
        account_id=1,
        strategy_id="strat1",
        exit_canonical_signal_id=exit_signal_id,
    )

    assert realized == Decimal("8")
    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.entry_exec_id != contribution.exit_exec_id
    assert contribution.entry_canonical_signal_id == entry_signal_id
    assert contribution.exit_canonical_signal_id == exit_signal_id
    assert contribution.quantity == Decimal("1")
    assert contribution.net_realized_pnl == Decimal("8")
    assert contribution.deployed_entry_capital == Decimal("101")
    assert contribution.account_currency == "USD"
    assert contribution.exit_time == datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(FrozenInstanceError):
        contribution.quantity = Decimal("2")  # type: ignore[misc]

    assert (
        service.realized_pnl_contributions_for_exit_signal(
            "u1",
            "BTCUSD",
            account_id=1,
            strategy_id="strat1",
            exit_canonical_signal_id=entry_signal_id,
        )
        == []
    )


def test_fifo_contribution_capital_handles_signed_fee_rebates_and_fails_closed() -> None:
    service = PnLService()
    trades = [
        TradeRecord(
            trade_id="long-with-rebate",
            exec_id=1,
            canonical_signal_id=201,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("100"),
            commission=Decimal("-1"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 10, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="long-close-short-flip-with-rebate",
            exec_id=2,
            canonical_signal_id=202,
            symbol="BTC-USD",
            side="sell",
            quantity=Decimal("2"),
            price=Decimal("110"),
            commission=Decimal("-2"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 11, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="short-close-with-rebate",
            exec_id=3,
            canonical_signal_id=203,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("90"),
            commission=Decimal("-1"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
        ),
    ]

    realized, qty, _avg_cost, _lots, contributions = service._calculate_fifo_pnl(trades)

    assert realized == Decimal("34")
    assert qty == 0
    assert [(item.net_realized_pnl, item.deployed_entry_capital) for item in contributions] == [
        (Decimal("12"), Decimal("99")),
        (Decimal("22"), Decimal("109")),
    ]

    invalid_capital_trades = [
        TradeRecord(
            trade_id="invalid-capital-entry",
            exec_id=4,
            canonical_signal_id=204,
            symbol="BTC-USD",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("100"),
            commission=Decimal("-100"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 13, 0, tzinfo=UTC),
        ),
        TradeRecord(
            trade_id="invalid-capital-close",
            exec_id=5,
            canonical_signal_id=205,
            symbol="BTC-USD",
            side="sell",
            quantity=Decimal("1"),
            price=Decimal("110"),
            commission=Decimal("0"),
            account_currency="USD",
            settlement_currency="USD",
            commission_currency="USD",
            timestamp=datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
        ),
    ]
    with pytest.raises(PnLLedgerUnavailableError, match="positive finite deployed"):
        service._calculate_fifo_pnl(invalid_capital_trades)


def test_persist_daily_nav_creates_explicit_account_scoped_snapshot() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base, DailyNav

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)

    service = PnLService(session_factory=session_local)
    asyncio.run(
        service.persist_daily_nav(
            user_id="u1",
            equity=Decimal("1050"),
            account_id=1,
            nav_ccy="USD",
        )
    )

    with session_local() as session:
        row = session.query(DailyNav).one()
        assert row.user_id == "u1"
        assert row.account_id == 1
        assert row.nav_value == Decimal("1050")
        assert row.nav_ccy == "USD"


def test_persist_daily_nav_keeps_accounts_and_native_currencies_separate() -> None:
    """A user's accounts retain independent NAV and account-native currency."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import (
        Base,
        Broker,
        DailyNav,
        LinkedBrokerAccount,
        User,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)
    with session_local() as s:
        s.add(User(user_id="eu-user", email="eu@example.com", base_ccy="EUR"))
        s.add(User(user_id="us-user", email="us@example.com", base_ccy="USD"))
        broker = s.query(Broker).filter_by(code="paper").one()
        s.add_all(
            [
                LinkedBrokerAccount(
                    account_id=2,
                    user_id="eu-user",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="EU USD Paper",
                    base_ccy="USD",
                    status="connected",
                    paper_initial_equity=Decimal("10000"),
                    paper_initial_cash=Decimal("10000"),
                ),
                LinkedBrokerAccount(
                    account_id=3,
                    user_id="us-user",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="US USD Paper",
                    base_ccy="USD",
                    status="connected",
                    paper_initial_equity=Decimal("10000"),
                    paper_initial_cash=Decimal("10000"),
                ),
                LinkedBrokerAccount(
                    account_id=4,
                    user_id="eu-user",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="EU EUR Paper",
                    base_ccy="EUR",
                    status="connected",
                    paper_initial_equity=Decimal("10000"),
                    paper_initial_cash=Decimal("10000"),
                ),
            ]
        )
        s.commit()

    service = PnLService(session_factory=session_local)

    asyncio.run(
        service.persist_daily_nav(
            user_id="us-user",
            equity=Decimal("2000"),
            account_id=3,
            nav_ccy="USD",
        )
    )
    asyncio.run(
        service.persist_daily_nav(
            user_id="eu-user",
            equity=Decimal("2100"),
            account_id=2,
            nav_ccy="USD",
        )
    )
    asyncio.run(
        service.persist_daily_nav(
            user_id="eu-user",
            equity=Decimal("1000"),
            account_id=4,
            nav_ccy="EUR",
        )
    )

    with pytest.raises(PnLLedgerUnavailableError, match="does not match linked broker account"):
        asyncio.run(
            service.persist_daily_nav(
                user_id="us-user",
                equity=Decimal("2000"),
                account_id=3,
                nav_ccy="EUR",
            )
        )
    with pytest.raises(PnLLedgerUnavailableError, match="unavailable for user eu-user"):
        asyncio.run(
            service.persist_daily_nav(
                user_id="eu-user",
                equity=Decimal("2100"),
                account_id=3,
                nav_ccy="USD",
            )
        )
    with session_local() as s:
        account = s.get(LinkedBrokerAccount, 3)
        assert account is not None
        account.status = "revoked"
        s.commit()
    with pytest.raises(PnLLedgerUnavailableError, match="Connected linked broker account"):
        asyncio.run(
            service.persist_daily_nav(
                user_id="us-user",
                equity=Decimal("2000"),
                account_id=3,
                nav_ccy="USD",
            )
        )
    with pytest.raises(PnLLedgerUnavailableError, match="finite non-negative"):
        asyncio.run(
            service.persist_daily_nav(
                user_id="eu-user",
                equity=Decimal("-1"),
                account_id=2,
                nav_ccy="USD",
            )
        )

    with session_local() as s:
        eu = (
            s.query(DailyNav)
            .filter(DailyNav.user_id == "eu-user")
            .order_by(DailyNav.account_id)
            .all()
        )
        us = s.query(DailyNav).filter(DailyNav.user_id == "us-user").one()
    assert [(row.account_id, row.nav_ccy, row.nav_value) for row in eu] == [
        (2, "USD", Decimal("2100.00")),
        (4, "EUR", Decimal("1000.00")),
    ]
    assert us.account_id == 3
    assert us.nav_ccy == "USD"


def test_unknown_trade_side_fails_closed() -> None:
    assert normalize_side("open_short") == "sell"
    with pytest.raises(ValueError, match="Unknown trade side"):
        normalize_side("mystery")
    with pytest.raises(TypeError, match="must be a string"):
        normalize_side(None)


def test_malformed_ledger_row_is_not_converted_to_empty_pnl() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)
    intent_id = _add_canonical_fill(
        session_local,
        local_order_id="bad-side",
        symbol="BTCUSD",
        side="BUY",
        price=Decimal("100"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with session_local() as session:
        from sqlalchemy import text

        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        session.execute(
            text("UPDATE order_intents SET side = 'MYSTERY' WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        )
        session.commit()

    service = PnLService(session_factory=session_local)

    with pytest.raises(PnLLedgerUnavailableError, match="complete PnL ledger"):
        service.partition_realized_pnl("u1", symbol="BTCUSD", account_id=1)


def test_open_position_with_no_price_marks_unrealized_to_zero_not_total_loss() -> None:
    """H-6: when current price is unavailable, an open position's unrealized P&L
    is marked to cost basis (0), not fabricated as -cost_basis from a 0 price."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine)
    _seed_account(session_local)

    _add_canonical_fill(
        session_local,
        local_order_id="o1",
        symbol="BTCUSD",
        side="BUY",
        price=Decimal("100"),
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    class _NoPriceProvider:
        async def get_quote(
            self,
            _symbol: str,
            *,
            user_id: str,
            environment: str,
        ):  # type: ignore[no-untyped-def]
            del user_id, environment
            # market data unavailable

    service = PnLService(session_factory=session_local, market_data_provider=_NoPriceProvider())
    snap = asyncio.run(service.get_position_pnl(user_id="u1", symbol="BTCUSD", account_id=1))

    assert snap.quantity == Decimal("1")
    assert snap.price_available is False
    # The bug would make unrealized_pnl == -cost_basis (-100); the fix marks it 0.
    assert snap.unrealized_pnl == Decimal("0")
    assert snap.cost_basis == Decimal("100")

    # And the aggregate flags the stale price symbol instead of silently mis-stating.
    agg = asyncio.run(service.get_aggregate_pnl(user_id="u1", account_id=1))
    assert agg["complete"] is True  # no exceptions
    assert "BTCUSD" in agg["stale_price_symbols"]
