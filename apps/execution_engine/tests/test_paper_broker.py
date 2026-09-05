from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from execution_engine.broker_bridge import to_broker_order_intent
from execution_engine.brokers.base import BrokerFill
from execution_engine.brokers.paper import PaperBroker
from execution_engine.config import FuturesConfig
from execution_engine.futures_builder import (
    FuturesContract,
    FuturesOrderBuilder,
    to_futures_intent,
)
from execution_engine.models import (
    HISTORICAL_REPLAY_FILL_POLICY,
    OptionsIntent,
    OptionsLeg,
    OrderCurrencyContext,
    OrderIntent,
)

_OBSERVED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _currency_context(
    *,
    account_currency: str = "USD",
    settlement_currency: str = "USD",
    rate: str = "1",
) -> OrderCurrencyContext:
    return OrderCurrencyContext(
        account_currency=account_currency,
        settlement_currency=settlement_currency,
        account_to_settlement_rate=Decimal(rate),
        requested_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        source="identity" if account_currency == settlement_currency else "coinbase_live",
    )


def _paper_broker(
    *,
    starting_balance: float,
    available_cash: float | None = None,
    account_id: str = "paper-test-account",
    currency: str = "USD",
    **kwargs,
) -> PaperBroker:
    """Construct an explicitly identified unit-test paper account."""
    return PaperBroker(
        account_id=account_id,
        starting_balance=starting_balance,
        available_cash=starting_balance if available_cash is None else available_cash,
        currency=currency,
        **kwargs,
    )


def _market_intent(*, side: str, quantity: float) -> OrderIntent:
    return OrderIntent(
        broker_code="paper",
        symbol="BTC-USD",
        side=side,
        quantity=quantity,
        order_type="market",
        metadata={"signal_id": f"sig-{side.lower()}-{quantity}"},
        currency_context=_currency_context(),
    )


def test_historical_replay_market_fill_retains_source_time_and_revision() -> None:
    persisted_fills: list[BrokerFill] = []

    def _commit_fill(**kwargs: object) -> None:
        persisted_fills.extend(kwargs["fills"])  # type: ignore[arg-type,index]

    broker = _paper_broker(
        starting_balance=10_000,
        slippage_pct=0,
        commission_pct=0,
        durable_fill_committer=_commit_fill,
    )
    broker.set_price("BTC-USD", 100)
    intent = _market_intent(side="BUY", quantity=1)
    intent.metadata = {
        **dict(intent.metadata or {}),
        "historical_replay": True,
        "source_price_id": 101,
        "source_content_revision": 3,
        "source_price_ts": "2026-07-14T11:15:00+00:00",
        "trigger_policy_version": HISTORICAL_REPLAY_FILL_POLICY,
    }
    broker.bind_canonical_order(intent, order_id=1, instrument_id=11)

    result = asyncio.run(broker.submit_order(intent))

    assert result.timestamp == datetime(2026, 7, 14, 11, 15, tzinfo=UTC)
    assert len(persisted_fills) == 1
    fill = persisted_fills[0]
    assert fill.timestamp == result.timestamp
    assert fill.raw_response == {
        "source_price_id": 101,
        "source_content_revision": 3,
        "trigger_policy_version": HISTORICAL_REPLAY_FILL_POLICY,
    }
    readback = asyncio.run(broker.get_fills(str(result.order_id)))
    assert len(readback) == 1
    assert readback[0] == fill


def _linear_futures_intent(
    *,
    execution_mode: str,
    side: str,
    quantity: float,
    context: OrderCurrencyContext | None = None,
    reduce_only: bool = False,
    contract_multiplier: object = 5.0,
) -> OrderIntent:
    contract_type = "perpetual" if execution_mode == "perpetual" else "dated"
    return OrderIntent(
        broker_code="paper",
        symbol="BTC-USD",
        side=side,
        quantity=quantity,
        order_type="market",
        metadata={
            "execution_mode": execution_mode,
            "contract_value_model": "linear",
            "contract_multiplier": contract_multiplier,
            "leverage": 2.0,
            "contract_type": contract_type,
            "purpose": "close_position" if reduce_only else "entry",
            "reduce_only": reduce_only,
        },
        currency_context=context or _currency_context(),
    )


