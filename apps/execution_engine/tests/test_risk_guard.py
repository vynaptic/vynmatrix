from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from execution_engine.brokers.base import AccountInfo, BrokerCapabilities, BrokerOrderResult
from execution_engine.config import AccountState, BrokerType, ExecutionMode
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_result import ExecutionResult
from execution_engine.models import (
    CloseQuantityOverride,
    OptionsIntent,
    OrderCurrencyContext,
    OrderIntent,
    TargetPositionQuantityOverride,
)
from execution_engine.order_builder import OrderBuilder
from execution_engine.risk_baseline import RiskBaseline, RiskPeakProvenance
from execution_engine.risk_guard import RiskGuard
from lib_strategy.signals.signal import Signal, SignalAction


class _FakeBreachStore:
    def __init__(self, *, mandates: list[dict] | None = None) -> None:
        self.records: list[dict] = []
        self.mandates = list(mandates or [])

    def record(self, **payload):  # type: ignore[no-untyped-def]
        self.records.append(payload)
        return len(self.records)

    def load_mandates(self, *, user_id: str):  # type: ignore[no-untyped-def]
        return list(self.mandates)

    def has_user_drawdown_mandate(self, *, user_id: str) -> bool:
        return any("max_drawdown_pct" in mandate for mandate in self.mandates)


class _RecordingMetricsStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **payload: Any) -> int:
        self.records.append(payload)
        return len(self.records)


class _FakeOrderBuilder(OrderBuilder):
    def build(self, req, account, market_data):  # type: ignore[no-untyped-def]
        return [
            OrderIntent(
                broker_code="coinbase",
                symbol=req.signal.symbol,
                side="SELL" if req.signal.action == SignalAction.SHORT else "BUY",
                quantity=1.0,
                order_type="market",
                metadata={"signal_id": req.signal.signal_id},
            )
        ]


class _FakeBroker:
    def __init__(self) -> None:
        self._connected = False
        self.submit_calls = 0
        self.seeded_prices: list[tuple[str, float]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
            supported_assets=["crypto"],
            has_paper_trading=True,
        )

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            broker=BrokerType.COINBASE,
            account_id="acct-1",
            equity=10_000.0,
            available_balance=10_000.0,
            margin_used=0.0,
            unrealized_pnl=0.0,
            positions=[],
        )

    async def submit_order(self, intent: OrderIntent) -> BrokerOrderResult:
        self.submit_calls += 1
        return BrokerOrderResult.filled(order_id="o-1", quantity=intent.quantity, price=100.0)

    async def submit_options_order(self, intent):  # type: ignore[no-untyped-def]
        raise AssertionError("Options flow not expected")

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult.filled(order_id=order_id, quantity=1.0, price=100.0)

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return []

    def set_price(self, symbol: str, price: float) -> None:
        self.seeded_prices.append((symbol, price))


def _signal(action: SignalAction, *, stop_loss: float | None = None) -> Signal:
    return Signal(
        strategy_id="SwingHighLowPMO",
        strategy_type="indicator",
        symbol="BTC-USD",
        action=action,
        confidence=0.9,
        timestamp=datetime.now(tz=UTC),
        entry_price=100.0,
        stop_loss=stop_loss,
        asset_class="crypto",
        source="paper",
        external_signal_id=f"ext-risk-{action.value.lower()}",
    )


def _owned_paper_profile() -> dict[str, Any]:
    account_id = 101
    account = {
        "user_id": "user-1",
        "broker": "coinbase",
        "environment": "paper",
        "status": "connected",
        "base_ccy": "USD",
        "paper_initial_equity": 10_000.0,
        "paper_initial_cash": 10_000.0,
    }
    return {
        "broker": "coinbase",
        "broker_account_id": account_id,
        "base_ccy": "USD",
        "sandbox": True,
        "equity": 10_000.0,
        "available_cash": 10_000.0,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "accounts": {str(account_id): account},
        "_broker_route_snapshot": {
            "broker": "coinbase",
            "broker_environment": "paper",
            "broker_account_id": account_id,
            "sandbox": True,
        },
    }


def _install_owned_account_route(engine: ExecutionEngine, monkeypatch: Any) -> None:
    def _resolve_account_id(
        *,
        user_id: str,
        broker_type: BrokerType,
        environment: str,
        profile: dict[str, Any],
    ) -> int:
        account_id = int(profile["broker_account_id"])
        route = profile["_broker_route_snapshot"]
        account = profile["accounts"][str(account_id)]
        assert int(route["broker_account_id"]) == account_id
        assert route["broker"] == broker_type.value
        assert route["broker_environment"] == environment
        assert account["user_id"] == user_id
        assert account["status"] == "connected"
        return account_id

    def _resolve_account_currency(*, user_id: str, account_id: int) -> str:
        assert user_id == "user-1"
        assert account_id == 101
        return "USD"

    monkeypatch.setattr(engine._route_resolver, "resolve_account_id", _resolve_account_id)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_account_currency",
        _resolve_account_currency,
    )


