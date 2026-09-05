from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from execution_engine.metrics.fx_rates import SqlFXRateProvider
from execution_engine.metrics.strategy_metrics import (
    StrategyMetricsCurrencyError,
    StrategyMetricsService,
    StrategyMetricsUnavailableError,
    TradeResult,
)
from lib_application.db.models import (
    Base,
    Broker,
    CanonicalSignal,
    Execution,
    Instrument,
    InstrumentPrice,
    LinkedBrokerAccount,
    Order,
    Strategy,
    User,
)
from lib_application.db.models import OrderIntent as PersistedOrderIntent

# ECB euro reference rate published for 2024-12-31:
# https://www.ecb.europa.eu/stats/exchange/eurofxref/shared/pdf/2024/12/20241231.pdf
FX_OBSERVED_AT = datetime(2024, 12, 31, 14, 10, tzinfo=UTC)
EUR_USD = Decimal("1.0389")
USD_EUR = (Decimal("1") / EUR_USD).quantize(Decimal("0.00000001"))


def _session_local() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as session:
        broker = Broker(
            broker_id=1,
            code="paper",
            name="Paper Broker",
            capabilities={},
        )
        session.add_all(
            [
                User(user_id="u1", email="u1@example.com", base_ccy="EUR"),
                User(user_id="u2", email="u2@example.com", base_ccy="USD"),
                broker,
                Strategy(strategy_id="strat1", strategy_name="Strategy One"),
                Strategy(strategy_id="other", strategy_name="Other Strategy"),
            ]
        )
        session.flush()
        session.add_all(
            [
                LinkedBrokerAccount(
                    account_id=101,
                    user_id="u1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="U1 EUR paper",
                    base_ccy="EUR",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=202,
                    user_id="u1",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="U1 USD paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
                LinkedBrokerAccount(
                    account_id=303,
                    user_id="u2",
                    broker_id=broker.broker_id,
                    environment="paper",
                    display_name="U2 USD paper",
                    base_ccy="USD",
                    paper_initial_equity=100_000,
                    paper_initial_cash=100_000,
                    status="connected",
                ),
            ]
        )
        session.commit()
    return session_local


def _add_fill(
    session: Session,
    *,
    order_id: str,
    account_id: int,
    symbol: str,
    settlement_currency: str,
    side: str,
    price: Decimal,
    at: datetime,
    strategy_id: str = "strat1",
    quantity: Decimal = Decimal("1"),
    commission: Decimal = Decimal("0"),
    commission_currency: str | None = None,
) -> int:
    instrument = session.query(Instrument).filter_by(canonical=symbol).one_or_none()
    if instrument is None:
        instrument = Instrument(
            asset_class="crypto",
            canonical=symbol,
            settlement_currency=settlement_currency,
        )
        session.add(instrument)
    elif instrument.settlement_currency != settlement_currency:
        msg = f"{symbol} cannot settle in both {instrument.settlement_currency} and {settlement_currency}"
        raise ValueError(msg)
    broker = session.query(Broker).filter_by(code="paper").one()
    session.flush()
    signal = CanonicalSignal(
        strategy_id=strategy_id,
        instr_id=instrument.instr_id,
        action="long" if side == "BUY" else "short",
        external_signal_id=f"metrics:{order_id}",
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
        execution_mode="spot",
        broker_environment="paper",
        method="SPOT",
        payload={"local_order_id": order_id},
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
        broker_order_ref=order_id,
        state="filled",
        routed_at=at,
        last_update=at,
    )
    session.add(order)
    session.flush()
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
            trade_id=f"{order_id}:1",
        )
    )
    return int(order.order_id)