@pytest.mark.parametrize("execution_mode", ["perpetual", "futures"])
def test_linear_futures_open_partial_close_and_full_close_use_contract_economics(
    execution_mode: str,
) -> None:
    broker = _paper_broker(
        starting_balance=10_000,
        slippage_pct=0,
        commission_pct=0.01,
    )
    broker.set_price("BTC-USD", 100)

    async def _run() -> tuple[object, object, object]:
        opened = await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="BUY",
                quantity=2,
            )
        )
        open_account = await broker.get_account_info()
        broker.set_price("BTC-USD", 110)
        partial = await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="SELL",
                quantity=1,
                reduce_only=True,
            )
        )
        partial_account = await broker.get_account_info()
        closed = await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="SELL",
                quantity=1,
                reduce_only=True,
            )
        )
        return (
            opened,
            open_account,
            (partial, partial_account, closed, await broker.get_account_info()),
        )

    opened, open_account, closing = asyncio.run(_run())
    partial, partial_account, closed, final_account = closing

    assert opened.commission == Decimal("10.000")
    assert open_account.equity == pytest.approx(9990)
    assert open_account.available_balance == pytest.approx(9490)
    assert open_account.margin_used == pytest.approx(500)
    assert open_account.realized_pnl == pytest.approx(-10)
    assert len(open_account.positions) == 1
    open_position = open_account.positions[0]
    assert open_position.quantity == 2
    assert open_position.quantity_unit == "contracts"
    assert open_position.contract_multiplier == 5
    assert open_position.leverage == 2
    assert open_position.contract_type == (
        "perpetual" if execution_mode == "perpetual" else "dated"
    )
    assert open_position.gross_notional == pytest.approx(1000)

    assert partial.commission == Decimal("5.500")
    assert partial_account.equity == pytest.approx(10084.5)
    assert partial_account.available_balance == pytest.approx(9809.5)
    assert partial_account.margin_used == pytest.approx(275)
    assert partial_account.realized_pnl == pytest.approx(34.5)
    assert partial_account.unrealized_pnl == pytest.approx(50)
    assert partial_account.positions[0].quantity == 1
    assert partial_account.positions[0].gross_notional == pytest.approx(550)

    assert closed.commission == Decimal("5.500")
    assert final_account.equity == pytest.approx(10079)
    assert final_account.available_balance == pytest.approx(10079)
    assert final_account.margin_used == 0
    assert final_account.realized_pnl == pytest.approx(79)
    assert final_account.positions == []


@pytest.mark.parametrize("execution_mode", ["perpetual", "futures"])
def test_linear_futures_cross_currency_round_trip_preserves_fill_fx(
    execution_mode: str,
) -> None:
    broker = _paper_broker(
        starting_balance=10_000,
        currency="EUR",
        slippage_pct=0,
        commission_pct=0.01,
    )
    entry_context = _currency_context(
        account_currency="EUR",
        settlement_currency="USD",
        rate="1.25",
    )
    exit_context = _currency_context(
        account_currency="EUR",
        settlement_currency="USD",
        rate="1.1",
    )
    broker.set_price("BTC-USD", 100)

    async def _run() -> tuple[object, object, object]:
        await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="BUY",
                quantity=2,
                context=entry_context,
            )
        )
        opened = await broker.get_account_info()
        broker.set_currency_context("BTC-USD", exit_context)
        broker.set_price("BTC-USD", 110)
        await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="SELL",
                quantity=1,
                context=exit_context,
                reduce_only=True,
            )
        )
        partial = await broker.get_account_info()
        await broker.submit_order(
            _linear_futures_intent(
                execution_mode=execution_mode,
                side="SELL",
                quantity=1,
                context=exit_context,
                reduce_only=True,
            )
        )
        return opened, partial, await broker.get_account_info()

    opened, partial, closed = asyncio.run(_run())
    assert opened.equity == pytest.approx(9992)
    assert opened.available_balance == pytest.approx(9592)
    assert opened.margin_used == pytest.approx(400)
    assert opened.positions[0].gross_notional == pytest.approx(800)
    assert partial.equity == pytest.approx(10187)
    assert partial.available_balance == pytest.approx(9937)
    assert partial.margin_used == pytest.approx(250)
    assert partial.realized_pnl == pytest.approx(87)
    assert partial.unrealized_pnl == pytest.approx(100)
    assert closed.equity == pytest.approx(10182)
    assert closed.available_balance == pytest.approx(10182)
    assert closed.realized_pnl == pytest.approx(182)
    assert closed.positions == []