def test_position_cap_allows_existing_target_topup_but_blocks_new_symbol() -> None:
    guard = RiskGuard()
    account = AccountState(
        broker=BrokerType.PAPER,
        equity=10_000,
        available_cash=5_000,
        open_positions=30,
        positions=[{"symbol": "BTC-USD", "qty": 2, "side": "long"}],
    )
    capabilities = BrokerCapabilities(
        broker_type=BrokerType.PAPER,
        supports_spot=True,
        supports_margin=False,
        supports_futures=False,
        supports_perpetual=False,
        supports_options=False,
    )
    config = {"require_stop_loss": True, "risk_caps": {"max_open_positions": 30}}

    topup = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95),
        profile={},
        user_strategy_config=config,
        account_state=account,
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=capabilities,
    )
    new_symbol = guard.evaluate(
        user_id="user-1",
        signal=replace(
            _signal(SignalAction.LONG, stop_loss=95),
            symbol="ETH-USD",
            external_signal_id="ext-risk-new-position",
        ),
        profile={},
        user_strategy_config=config,
        account_state=account,
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=capabilities,
    )

    assert topup.allowed is True
    assert new_symbol.allowed is False
    assert new_symbol.rule_code == "max_open_positions"


def test_risk_guard_blocks_short_when_shorting_disabled() -> None:
    breach_store = _FakeBreachStore()
    guard = RiskGuard(breach_store=breach_store)
    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.SHORT),
        profile={"enable_shorting": False},
        user_strategy_config={"enable_shorting": False, "require_stop_loss": False},
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[],
        market_data={"price": 100.0},
    )
    assert decision.allowed is False
    assert decision.rule_code == "shorting_disabled"


def test_risk_guard_can_require_calibrated_scoring_inputs() -> None:
    guard = RiskGuard()
    signal = _signal(SignalAction.LONG, stop_loss=95.0)
    signal.metadata["scoring_input_source"] = "stop_risk_reward_heuristic"

    decision = guard.evaluate(
        user_id="user-1",
        signal=signal,
        profile={},
        user_strategy_config={"require_explicit_scoring_inputs": True},
        account_state=type(
            "AccountStateStub",
            (),
            {"equity": 10_000.0, "daily_trades": 0, "open_positions": 0},
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "explicit_scoring_inputs_required"


# --- Q2: shorting-eligibility capability matrix --------------------------------


def _caps(broker: BrokerType, **flags: bool) -> BrokerCapabilities:
    base = {
        "supports_spot": True,
        "supports_margin": False,
        "supports_futures": False,
        "supports_perpetual": False,
        "supports_options": False,
    }
    base.update(flags)
    return BrokerCapabilities(broker_type=broker, **base)


def _short(*, shortable: bool | None = None, asset_class: str | None = "crypto") -> Signal:
    meta = {} if shortable is None else {"instrument_shortable": shortable}
    return Signal(
        strategy_id="s",
        strategy_type="indicator",
        symbol="BTC-PERP",
        action=SignalAction.SHORT,
        confidence=0.9,
        timestamp=datetime(2026, 3, 6, 12, 0, tzinfo=UTC),
        entry_price=100.0,
        asset_class=asset_class,
        source="paper",
        external_signal_id="ext-risk-short",
        metadata=meta,
    )


def _eval_short(
    signal: Signal,
    *,
    mode: ExecutionMode,
    caps: BrokerCapabilities,
    enable: bool,
    allowlist: frozenset[str] | None = None,
):
    guard = RiskGuard(short_eligible_asset_classes=allowlist)
    return guard._evaluate_short_block(
        signal=signal,
        profile={},
        user_strategy_config={"enable_shorting": enable},
        broker_capabilities=caps,
        execution_mode=mode,
    )


def test_short_allowed_when_all_dimensions_permit() -> None:
    # Futures mode + broker supports futures + user enabled + instrument shortable.
    decision = _eval_short(
        _short(shortable=True),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
    )
    assert decision is None  # allowed — no block


def test_short_allowed_when_instrument_shortability_unknown() -> None:
    # Absent instrument_shortable means "no per-instrument restriction" (additive).
    decision = _eval_short(
        _short(shortable=None),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
    )
    assert decision is None


def test_short_blocked_when_instrument_not_shortable() -> None:
    # All other dimensions permit, but the instrument is explicitly non-shortable.
    decision = _eval_short(
        _short(shortable=False),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
    )
    assert decision is not None
    assert decision.allowed is False
    assert decision.rule_code == "shorting_disabled"
    assert decision.context["short_block_dimension"] == "instrument_not_shortable"


def test_short_block_attributes_user_dimension() -> None:
    decision = _eval_short(
        _short(shortable=True),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=False,
    )
    assert decision is not None
    assert decision.context["short_block_dimension"] == "user_not_eligible"


def test_short_block_attributes_broker_mode_dimension() -> None:
    # Spot is long-only regardless of user/instrument: broker+mode cannot short.
    decision = _eval_short(
        _short(shortable=True),
        mode=ExecutionMode.SPOT,
        caps=_caps(BrokerType.COINBASE),
        enable=True,
    )
    assert decision is not None
    assert decision.context["short_block_dimension"] == "broker_mode_unsupported"


# --- Q2 dim 4: strategy asset-class eligibility -------------------------------


def test_asset_class_dimension_is_noop_when_allowlist_empty() -> None:
    # Default (no allowlist): the asset-class dimension never blocks — the running
    # spot-only soak is byte-identical (the other three dims still gate).
    decision = _eval_short(
        _short(shortable=True),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
        allowlist=None,
    )
    assert decision is None


def test_short_blocked_when_asset_class_not_eligible() -> None:
    # All other dimensions permit, but crypto is not in the short-eligible allowlist.
    decision = _eval_short(
        _short(shortable=True, asset_class="crypto"),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
        allowlist=frozenset({"equity"}),
    )
    assert decision is not None
    assert decision.rule_code == "shorting_disabled"
    assert decision.context["short_block_dimension"] == "asset_class_not_shortable"


def test_short_allowed_when_asset_class_in_allowlist() -> None:
    decision = _eval_short(
        _short(shortable=True, asset_class="crypto"),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
        allowlist=frozenset({"crypto"}),
    )
    assert decision is None


def test_short_allowed_when_asset_class_unknown_even_with_allowlist() -> None:
    # Missing asset_class must not block (additive — never gate on missing data).
    decision = _eval_short(
        _short(shortable=True, asset_class=None),
        mode=ExecutionMode.FUTURES,
        caps=_caps(BrokerType.DELTA, supports_futures=True),
        enable=True,
        allowlist=frozenset({"equity"}),
    )
    assert decision is None


def test_asset_class_dimension_does_not_override_broker_mode_for_spot() -> None:
    # Even with an allowlist set, a spot short still blocks at broker/mode first —
    # spot stays long-only (precedence preserved).
    decision = _eval_short(
        _short(shortable=True, asset_class="crypto"),
        mode=ExecutionMode.SPOT,
        caps=_caps(BrokerType.COINBASE),
        enable=True,
        allowlist=frozenset({"crypto"}),
    )
    assert decision is not None
    assert decision.context["short_block_dimension"] == "broker_mode_unsupported"


def test_engine_blocks_short_before_broker_submission(monkeypatch) -> None:
    breach_store = _FakeBreachStore()
    metrics_store = _RecordingMetricsStore()
    broker = _FakeBroker()
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        risk_breach_store=breach_store,
        execution_metrics_store=metrics_store,
        default_mode="paper",
        allow_live=False,
    )

    async def _fake_get_broker(*args, **kwargs):  # type: ignore[no-untyped-def]
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_settlement_currency",
        lambda _signal: "USD",
    )

    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_owned_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
                "enable_shorting": False,
                "require_stop_loss": False,
            },
            signal=_signal(SignalAction.SHORT),
        )
    )

    assert result.success is False
    assert "SHORT blocked" in (result.error_message or "")
    assert result.broker_account_id == 101
    assert result.settlement_currency == "USD"
    assert broker.submit_calls == 0
    assert len(metrics_store.records) == 1
    assert metrics_store.records[0]["account_id"] == 101
    assert metrics_store.records[0]["settlement_currency"] == "USD"
    assert metrics_store.records[0]["execution_mode"] == "blocked"
    assert breach_store.records[-1]["rule_code"] == "shorting_disabled"