def test_compute_metrics_isolates_accounts_and_preserves_strategy_scope() -> None:
    session_local = _session_local()
    with session_local() as session:
        _add_fill(
            session,
            order_id="eur-entry",
            account_id=101,
            symbol="BTCEUR",
            settlement_currency="EUR",
            side="BUY",
            price=Decimal("100"),
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="usd-entry",
            account_id=202,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="BUY",
            price=Decimal("100"),
            at=datetime(2025, 1, 1, 1, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="eur-exit",
            account_id=101,
            symbol="BTCEUR",
            settlement_currency="EUR",
            side="SELL",
            price=Decimal("110"),
            at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="usd-exit",
            account_id=202,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="SELL",
            price=Decimal("200"),
            at=datetime(2025, 1, 2, 1, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="other-entry",
            account_id=101,
            symbol="BTCEUR",
            settlement_currency="EUR",
            side="BUY",
            price=Decimal("1"),
            at=datetime(2025, 1, 3, tzinfo=UTC),
            strategy_id="other",
        )
        _add_fill(
            session,
            order_id="other-exit",
            account_id=101,
            symbol="BTCEUR",
            settlement_currency="EUR",
            side="SELL",
            price=Decimal("1001"),
            at=datetime(2025, 1, 4, tzinfo=UTC),
            strategy_id="other",
        )
        session.commit()

    service = StrategyMetricsService(session_factory=session_local)
    eur_metrics = asyncio.run(
        service.compute_metrics(
            strategy_id="strat1",
            user_id="u1",
            account_id=101,
            mode="paper",
        )
    )
    usd_metrics = asyncio.run(
        service.compute_metrics(
            strategy_id="strat1",
            user_id="u1",
            account_id=202,
            mode="paper",
        )
    )

    assert eur_metrics.account_id == 101
    assert eur_metrics.currency == "EUR"
    assert eur_metrics.total_trades == 1
    assert eur_metrics.total_pnl == Decimal("10")
    assert usd_metrics.account_id == 202
    assert usd_metrics.currency == "USD"
    assert usd_metrics.total_trades == 1
    assert usd_metrics.total_pnl == Decimal("100")


def test_compute_metrics_normalizes_mixed_settlement_with_observed_fx() -> None:
    session_local = _session_local()
    with session_local() as session:
        usd_eur = Instrument(
            instr_id=1,
            asset_class="fx",
            canonical="USD/EUR",
            settlement_currency="EUR",
        )
        session.add(usd_eur)
        session.flush()
        session.add(
            InstrumentPrice(
                instr_id=usd_eur.instr_id,
                ts=FX_OBSERVED_AT.replace(tzinfo=None),
                timeframe="1d",
                close=USD_EUR,
                source="ecb_eurofxref_2024_12_31",
            )
        )
        _add_fill(
            session,
            order_id="usd-entry",
            account_id=101,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="BUY",
            price=Decimal("100"),
            commission=Decimal("1"),
            commission_currency="USD",
            at=datetime(2025, 1, 1, 10, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="usd-exit",
            account_id=101,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="SELL",
            price=Decimal("110"),
            commission=Decimal("1"),
            commission_currency="USD",
            at=datetime(2025, 1, 2, 10, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="eur-entry",
            account_id=101,
            symbol="ETHEUR",
            settlement_currency="EUR",
            side="BUY",
            price=Decimal("50"),
            commission=Decimal("0.5"),
            commission_currency="EUR",
            at=datetime(2025, 1, 1, 11, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="eur-exit",
            account_id=101,
            symbol="ETHEUR",
            settlement_currency="EUR",
            side="SELL",
            price=Decimal("55"),
            commission=Decimal("0.5"),
            commission_currency="EUR",
            at=datetime(2025, 1, 2, 11, tzinfo=UTC),
        )
        session.commit()

    service = StrategyMetricsService(
        session_factory=session_local,
        fx_rate_provider=SqlFXRateProvider(
            session_local,
            max_age=timedelta(days=3),
        ),
    )
    metrics = asyncio.run(
        service.compute_metrics(
            strategy_id="strat1",
            user_id="u1",
            account_id=101,
            mode="paper",
        )
    )

    expected_usd_pnl_in_eur = Decimal("8") * USD_EUR
    assert metrics.currency == "EUR"
    assert metrics.total_trades == 2
    assert metrics.gross_profit == expected_usd_pnl_in_eur + Decimal("4")
    assert metrics.total_pnl == expected_usd_pnl_in_eur + Decimal("4")


def test_reporting_window_preserves_inventory_opened_before_start() -> None:
    session_local = _session_local()
    with session_local() as session:
        _add_fill(
            session,
            order_id="entry-before-window",
            account_id=101,
            symbol="ETHEUR",
            settlement_currency="EUR",
            side="BUY",
            price=Decimal("100"),
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="exit-in-window",
            account_id=101,
            symbol="ETHEUR",
            settlement_currency="EUR",
            side="SELL",
            price=Decimal("112"),
            at=datetime(2025, 1, 3, tzinfo=UTC),
        )
        session.commit()

    metrics = asyncio.run(
        StrategyMetricsService(session_factory=session_local).compute_metrics(
            strategy_id="strat1",
            user_id="u1",
            account_id=101,
            mode="paper",
            start_date=datetime(2025, 1, 2, tzinfo=UTC),
            end_date=datetime(2025, 1, 4, tzinfo=UTC),
        )
    )

    assert metrics.total_trades == 1
    assert metrics.total_pnl == Decimal("12")


def test_compute_metrics_fails_closed_without_required_fx_observation() -> None:
    session_local = _session_local()
    with session_local() as session:
        _add_fill(
            session,
            order_id="usd-entry",
            account_id=101,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="BUY",
            price=Decimal("100"),
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        _add_fill(
            session,
            order_id="usd-exit",
            account_id=101,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="SELL",
            price=Decimal("110"),
            at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        session.commit()

    service = StrategyMetricsService(session_factory=session_local)

    with pytest.raises(StrategyMetricsCurrencyError, match="observed FX-rate provider"):
        asyncio.run(
            service.compute_metrics(
                strategy_id="strat1",
                user_id="u1",
                account_id=101,
                mode="paper",
            )
        )


def test_compute_metrics_fails_closed_without_owned_account_context() -> None:
    session_local = _session_local()
    service = StrategyMetricsService(session_factory=session_local)

    with pytest.raises(StrategyMetricsUnavailableError, match="unavailable for user"):
        asyncio.run(
            service.compute_metrics(
                strategy_id="strat1",
                user_id="u1",
                account_id=303,
                mode="paper",
            )
        )
    with pytest.raises(StrategyMetricsUnavailableError, match="positive broker account_id"):
        asyncio.run(
            service.compute_metrics(
                strategy_id="strat1",
                user_id="u1",
                account_id=0,
                mode="paper",
            )
        )
    with pytest.raises(StrategyMetricsUnavailableError, match="configured for paper, not live"):
        asyncio.run(
            service.compute_metrics(
                strategy_id="strat1",
                user_id="u1",
                account_id=101,
                mode="live",
            )
        )


def test_execution_schema_rejects_commission_without_currency() -> None:
    session_local = _session_local()
    with session_local() as session:
        order_id = _add_fill(
            session,
            order_id="missing-fee-currency",
            account_id=101,
            symbol="ETHEUR",
            settlement_currency="EUR",
            side="BUY",
            price=Decimal("100"),
            commission=Decimal("1"),
            commission_currency="EUR",
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        from sqlalchemy import text

        session.flush()
        with pytest.raises(IntegrityError):
            session.execute(
                text("UPDATE executions SET fee_ccy = NULL WHERE order_id = :order_id"),
                {"order_id": order_id},
            )


def test_compute_metrics_preserves_multiple_exact_partial_fills() -> None:
    session_local = _session_local()
    with session_local() as session:
        buy_order_id = _add_fill(
            session,
            order_id="partial-buy",
            account_id=202,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="BUY",
            quantity=Decimal("0.4"),
            price=Decimal("100"),
            commission=Decimal("0.40"),
            commission_currency="USD",
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        instrument = session.query(Instrument).filter_by(canonical="BTCUSD").one()
        session.add(
            Execution(
                order_id=buy_order_id,
                instr_id=instrument.instr_id,
                fill_ts=datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
                qty=Decimal("0.6"),
                price=Decimal("110"),
                fee_ccy="USD",
                fee_amount=Decimal("0.85"),
                venue="paper",
                trade_id="partial-buy:2",
            )
        )
        _add_fill(
            session,
            order_id="full-sell",
            account_id=202,
            symbol="BTCUSD",
            settlement_currency="USD",
            side="SELL",
            price=Decimal("120"),
            commission=Decimal("1"),
            commission_currency="USD",
            at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        session.commit()

    metrics = asyncio.run(
        StrategyMetricsService(session_factory=session_local).compute_metrics(
            strategy_id="strat1",
            user_id="u1",
            account_id=202,
            mode="paper",
        )
    )

    assert metrics.total_trades == 2
    assert metrics.total_pnl == Decimal("11.75")
    assert metrics.gross_profit == Decimal("11.75")


def test_risk_ratio_annualization_uses_observed_trade_cadence() -> None:
    first_exit = datetime(2024, 1, 1, tzinfo=UTC)
    second_exit = first_exit + timedelta(days=365.2425)

    def _trade(trade_id: str, exit_time: datetime) -> TradeResult:
        return TradeResult(
            trade_id=trade_id,
            strategy_id="strat1",
            account_id=101,
            currency="EUR",
            symbol="BTCUSDC",
            side="long",
            entry_price=Decimal("42000"),
            exit_price=Decimal("43000"),
            quantity=Decimal("0.1"),
            pnl=Decimal("100"),
            return_pct=2.380952,
            commission=Decimal("2"),
            entry_time=exit_time - timedelta(hours=4),
            exit_time=exit_time,
            duration_hours=4.0,
        )

    service = StrategyMetricsService()
    observed = service._observed_trades_per_year(
        [_trade("first", first_exit), _trade("second", second_exit)]
    )

    assert observed == pytest.approx(1.0)