@pytest.mark.parametrize("execution_mode", ["perpetual", "futures"])
def test_futures_builder_preserves_terms_on_entry_and_protective_intents(
    execution_mode: str,
) -> None:
    result = FuturesOrderBuilder(
        FuturesConfig(
            contract_size=5,
            max_leverage=10,
            target_leverage=2,
            use_perpetual=execution_mode == "perpetual",
        )
    ).build_order(
        symbol="BTC-USD",
        is_long=True,
        price=100,
        notional_size=1000,
        available_margin=10000,
    )
    intents = to_futures_intent(
        result,
        broker_code="paper",
        stop_loss=90,
        take_profit=120,
    )

    assert [intent.metadata["purpose"] for intent in intents] == [
        "entry",
        "stop_loss",
        "take_profit",
    ]
    for intent in intents:
        assert intent.metadata["execution_mode"] == execution_mode
        assert intent.metadata["contract_value_model"] == "linear"
        assert intent.metadata["contract_multiplier"] == 5
        assert intent.metadata["leverage"] == 2
        assert intent.metadata["reference_price"] == 100
        assert to_broker_order_intent(intent)["execution_method"] == execution_mode
    assert intents[1].metadata["reduce_only"] is True
    assert intents[2].metadata["reduce_only"] is True


@pytest.mark.parametrize(
    ("stop_loss", "take_profit", "error_pattern"),
    [
        (100, None, "stop_loss is on the wrong side"),
        (None, 100, "take_profit is on the wrong side"),
        (-1, None, "stop_loss must be finite and positive"),
        (None, float("nan"), "take_profit must be finite and positive"),
    ],
)
def test_futures_builder_rejects_invalid_protective_prices(
    stop_loss: float | None,
    take_profit: float | None,
    error_pattern: str,
) -> None:
    result = FuturesOrderBuilder(FuturesConfig()).build_order(
        symbol="BTC-PERP",
        is_long=True,
        price=100,
        notional_size=1_000,
        available_margin=1_000,
    )

    with pytest.raises(ValueError, match=error_pattern):
        to_futures_intent(
            result,
            broker_code="paper",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )


def test_observed_futures_contract_multiplier_flows_into_every_intent() -> None:
    contract = FuturesContract(
        symbol="ESZ6",
        underlying="ES",
        contract_type="dated",
        expiry=datetime(2026, 12, 18, tzinfo=UTC),
        contract_size=50,
        tick_size=0.25,
        margin_requirement=0.1,
        maintenance_margin=0.08,
        funding_rate=None,
    )
    result = FuturesOrderBuilder(
        FuturesConfig(
            contract_size=1,
            max_leverage=20,
            target_leverage=15,
            use_perpetual=False,
        )
    ).build_order(
        symbol="ESZ6",
        is_long=True,
        price=5000,
        notional_size=500_000,
        available_margin=100_000,
        contract_info=contract,
    )
    intents = to_futures_intent(
        result,
        broker_code="paper",
        stop_loss=4900,
        take_profit=5200,
    )
    assert result.quantity == 2
    assert result.contract_multiplier == 50
    # The observed 10% contract margin caps a looser 15x user preference.
    assert result.leverage == 10
    assert all(intent.metadata["contract_multiplier"] == 50 for intent in intents)
    assert all(intent.metadata["leverage"] == 10 for intent in intents)