def test_risk_guard_honors_execution_policy_snapshot_risk_caps() -> None:
    breach_store = _FakeBreachStore()
    guard = RiskGuard(breach_store=breach_store)

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "_execution_policy_snapshot": {
                "binding_id": 3,
                "risk_caps": {
                    "max_position_pct": 0.40,
                    "max_daily_loss_pct": 0.05,
                    "max_open_positions": 10,
                },
                "config": {
                    "risk_caps": {
                        "max_position_pct": 0.40,
                        "max_daily_loss_pct": 0.05,
                        "max_open_positions": 10,
                    }
                },
            }
        },
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=1.0,
                order_type="market",
            )
        ],
        market_data={"price": 3000.0},
    )

    assert decision.allowed is True


def test_risk_guard_ignores_protective_exit_orders_in_position_limit() -> None:
    breach_store = _FakeBreachStore()
    guard = RiskGuard(breach_store=breach_store)

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={"require_stop_loss": True},
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=10.0,
                order_type="market",
                metadata={"signal_id": "entry"},
            ),
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="SELL",
                quantity=10.0,
                order_type="stop",
                metadata={"purpose": "stop_loss"},
            ),
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="SELL",
                quantity=10.0,
                order_type="limit",
                metadata={"purpose": "take_profit"},
            ),
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is True
    assert decision.rule_code is None


def test_risk_guard_mandates_cap_snapshot_risk_limits() -> None:
    breach_store = _FakeBreachStore(
        mandates=[
            {
                "max_position_pct": 0.10,
                "max_daily_loss_pct": 0.02,
            }
        ]
    )
    guard = RiskGuard(breach_store=breach_store)

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "_execution_policy_snapshot": {
                "binding_id": 3,
                "risk_caps": {
                    "max_position_pct": 0.40,
                    "max_daily_loss_pct": 0.05,
                    "max_open_positions": 10,
                },
            },
            "max_position_pct": 0.50,
        },
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=1.0,
                order_type="market",
            )
        ],
        market_data={"price": 3000.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"


def test_risk_guard_mandates_cap_daily_trade_limit() -> None:
    breach_store = _FakeBreachStore(mandates=[{"daily_trade_limit": 2}])
    guard = RiskGuard(breach_store=breach_store)

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "daily_trade_limit": 10,
            "risk_limits": {"daily_trade_limit": 12},
        },
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 2,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "daily_trade_limit"