@pytest.mark.parametrize("contract_size", [0, -1, float("nan")])
def test_observed_futures_contract_rejects_invalid_multiplier(
    contract_size: float,
) -> None:
    with pytest.raises(ValueError, match="contract_size must be finite and positive"):
        FuturesContract(
            symbol="ESZ6",
            underlying="ES",
            contract_type="dated",
            expiry=datetime(2026, 12, 18, tzinfo=UTC),
            contract_size=contract_size,
            tick_size=0.25,
            margin_requirement=0.1,
            maintenance_margin=0.08,
            funding_rate=None,
        )


@pytest.mark.parametrize("contract_multiplier", [None, 0, -1, float("nan")])
def test_linear_futures_rejects_missing_or_invalid_multiplier_without_mutation(
    contract_multiplier: object,
) -> None:
    broker = _paper_broker(starting_balance=10_000)
    broker.set_price("BTC-USD", 100)
    result = asyncio.run(
        broker.submit_order(
            _linear_futures_intent(
                execution_mode="perpetual",
                side="BUY",
                quantity=1,
                contract_multiplier=contract_multiplier,
            )
        )
    )
    assert result.is_rejected
    assert result.error_code == "contract_terms_invalid"
    assert asyncio.run(broker.get_positions()) == []
    assert broker.get_trade_history() == []


def test_linear_futures_rejects_mode_contract_type_mismatch_without_mutation() -> None:
    broker = _paper_broker(starting_balance=10_000)
    broker.set_price("BTC-USD", 100)
    intent = _linear_futures_intent(
        execution_mode="perpetual",
        side="BUY",
        quantity=1,
    )
    assert intent.metadata is not None
    intent.metadata["contract_type"] = "dated"

    result = asyncio.run(broker.submit_order(intent))

    assert result.is_rejected
    assert result.error_code == "contract_terms_invalid"
    assert asyncio.run(broker.get_positions()) == []
    assert broker.get_trade_history() == []


@pytest.mark.parametrize("contract_size", [0, -1, float("nan")])
def test_futures_config_rejects_invalid_contract_size(contract_size: float) -> None:
    with pytest.raises(ValueError, match="contract_size must be finite and positive"):
        FuturesConfig(contract_size=contract_size)


def test_paper_broker_full_close_does_not_double_count_realized_pnl() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> tuple[object, object]:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))
        broker.set_price("BTC-USD", 110.0)
        await broker.submit_order(_market_intent(side="SELL", quantity=1.0))
        return await broker.get_account_info(), broker.get_pnl_summary()

    account, pnl = asyncio.run(_run())

    assert account.equity == 1010.0
    assert account.available_balance == 1010.0
    assert account.unrealized_pnl == 0.0
    assert account.realized_pnl == 10.0
    assert account.positions == []
    assert pnl["current_balance"] == 1010.0
    assert pnl["realized_pnl"] == 10.0
    assert pnl["unrealized_pnl"] == 0.0
    assert pnl["total_pnl"] == 10.0


def test_paper_broker_realized_pnl_is_net_of_commission() -> None:
    # PL-3: realized P&L must subtract commissions (gross here would be 10.0).
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.01,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> tuple[object, object]:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))  # comm 1.0
        broker.set_price("BTC-USD", 110.0)
        await broker.submit_order(_market_intent(side="SELL", quantity=1.0))  # comm 1.1
        return await broker.get_account_info(), broker.get_pnl_summary()

    account, pnl = asyncio.run(_run())

    # 10.0 price move - 1.0 entry fee - 1.1 exit fee = 7.9 net.
    assert account.realized_pnl == pytest.approx(7.9)
    assert pnl["realized_pnl"] == pytest.approx(7.9)
    assert account.equity == pytest.approx(1007.9)