def test_risk_guard_applies_position_limit_to_options_intents() -> None:
    breach_store = _FakeBreachStore()
    guard = RiskGuard(breach_store=breach_store)

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {"max_position_pct": 0.10},
        },
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 0,
            },
        )(),
        execution_mode=ExecutionMode.BULL_CALL,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.PAPER,
            supports_spot=True,
            supports_margin=True,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=True,
            supports_multi_leg=True,
        ),
        intents=[
            OptionsIntent(
                broker_code="paper",
                symbol="BTC-USD",
                side="BUY",
                quantity=1,
                order_type="limit",
                max_loss=1500.0,
                metadata={"max_loss": 1500.0},
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"


def test_risk_guard_blocks_projected_total_exposure() -> None:
    guard = RiskGuard()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.50,
                "max_total_exposure_pct": 0.25,
            },
        },
        account_state=type(
            "AccountStateStub",
            (),
            {
                "equity": 10_000.0,
                "daily_trades": 0,
                "open_positions": 1,
                "positions": [
                    {
                        "symbol": "ETH-USD",
                        "quantity": 10.0,
                        "quantity_unit": "asset",
                        "current_price": 100.0,
                    }
                ],
            },
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=20.0,
                order_type="market",
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_total_exposure_pct"


def test_risk_guard_uses_explicit_contract_notional() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "daily_trades": 0,
            "open_positions": 1,
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "quantity": 2.0,
                    "quantity_unit": "contracts",
                    "current_price": 25_000.0,
                    "gross_notional": 1_000.0,
                }
            ],
        },
    )()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.50,
                "max_total_exposure_pct": 0.25,
            },
        },
        account_state=account_state,
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.DERIBIT,
            supports_spot=True,
            supports_futures=True,
        ),
        intents=[
            OrderIntent(
                broker_code="deribit",
                symbol="ETH-USD",
                side="BUY",
                quantity=10.0,
                order_type="market",
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is True


def test_risk_guard_blocks_contract_position_without_notional() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "daily_trades": 0,
            "open_positions": 1,
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "quantity": 2.0,
                    "quantity_unit": "contracts",
                    "current_price": 25_000.0,
                }
            ],
        },
    )()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.50,
                "max_total_exposure_pct": 0.90,
            },
        },
        account_state=account_state,
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.DERIBIT,
            supports_spot=True,
            supports_futures=True,
        ),
        intents=[
            OrderIntent(
                broker_code="deribit",
                symbol="ETH-USD",
                side="BUY",
                quantity=1.0,
                order_type="market",
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "account_position_valuation_unavailable"


def test_risk_guard_blocks_explicit_notional_without_position_quantity() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "daily_trades": 0,
            "open_positions": 1,
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "quantity_unit": "contracts",
                    "gross_notional": 1_000.0,
                }
            ],
        },
    )()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.50,
                "max_total_exposure_pct": 0.90,
            },
        },
        account_state=account_state,
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.DERIBIT,
            supports_spot=True,
            supports_futures=True,
        ),
        intents=[
            OrderIntent(
                broker_code="deribit",
                symbol="ETH-USD",
                side="BUY",
                quantity=1.0,
                order_type="market",
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "account_position_valuation_unavailable"


def test_risk_guard_uses_futures_intent_notional_value() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "daily_trades": 0,
            "open_positions": 0,
            "positions": [],
        },
    )()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.25,
                "max_total_exposure_pct": 0.90,
            },
        },
        account_state=account_state,
        execution_mode=ExecutionMode.FUTURES,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.DERIBIT,
            supports_spot=False,
            supports_futures=True,
        ),
        intents=[
            OrderIntent(
                broker_code="deribit",
                symbol="BTC-PERP",
                side="BUY",
                quantity=1.0,
                order_type="market",
                metadata={
                    "contract_type": "perpetual",
                    "notional_value": 3_000.0,
                },
            )
        ],
        market_data={"price": 100.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"
    assert decision.context["projected_notional"] == 3_000.0


def test_risk_guard_rejects_options_fallback_without_multiplier() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "daily_trades": 0,
            "open_positions": 0,
            "positions": [],
        },
    )()

    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={
            "require_stop_loss": True,
            "risk_limits": {
                "max_position_pct": 0.90,
                "max_total_exposure_pct": 0.90,
            },
        },
        account_state=account_state,
        execution_mode=ExecutionMode.OPTIONS_SINGLE,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.IBKR,
            supports_spot=False,
            supports_options=True,
        ),
        intents=[
            OptionsIntent(
                broker_code="ibkr",
                symbol="ASML-OPTION",
                side="BUY",
                quantity=1.0,
                order_type="market",
                underlying_price=650.0,
                metadata={},
            )
        ],
        market_data={"price": 650.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "projected_position_valuation_unavailable"


def test_risk_guard_post_trade_blocks_contract_without_notional() -> None:
    guard = RiskGuard()
    account_state = type(
        "AccountStateStub",
        (),
        {
            "equity": 10_000.0,
            "positions": [
                {
                    "symbol": "BTC-PERP",
                    "quantity": 2.0,
                    "quantity_unit": "contracts",
                    "current_price": 25_000.0,
                }
            ],
        },
    )()

    decision = guard.evaluate_post_trade(
        user_id="user-1",
        profile={},
        user_strategy_config={},
        account_state=account_state,
    )

    assert decision.allowed is False
    assert decision.rule_code == "account_position_valuation_unavailable"


# ── RG-1: daily-loss / drawdown caps now actually fire ───────────────────
# Before RG-1 these caps were fail-open because day_start_equity / peak_equity
# were never populated. _execute now injects them (resolve_risk_baseline), so
# the guard evaluates them. These tests feed the baselines directly.

_CAPS = BrokerCapabilities(
    broker_type=BrokerType.COINBASE,
    supports_spot=True,
    supports_margin=False,
    supports_futures=False,
    supports_perpetual=False,
    supports_options=False,
)


def _policy(**caps: float) -> dict:
    snapshot = {"binding_id": 1, "risk_caps": caps, "config": {"risk_caps": caps}}
    return {"_execution_policy_snapshot": snapshot}


def _account_stub(equity: float):
    return type(
        "AccountStateStub",
        (),
        {"equity": equity, "daily_trades": 0, "open_positions": 0},
    )()


def _small_long() -> OrderIntent:
    return OrderIntent(
        broker_code="coinbase", symbol="BTC-USD", side="BUY", quantity=0.0001, order_type="market"
    )


def _target_override(
    *,
    target_quantity: str,
    strategy_quantity: str,
    broker_quantity: str,
    reference_price: str = "200",
    target_allocation: str = "0.25",
) -> TargetPositionQuantityOverride:
    observed_at = datetime.now(tz=UTC)
    return TargetPositionQuantityOverride(
        account_plan_id="a" * 64,
        plan_leg_id="b" * 64,
        symbol="BTC-USD",
        target_allocation=Decimal(target_allocation),
        target_quantity=Decimal(target_quantity),
        strategy_quantity=Decimal(strategy_quantity),
        broker_quantity=Decimal(broker_quantity),
        target_weight_drift_fraction=Decimal("0"),
        broker_observed_at=observed_at,
        reference_price=Decimal(reference_price),
        quote_observed_at=observed_at,
    )


def _target_intent(
    override: TargetPositionQuantityOverride,
    *,
    reduce_only: bool | None = None,
    quantity: Decimal | None = None,
) -> OrderIntent:
    delta = override.delta_quantity
    reduction = delta < 0
    metadata: dict[str, object] = {
        "purpose": "close_position" if reduction else "target_position_entry",
        "reference_price": override.reference_price,
        "rebalance_target_override": {
            "account_plan_id": override.account_plan_id,
            "plan_leg_id": override.plan_leg_id,
            "target_allocation": str(override.target_allocation),
            "target_quantity": str(override.target_quantity),
            "revalidated_target_quantity": str(override.target_quantity),
            "strategy_quantity": str(override.strategy_quantity),
            "broker_quantity": str(override.broker_quantity),
            "delta_quantity": str(delta),
            "projected_broker_quantity": str(override.broker_quantity + delta),
            "target_weight_drift_fraction": str(override.target_weight_drift_fraction),
            "broker_observed_at": override.broker_observed_at.isoformat(),
            "reference_price": str(override.reference_price),
            "quote_observed_at": override.quote_observed_at.isoformat(),
        },
    }
    if reduce_only is not None:
        metadata["reduce_only"] = reduce_only
    return OrderIntent(
        broker_code="coinbase",
        symbol=override.symbol,
        side="SELL" if reduction else "BUY",
        quantity=float(quantity if quantity is not None else abs(delta)),
        order_type="market",
        metadata=metadata,
    )


def _close_override() -> CloseQuantityOverride:
    return CloseQuantityOverride(
        account_plan_id="c" * 64,
        plan_leg_id="d" * 64,
        symbol="BTC-USD",
        quantity=Decimal("100"),
        strategy_quantity=Decimal("100"),
        broker_quantity=Decimal("100"),
        broker_observed_at=datetime.now(tz=UTC),
    )


def _close_intent(override: CloseQuantityOverride) -> OrderIntent:
    return OrderIntent(
        broker_code="coinbase",
        symbol=override.symbol,
        side="SELL",
        quantity=float(override.quantity),
        order_type="market",
        metadata={
            "purpose": "close_position",
            "rebalance_close_override": {
                "account_plan_id": override.account_plan_id,
                "plan_leg_id": override.plan_leg_id,
                "strategy_quantity": str(override.strategy_quantity),
                "broker_quantity": str(override.broker_quantity),
                "clamped_quantity": str(override.quantity),
                "broker_observed_at": override.broker_observed_at.isoformat(),
            },
        },
    )


def _breached_account() -> AccountState:
    return AccountState(
        broker=BrokerType.COINBASE,
        equity=5_000.0,
        available_cash=0.0,
        open_positions=20,
        positions=[
            {
                "symbol": "BTC-USD",
                "quantity": 100.0,
                "quantity_unit": "asset",
                "current_price": 200.0,
            }
        ],
    )


def _portfolio_long() -> Signal:
    signal = _signal(SignalAction.LONG)
    signal.metadata["portfolio_stop_policy"] = "account_drawdown_mandate_v1"
    return signal


class _QuoteProvider:
    async def get_quote(
        self,
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ) -> dict[str, object]:
        del user_id, environment
        assert symbol == "BTC-USD"
        return {"price": 75.0, "timestamp": datetime.now(tz=UTC)}


def _portfolio_flow_engine(
    monkeypatch: Any,
    *,
    market_data_provider: Any | None = None,
) -> tuple[ExecutionEngine, _FakeBroker, _FakeBreachStore]:
    breach_store = _FakeBreachStore(mandates=[{"max_drawdown_pct": 0.15}])
    broker = _FakeBroker()
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        risk_breach_store=breach_store,
        market_data_provider=market_data_provider,
        default_mode="paper",
        allow_live=False,
    )

    async def _fake_get_broker(*args: Any, **kwargs: Any) -> _FakeBroker:
        del args, kwargs
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_settlement_currency",
        lambda _signal: "USD",
    )
    return engine, broker, breach_store


def _run_portfolio_flow(
    engine: ExecutionEngine,
    *,
    target_position_override: TargetPositionQuantityOverride | None = None,
) -> ExecutionResult:
    return asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_owned_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
                "require_stop_loss": False,
            },
            signal=_portfolio_long(),
            target_position_override=target_position_override,
        )
    )


def test_portfolio_stop_policy_requires_explicit_binding_override() -> None:
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={"peak_equity": 10_000.0},
        user_strategy_config={},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is False
    assert decision.rule_code == "portfolio_stop_override_required"


def test_portfolio_stop_policy_never_blocks_risk_reducing_close() -> None:
    signal = _signal(SignalAction.CLOSE)
    signal.metadata["portfolio_stop_policy"] = "account_drawdown_mandate_v1"
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=signal,
        profile={},
        user_strategy_config={},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is True


def test_portfolio_stop_policy_requires_user_owned_drawdown_mandate() -> None:
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={"peak_equity": 10_000.0},
        user_strategy_config={"require_stop_loss": False},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is False
    assert decision.rule_code == "portfolio_drawdown_mandate_missing"


def test_portfolio_stop_policy_requires_account_peak_equity_baseline() -> None:
    store = _FakeBreachStore(mandates=[{"max_drawdown_pct": 0.15}])
    decision = RiskGuard(breach_store=store).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={
            "peak_equity": 10_000.0,
            "risk_baseline_has_persisted_account_peak": False,
            "risk_baseline_current_equity_from_broker": True,
        },
        user_strategy_config={"require_stop_loss": False},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is False
    assert decision.rule_code == "portfolio_drawdown_baseline_missing"