def test_multicurrency_round_trip_converts_account_economics_and_keeps_fee_currency() -> None:
    broker = _paper_broker(
        starting_balance=100_000.0,
        currency="EUR",
        slippage_pct=0.0,
        commission_pct=0.01,
    )
    broker.set_price("BTC-USDC", 50_000.0)
    entry_context = _currency_context(
        account_currency="EUR",
        settlement_currency="USDC",
        rate="1.2",
    )
    exit_context = _currency_context(
        account_currency="EUR",
        settlement_currency="USDC",
        rate="1.1",
    )
    entry = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=0.048,
        order_type="market",
        currency_context=entry_context,
    )
    exit_intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="SELL",
        quantity=0.048,
        order_type="market",
        currency_context=exit_context,
    )

    async def _run() -> tuple[object, dict[str, object], object, object]:
        entry_result = await broker.submit_order(entry)
        after_entry = await broker.get_account_info()
        entry_snapshot = {
            "available_balance": after_entry.available_balance,
            "equity": after_entry.equity,
            "realized_pnl": after_entry.realized_pnl,
            "gross_notional": after_entry.positions[0].gross_notional,
            "notional_currency": after_entry.positions[0].notional_currency,
        }
        broker.set_currency_context("BTC-USDC", exit_context)
        broker.set_price("BTC-USDC", 55_000.0)
        exit_result = await broker.submit_order(exit_intent)
        return entry_result, entry_snapshot, exit_result, await broker.get_account_info()

    entry_result, entry_snapshot, exit_result, after_exit = asyncio.run(_run())

    assert entry_result.commission == Decimal("24.00000")
    assert entry_result.commission_currency == "USDC"
    assert entry_snapshot["available_balance"] == pytest.approx(97_780.0)
    assert entry_snapshot["equity"] == pytest.approx(99_980.0)
    assert entry_snapshot["realized_pnl"] == pytest.approx(-20.0)
    assert entry_snapshot["gross_notional"] == pytest.approx(2_000.0)
    assert entry_snapshot["notional_currency"] == "EUR"
    assert exit_result.commission == Decimal("26.40000")
    assert exit_result.commission_currency == "USDC"
    assert after_exit.available_balance == pytest.approx(100_356.0)
    assert after_exit.equity == pytest.approx(100_356.0)
    assert after_exit.realized_pnl == pytest.approx(356.0)
    assert after_exit.positions == []


def test_paper_broker_rejects_missing_or_mismatched_currency_context() -> None:
    broker = _paper_broker(
        starting_balance=1_000.0,
        currency="EUR",
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USDC", 100.0)
    missing = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1.0,
        order_type="market",
    )
    mismatched = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1.0,
        order_type="market",
        currency_context=_currency_context(
            account_currency="USD",
            settlement_currency="USDC",
            rate="1.01",
        ),
    )

    missing_result = asyncio.run(broker.submit_order(missing))
    mismatched_result = asyncio.run(broker.submit_order(mismatched))

    assert missing_result.is_rejected
    assert missing_result.error_code == "currency_context_invalid"
    assert mismatched_result.is_rejected
    assert mismatched_result.error_code == "currency_context_invalid"
    assert asyncio.run(broker.get_account_info()).equity == 1_000.0
    assert broker.get_trade_history() == []


def test_paper_broker_partial_close_keeps_equity_and_realized_pnl_consistent() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> tuple[object, object]:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))
        broker.set_price("BTC-USD", 110.0)
        await broker.submit_order(_market_intent(side="SELL", quantity=0.5))
        return await broker.get_account_info(), broker.get_pnl_summary()

    account, pnl = asyncio.run(_run())

    assert account.equity == 1010.0
    assert account.available_balance == 949.5
    assert account.margin_used == 5.5
    assert account.unrealized_pnl == 5.0
    assert account.realized_pnl == 5.0
    assert len(account.positions) == 1
    assert account.positions[0].quantity == 0.5
    assert account.positions[0].realized_pnl == 5.0
    assert pnl["current_balance"] == 955.0
    assert pnl["realized_pnl"] == 5.0
    assert pnl["unrealized_pnl"] == 5.0
    assert pnl["total_pnl"] == 10.0