def test_portfolio_stop_policy_allows_entry_with_mandate_and_baseline() -> None:
    store = _FakeBreachStore(mandates=[{"max_drawdown_pct": 0.15}])
    decision = RiskGuard(breach_store=store).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={
            "peak_equity": 10_000.0,
            "risk_baseline_has_persisted_account_peak": True,
            "risk_baseline_current_equity_from_broker": True,
        },
        user_strategy_config={"require_stop_loss": False},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is True


def test_portfolio_stop_policy_rejects_profile_cache_current_equity() -> None:
    store = _FakeBreachStore(mandates=[{"max_drawdown_pct": 0.15}])
    decision = RiskGuard(breach_store=store).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={
            "peak_equity": 10_000.0,
            "risk_baseline_has_persisted_account_peak": True,
            "risk_baseline_current_equity_from_broker": False,
        },
        user_strategy_config={"require_stop_loss": False},
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is False
    assert decision.rule_code == "portfolio_drawdown_baseline_missing"
    assert "authoritative broker account" in (decision.message or "")


def test_portfolio_stop_policy_still_enforces_mandated_drawdown_ceiling() -> None:
    store = _FakeBreachStore(mandates=[{"max_drawdown_pct": 0.05}])
    decision = RiskGuard(breach_store=store).evaluate(
        user_id="user-1",
        signal=_portfolio_long(),
        profile={
            "peak_equity": 10_000.0,
            "risk_baseline_has_persisted_account_peak": True,
            "risk_baseline_current_equity_from_broker": True,
        },
        user_strategy_config={"require_stop_loss": False},
        account_state=_account_stub(9_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_drawdown_pct"


def test_portfolio_flow_blocks_bootstrap_peak_without_persisted_authority(monkeypatch) -> None:
    engine, broker, breach_store = _portfolio_flow_engine(monkeypatch)
    monkeypatch.setattr(
        "execution_engine._execute.resolve_risk_baseline",
        lambda *args, **kwargs: RiskBaseline(
            day_start_equity=10_000.0,
            peak_equity=10_000.0,
            current_equity_observation=kwargs["equity_observation"],
        ),
    )

    result = _run_portfolio_flow(engine)

    assert result.success is False
    assert "durable account-owned" in (result.error_message or "")
    assert broker.submit_calls == 0
    assert breach_store.records[-1]["rule_code"] == "portfolio_drawdown_baseline_missing"


def test_portfolio_flow_allows_exact_persisted_peak_provenance(monkeypatch) -> None:
    engine, broker, breach_store = _portfolio_flow_engine(monkeypatch)
    monkeypatch.setattr(
        "execution_engine._execute.resolve_risk_baseline",
        lambda *args, **kwargs: RiskBaseline(
            day_start_equity=10_000.0,
            peak_equity=10_000.0,
            peak_provenance=RiskPeakProvenance(
                user_id="user-1",
                account_id=101,
                currency="USD",
                source="execution_metric",
                source_id="metric-1",
                equity=10_000.0,
                observed_at=datetime.now(tz=UTC),
            ),
            current_equity_observation=kwargs["equity_observation"],
        ),
    )

    result = _run_portfolio_flow(engine)

    assert result.success is True
    assert broker.submit_calls == 1
    assert breach_store.records == []


def test_portfolio_flow_blocks_when_baseline_database_errors(monkeypatch) -> None:
    engine, broker, _breach_store = _portfolio_flow_engine(monkeypatch)

    def _raise_database_error(*args: Any, **kwargs: Any) -> RiskBaseline:
        del args, kwargs
        raise SQLAlchemyError("risk baseline database unavailable")

    monkeypatch.setattr(
        "execution_engine._execute.resolve_risk_baseline",
        _raise_database_error,
    )

    result = _run_portfolio_flow(engine)

    assert result.success is False
    assert result.block_reason == "risk_baseline_unavailable"
    assert broker.submit_calls == 0


def test_target_override_reference_price_seeds_paper_broker(monkeypatch) -> None:
    engine, broker, _breach_store = _portfolio_flow_engine(
        monkeypatch,
        market_data_provider=_QuoteProvider(),
    )
    monkeypatch.setattr(
        "execution_engine._execute.resolve_risk_baseline",
        lambda *args, **kwargs: RiskBaseline(
            day_start_equity=10_000.0,
            peak_equity=10_000.0,
            peak_provenance=RiskPeakProvenance(
                user_id="user-1",
                account_id=101,
                currency="USD",
                source="execution_metric",
                source_id="metric-1",
                equity=10_000.0,
                observed_at=datetime.now(tz=UTC),
            ),
            current_equity_observation=kwargs["equity_observation"],
        ),
    )
    observed_at = datetime.now(tz=UTC)
    override = TargetPositionQuantityOverride(
        account_plan_id="a" * 64,
        plan_leg_id="b" * 64,
        symbol="BTC-USD",
        target_allocation=Decimal("0.01"),
        target_quantity=Decimal("1"),
        strategy_quantity=Decimal("0"),
        broker_quantity=Decimal("0"),
        target_weight_drift_fraction=Decimal("0.15"),
        broker_observed_at=observed_at,
        reference_price=Decimal("125"),
        quote_observed_at=observed_at,
    )

    result = _run_portfolio_flow(engine, target_position_override=override)

    assert result.success is True
    assert broker.seeded_prices == [("BTC-USD", 125.0)]


def test_rg1_daily_loss_cap_fires_with_baseline() -> None:
    # equity 9000 vs day_start 10000 -> 10% daily loss > 5% cap.
    guard = RiskGuard(breach_store=_FakeBreachStore())
    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 9_000.0, "day_start_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.50,
            max_position_pct=1.0,
            max_open_positions=100,
        ),
        account_state=_account_stub(9_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_small_long()],
        market_data={"price": 3000.0},
    )
    assert decision.allowed is False
    assert decision.rule_code == "max_daily_loss_pct"