def test_multi_leg_option_fill_tracks_each_contract_position() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    intent = OptionsIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1,
        order_type="limit",
        legs=[
            OptionsLeg(
                option_type="CALL",
                strike=50000.0,
                expiry="2026-08-28",
                side="BUY",
                quantity=1,
                premium=5.0,
            ),
            OptionsLeg(
                option_type="CALL",
                strike=55000.0,
                expiry="2026-08-28",
                side="SELL",
                quantity=1,
                premium=2.0,
            ),
        ],
        spread_type="bull_call",
        metadata={"contract_multiplier": 100.0},
        currency_context=_currency_context(),
    )

    async def _run() -> tuple[object, list, object]:
        result = await broker.submit_options_order(intent)
        positions = await broker.get_positions()
        account = await broker.get_account_info()
        return result, positions, account

    result, positions, account = asyncio.run(_run())

    assert result.is_filled
    assert result.average_price == 3.0
    assert account.available_balance == 630.0
    assert account.margin_used == 70.0
    assert account.equity == 1000.0
    assert {(position.symbol, position.side) for position in positions} == {
        ("BTC-USDC:2026-08-28:50000:CALL", "LONG"),
        ("BTC-USDC:2026-08-28:55000:CALL", "SHORT"),
    }
    assert all(position.quantity_unit == "contracts" for position in positions)
    assert all(position.contract_multiplier == 100.0 for position in positions)
    assert {position.gross_notional for position in positions} == {500.0, 200.0}
    assert all(position.notional_currency == "USD" for position in positions)


def test_multi_leg_option_fill_rejects_missing_leg_premium_without_mutation() -> None:
    broker = _paper_broker(starting_balance=1000.0)
    intent = OptionsIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1,
        legs=[
            OptionsLeg(
                option_type="CALL",
                strike=50000.0,
                expiry="2026-08-28",
                side="BUY",
                quantity=1,
            ),
            OptionsLeg(
                option_type="CALL",
                strike=55000.0,
                expiry="2026-08-28",
                side="SELL",
                quantity=1,
                premium=2.0,
            ),
        ],
        spread_type="bull_call",
        metadata={"contract_multiplier": 100.0},
        currency_context=_currency_context(),
    )

    result = asyncio.run(broker.submit_options_order(intent))

    assert result.is_rejected
    assert asyncio.run(broker.get_positions()) == []
    assert broker.get_trade_history() == []


def test_multi_leg_option_fill_rejects_implicit_contract_multiplier() -> None:
    broker = _paper_broker(starting_balance=1000.0)
    intent = OptionsIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=1,
        legs=[
            OptionsLeg(
                option_type="CALL",
                strike=50000.0,
                expiry="2026-08-28",
                side="BUY",
                quantity=1,
                premium=5.0,
            ),
            OptionsLeg(
                option_type="CALL",
                strike=55000.0,
                expiry="2026-08-28",
                side="SELL",
                quantity=1,
                premium=2.0,
            ),
        ],
        spread_type="bull_call",
        currency_context=_currency_context(),
    )

    result = asyncio.run(broker.submit_options_order(intent))

    assert result.is_rejected
    assert "contract multiplier" in (result.error_message or "").lower()
    assert asyncio.run(broker.get_positions()) == []


# ── M1: paper resting orders are tracked + exposed to reconciliation ──


def _resting_intent(*, order_type: str, side: str = "SELL", price: float = 110.0) -> OrderIntent:
    return OrderIntent(
        broker_code="paper",
        symbol="BTC-USD",
        side=side,
        quantity=1.0,
        order_type=order_type,
        limit_price=price if order_type == "limit" else None,
        stop_price=price if order_type == "stop" else None,
        metadata={"signal_id": f"sig-{order_type}"},
        currency_context=_currency_context(),
    )


def test_resting_order_is_tracked_and_returned_by_get_open_orders() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )

    async def _run() -> list[dict]:
        result = await broker.submit_order(_resting_intent(order_type="stop", price=90.0))
        # Resting orders are accepted/working, not filled.
        assert not result.is_filled
        return await broker.get_open_orders()

    open_orders = asyncio.run(_run())

    assert len(open_orders) == 1
    assert open_orders[0]["symbol"] == "BTC-USD"
    assert open_orders[0]["side"] == "SELL"
    assert open_orders[0]["order_type"] == "stop"
    assert open_orders[0]["status"] == "working"
    # The exposed order_id matches the accepted result so reconciliation can match it.
    assert "order_id" in open_orders[0]