def test_rg1_drawdown_cap_fires_with_baseline() -> None:
    # day_start == equity (no daily loss); peak 10000 vs equity 9000 -> 10%
    # drawdown > 5% cap.
    guard = RiskGuard(breach_store=_FakeBreachStore())
    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 9_000.0, "day_start_equity": 9_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.50,
            max_drawdown_pct=0.05,
            max_position_pct=1.0,
            max_open_positions=100,
        ),
        account_state=_account_stub(9_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_small_long()],
        market_data={"price": 3000.0},
    )
    assert decision.allowed is False
    assert decision.rule_code == "max_drawdown_pct"


def test_exact_target_reduction_bypasses_only_exposure_increase_caps() -> None:
    override = _target_override(
        target_quantity="50",
        strategy_quantity="100",
        broker_quantity="100",
    )
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.CLOSE),
        profile={"day_start_equity": 10_000.0, "peak_equity": 12_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.01,
            max_drawdown_pct=0.05,
            max_position_pct=0.01,
            max_total_exposure_pct=0.01,
            max_open_positions=1,
        ),
        account_state=_breached_account(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_target_intent(override, reduce_only=True)],
        market_data={"price": 1.0},
        target_position_override=override,
    )

    assert decision.allowed is True


def test_exact_clamped_close_bypasses_only_exposure_increase_caps() -> None:
    override = _close_override()
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.CLOSE),
        profile={"day_start_equity": 10_000.0, "peak_equity": 12_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.01,
            max_drawdown_pct=0.05,
            max_position_pct=0.01,
            max_total_exposure_pct=0.01,
            max_open_positions=1,
        ),
        account_state=_breached_account(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_close_intent(override)],
        market_data={"price": 200.0},
        close_quantity_override=override,
    )

    assert decision.allowed is True


def test_free_form_close_does_not_bypass_daily_loss_cap() -> None:
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.CLOSE),
        profile={"day_start_equity": 10_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.01,
            max_drawdown_pct=0.50,
            max_position_pct=1.0,
            max_total_exposure_pct=1.0,
            max_open_positions=100,
        ),
        account_state=_breached_account(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="SELL",
                quantity=50.0,
                metadata={"purpose": "close_position", "reduce_only": True},
            )
        ],
        market_data={"price": 200.0},
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_daily_loss_pct"


def test_malformed_typed_reduction_is_valued_and_blocked() -> None:
    override = _target_override(
        target_quantity="50",
        strategy_quantity="100",
        broker_quantity="100",
    )
    malformed = _target_intent(
        override,
        reduce_only=False,
        quantity=Decimal("25"),
    )
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.CLOSE),
        profile={"day_start_equity": 5_000.0, "peak_equity": 5_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.50,
            max_drawdown_pct=0.50,
            max_position_pct=0.10,
            max_total_exposure_pct=1.0,
            max_open_positions=100,
        ),
        account_state=AccountState(
            broker=BrokerType.COINBASE,
            equity=5_000.0,
            available_cash=5_000.0,
        ),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[malformed],
        market_data={"price": 200.0},
        target_position_override=override,
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"
    assert decision.context["projected_notional"] == 5_000.0


def test_target_entry_risk_uses_authoritative_override_reference_quote() -> None:
    override = _target_override(
        target_quantity="10",
        strategy_quantity="0",
        broker_quantity="0",
        reference_price="250",
    )
    intent = _target_intent(override)
    assert intent.metadata is not None
    intent.metadata["notional_value"] = "1"
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=replace(
            _signal(SignalAction.LONG, stop_loss=9.0),
            entry_price=10.0,
        ),
        profile={"day_start_equity": 10_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.50,
            max_drawdown_pct=0.50,
            max_position_pct=0.10,
            max_total_exposure_pct=1.0,
            max_open_positions=100,
        ),
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[intent],
        market_data={"price": 20.0},
        target_position_override=override,
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"
    assert decision.context["projected_notional"] == 2_500.0