def test_market_order_does_not_appear_in_open_orders() -> None:
    broker = _paper_broker(
        starting_balance=10000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> list[dict]:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))
        return await broker.get_open_orders()

    assert asyncio.run(_run()) == []


def test_local_paper_fill_has_stable_identity_and_observed_economics() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.001,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run():  # type: ignore[no-untyped-def]
        result = await broker.submit_order(_market_intent(side="BUY", quantity=1.0))
        first = await broker.get_fills(str(result.order_id))
        replay = await broker.get_fills(str(result.order_id))
        return result, first, replay

    result, fills, replay = asyncio.run(_run())

    assert len(fills) == 1
    assert fills == replay
    assert fills[0].trade_id == f"{result.order_id}:fill:1"
    assert fills[0].order_id == result.order_id
    assert fills[0].quantity == Decimal("1.0")
    assert fills[0].price == Decimal("100.0")
    assert fills[0].commission == Decimal("0.1")
    assert fills[0].commission_currency == "USD"
    assert fills[0].timestamp.tzinfo is not None
    assert fills[0].venue == "paper"


def test_cancel_order_removes_it_from_open_orders() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )

    async def _run() -> list[dict]:
        result = await broker.submit_order(_resting_intent(order_type="limit", price=120.0))
        cancelled = await broker.cancel_order(result.order_id)
        assert cancelled is True
        return await broker.get_open_orders()

    assert asyncio.run(_run()) == []


def test_get_open_orders_filters_by_symbol() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )

    async def _run() -> tuple[list[dict], list[dict]]:
        await broker.submit_order(_resting_intent(order_type="stop", price=90.0))
        return (
            await broker.get_open_orders(symbol="BTC-USD"),
            await broker.get_open_orders(symbol="ETH-USD"),
        )

    btc, eth = asyncio.run(_run())
    assert len(btc) == 1
    assert eth == []


# ── Spot long-only: an oversell must clamp to flat, NOT open a reverse short
# (the negative-qty state corruption the soak acceptance gate flags). ──


def test_oversell_clamps_to_flat_not_reverse_short() -> None:
    broker = _paper_broker(
        starting_balance=100000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> tuple[object, object, list, list[dict]]:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))  # long 1.0
        result = await broker.submit_order(
            _market_intent(side="SELL", quantity=2.0)
        )  # oversell by 1.0
        account = await broker.get_account_info()
        return result, account, await broker.get_positions(), broker.get_trade_history()

    result, account, positions, trades = asyncio.run(_run())
    assert positions == []  # flat — no reverse short, no negative qty
    assert result.filled_quantity == 1.0
    assert trades[-1]["quantity"] == 1.0
    # Cash credits only the held quantity: 100000 - 100 + 100 = 100000.
    assert account.available_balance == 100000.0


def test_spot_sell_without_a_long_position_is_rejected() -> None:
    broker = _paper_broker(
        starting_balance=1000.0,
        slippage_pct=0.0,
        commission_pct=0.0,
    )
    broker.set_price("BTC-USD", 100.0)

    result = asyncio.run(broker.submit_order(_market_intent(side="SELL", quantity=1.0)))

    assert result.is_rejected
    assert result.filled_quantity == 0.0
    assert asyncio.run(broker.get_positions()) == []
    assert broker.get_trade_history() == []


def test_oversell_reverses_only_when_allow_reversal() -> None:
    broker = _paper_broker(
        starting_balance=100000.0, slippage_pct=0.0, commission_pct=0.0, allow_reversal=True
    )
    broker.set_price("BTC-USD", 100.0)

    async def _run() -> list:
        await broker.submit_order(_market_intent(side="BUY", quantity=1.0))
        await broker.submit_order(_market_intent(side="SELL", quantity=2.0))
        return await broker.get_positions()

    positions = asyncio.run(_run())
    assert len(positions) == 1
    assert positions[0].side == "SHORT"
    assert positions[0].quantity == 1.0