def test_target_entry_position_cap_includes_manual_broker_incumbent() -> None:
    override = _target_override(
        target_quantity="3",
        strategy_quantity="0",
        broker_quantity="10",
        reference_price="100",
        target_allocation="0.03",
    )
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"day_start_equity": 10_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.50,
            max_drawdown_pct=0.50,
            max_position_pct=0.10,
            max_total_exposure_pct=1.0,
            max_open_positions=100,
        ),
        account_state=AccountState(
            broker=BrokerType.COINBASE,
            equity=10_000.0,
            available_cash=9_000.0,
            open_positions=1,
            positions=[
                {
                    "symbol": "BTC-USD",
                    "quantity": 10.0,
                    "quantity_unit": "asset",
                    "current_price": 100.0,
                }
            ],
        ),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_target_intent(override)],
        market_data={"price": 1.0},
        target_position_override=override,
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_position_pct"
    assert decision.context["projected_notional"] == 1_300.0
    assert decision.context["projected_position_pct"] == 0.13


def test_target_entry_total_exposure_adds_only_incremental_notional() -> None:
    override = _target_override(
        target_quantity="3",
        strategy_quantity="0",
        broker_quantity="10",
        reference_price="100",
        target_allocation="0.03",
    )
    decision = RiskGuard(breach_store=_FakeBreachStore()).evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"day_start_equity": 10_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.50,
            max_drawdown_pct=0.50,
            max_position_pct=0.20,
            max_total_exposure_pct=0.12,
            max_open_positions=100,
        ),
        account_state=AccountState(
            broker=BrokerType.COINBASE,
            equity=10_000.0,
            available_cash=9_000.0,
            open_positions=1,
            positions=[
                {
                    "symbol": "BTC-USD",
                    "quantity": 10.0,
                    "quantity_unit": "asset",
                    "current_price": 100.0,
                }
            ],
        ),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_target_intent(override)],
        market_data={"price": 1.0},
        target_position_override=override,
    )

    assert decision.allowed is False
    assert decision.rule_code == "max_total_exposure_pct"
    assert decision.context["existing_exposure"] == 1_000.0
    assert decision.context["projected_notional"] == 300.0
    assert decision.context["projected_total_exposure"] == 1_300.0


def test_rg1_caps_inert_when_bootstrapped_to_current_equity() -> None:
    # Bootstrap (no history): day_start == peak == current equity -> 0% loss,
    # 0% drawdown -> caps do NOT block (inert, not fail-closed).
    guard = RiskGuard(breach_store=_FakeBreachStore())
    decision = guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0, "day_start_equity": 10_000.0, "peak_equity": 10_000.0},
        user_strategy_config=_policy(
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.05,
            max_position_pct=1.0,
            max_open_positions=100,
        ),
        account_state=_account_stub(10_000.0),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=_CAPS,
        intents=[_small_long()],
        market_data={"price": 3000.0},
    )
    assert decision.allowed is True
    assert decision.rule_code is None


def _max_position_decision(*, price: float):  # type: ignore[no-untyped-def]
    # equity 10_000, qty 1 @ `price` -> projected_pct = price/10_000.
    guard = RiskGuard(breach_store=_FakeBreachStore(mandates=[{"max_position_pct": 0.10}]))
    return guard.evaluate(
        user_id="user-1",
        signal=_signal(SignalAction.LONG, stop_loss=95.0),
        profile={"equity": 10_000.0},
        user_strategy_config={"max_position_pct": 0.10},
        account_state=type(
            "AccountStateStub",
            (),
            {"equity": 10_000.0, "daily_trades": 0, "open_positions": 0},
        )(),
        execution_mode=ExecutionMode.SPOT,
        broker_capabilities=BrokerCapabilities(
            broker_type=BrokerType.COINBASE,
            supports_spot=True,
            supports_margin=False,
            supports_futures=False,
            supports_perpetual=False,
            supports_options=False,
        ),
        intents=[
            OrderIntent(
                broker_code="coinbase",
                symbol="BTC-USD",
                side="BUY",
                quantity=1.0,
                order_type="market",
            )
        ],
        market_data={"price": price},
    )


def test_max_position_tolerance_allows_marginal_overshoot_but_blocks_real_breach() -> None:
    # N6: the sizer caps notional to exactly the cap; the risk guard re-estimates from
    # market_data and can land a hair over (0.1004 vs a 0.10 cap). A sub-0.5% overshoot
    # must NOT block (else ~all entries are blocked); a clear breach still blocks.
    within = _max_position_decision(price=1004.0)  # projected 0.1004, within 0.5% of 0.10
    assert within.rule_code != "max_position_pct"
    breach = _max_position_decision(price=1100.0)  # projected 0.11, a real breach
    assert breach.allowed is False
    assert breach.rule_code == "max_position_pct"


def test_risk_guard_compares_multicurrency_intent_in_account_currency() -> None:
    observed_at = datetime(2026, 7, 15, 12, tzinfo=UTC)
    intent = OrderIntent(
        broker_code="paper",
        symbol="BTC-USDC",
        side="BUY",
        quantity=0.048,
        order_type="market",
        currency_context=OrderCurrencyContext(
            account_currency="EUR",
            settlement_currency="USDC",
            account_to_settlement_rate=Decimal("1.2"),
            requested_at=observed_at,
            observed_at=observed_at,
            source="coinbase_live",
        ),
    )

    notional = RiskGuard(breach_store=_FakeBreachStore())._estimate_notional(
        [intent],
        _signal(SignalAction.LONG, stop_loss=45_000.0),
        {"price": 50_000.0},
    )

    assert notional == 2_000.0
