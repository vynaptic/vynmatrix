"""Golden-output snapshots for ``ExecutionEngine.handle_signal``.

These tests lock in the exact ``ExecutionResult`` shape returned for each
canonical order path. The Phase-3 decomposition reduced
``apps/execution_engine/execution_engine/engine.py`` below 1,000 lines by
extracting injected collaborators; these snapshots prevent later orchestration
changes from silently changing observable behaviour.

Each scenario:

1. Builds a deterministic ``Signal`` + ``profile`` + ``user_strategy_config``.
2. Invokes ``engine.handle_signal(...)``.
3. Strips non-deterministic fields (``executed_at``) and asserts against an
   expected dictionary captured from the *current* implementation.

When the engine is changed, these tests must pass unchanged. If a
snapshot diverges, the refactor either changed semantics (intentional → update
the snapshot in the same PR with rationale) or introduced a regression (fix
the refactor).

The scenarios intentionally cover only the slices that have stable contracts
today: happy spot fill, broker-circuit-blocked, live-mode-rejected, and
broker-route fallback. Options and futures golden tests should be added when
those paths are exercised in this harness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from execution_engine.brokers.base import (
    AccountInfo,
    BrokerCapabilities,
    BrokerOrderResult,
)
from execution_engine.config import BrokerType
from execution_engine.engine import ExecutionEngine
from execution_engine.models import OrderIntent
from execution_engine.order_builder import OrderBuilder
from lib_strategy.signals.signal import Signal, SignalAction

# ---------------------------------------------------------------------------
# Test doubles. Kept in this module (not conftest) to make the golden contract
# self-contained — anyone reading the test can see exactly what is being
# fed to the engine.
# ---------------------------------------------------------------------------


class _FixedOrderBuilder(OrderBuilder):
    """OrderBuilder that always emits a single deterministic OrderIntent."""

    def build(  # type: ignore[no-untyped-def]
        self,
        req,
        account,
        market_data,
    ) -> list[OrderIntent]:
        return [
            OrderIntent(
                broker_code="coinbase",
                symbol=req.signal.symbol,
                side="BUY" if req.signal.action == SignalAction.LONG else "SELL",
                quantity=1.0,
                order_type="market",
                metadata={"signal_id": req.signal.signal_id},
            )
        ]


class _FilledBroker:
    """Minimal broker double that always reports a filled order at $100."""

    def __init__(self) -> None:
        self._connected = False
        self.submit_calls = 0

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
        return BrokerOrderResult.filled(
            order_id="o-1",
            quantity=intent.quantity,
            price=100.0,
        )

    async def submit_options_order(self, intent):  # type: ignore[no-untyped-def]
        msg = "Options flow not expected in spot golden tests"
        raise AssertionError(msg)

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def get_order_status(self, order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult.filled(order_id=order_id, quantity=1.0, price=100.0)

    async def get_positions(self):  # type: ignore[no-untyped-def]
        return []


def _signal(*, action: SignalAction = SignalAction.LONG, symbol: str = "BTC-USD") -> Signal:
    """Return a fresh Signal so the production paper-age gate reaches the tested path."""
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol=symbol,
        asset_class="crypto",
        action=action,
        confidence=0.8,
        timestamp=datetime.now(tz=UTC),
        entry_price=100.0,
        stop_loss=95.0,
        external_signal_id=f"ext-golden-{symbol}-{action.value.lower()}",
    )


def _paper_profile(*, profile_sandbox: bool = True) -> dict[str, Any]:
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
        "sandbox": profile_sandbox,
        "equity": 10_000.0,
        "available_cash": 10_000.0,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "accounts": {str(account_id): account},
        "brokers": {"coinbase": {"credential_ref": "coinbase-paper"}},
        "_broker_route_snapshot": {
            "broker": "coinbase",
            "broker_environment": "paper",
            "broker_account_id": account_id,
            "sandbox": True,
            "allowed_brokers": ["coinbase"],
            "route_source": "scoring_policy",
            "live_enabled": False,
            "asset_class": "crypto",
            "execution_mode": "spot",
        },
    }


def _install_owned_account_route(
    engine: ExecutionEngine,
    monkeypatch: Any,
) -> None:
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

    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_account_id",
        _resolve_account_id,
    )
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_account_currency",
        _resolve_account_currency,
    )


def _strip_volatile(result_dict: dict[str, Any]) -> dict[str, Any]:
    """Drop fields whose value is intentionally non-deterministic."""
    out = dict(result_dict)
    out.pop("executed_at", None)
    return out


# ---------------------------------------------------------------------------
# Scenario 1 — Spot LONG happy path with broker route snapshot.
# Locks in: success=True, single filled order at $100, broker=coinbase, paper.
# ---------------------------------------------------------------------------


def test_golden_spot_long_happy_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)

    signal = _signal()
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
            },
            signal=signal,
        )
    )

    assert _strip_volatile(result.to_dict()) == {
        "success": True,
        "signal_id": signal.signal_id,
        "symbol": "BTC-USD",
        "execution_mode": "spot",
        "broker": "coinbase",
        "broker_account_id": 101,
        "orders_submitted": 1,
        "orders_filled": 1,
        "total_quantity": 1.0,
        "average_price": 100.0,
        "total_commission": 0.0,
        "order_results": [
            {
                "order_id": "o-1",
                "status": "filled",
                "filled_quantity": 1.0,
                "average_price": 100.0,
                "commission": 0.0,
                "commission_currency": None,
                "error": None,
            }
        ],
        "error_message": None,
    }
    assert broker.submit_calls == 1


# ---------------------------------------------------------------------------
# Scenario 2 — Broker-global circuit breaker blocks the trade.
# Locks in: success=False, execution_mode="blocked", error_message contains
# the exact 'Broker-global circuit breaker' phrase.
# ---------------------------------------------------------------------------


def test_golden_broker_global_circuit_breaker_blocks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(order_builder=OrderBuilder())
    _install_owned_account_route(engine, monkeypatch)
    breaker_key = engine._breaker_manager.broker_key(
        broker_code="coinbase",
        environment="paper",
    )
    engine.open_circuit_breaker(
        scope="broker",
        breaker_key=breaker_key,
        user_id="user-1",
        strategy_id="swing_high_low_pmo_v1",
        broker="coinbase",
        environment="paper",
        reason="service unavailable",
    )

    signal = _signal()
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={"execution_mode": "spot"},
            signal=signal,
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    # Lock the deterministic fields and assert the error preamble verbatim.
    assert snapshot["success"] is False
    assert snapshot["execution_mode"] == "blocked"
    assert snapshot["orders_submitted"] == 0
    assert snapshot["orders_filled"] == 0
    assert snapshot["total_quantity"] == 0.0
    assert snapshot["average_price"] == 0.0
    assert snapshot["total_commission"] == 0.0
    assert snapshot["order_results"] == []
    assert snapshot["signal_id"] == signal.signal_id
    assert snapshot["symbol"] == "BTC-USD"
    error_message = snapshot["error_message"] or ""
    assert error_message.startswith("Broker-global circuit breaker"), error_message


# ---------------------------------------------------------------------------
# Scenario 3 — Route snapshot forces paper environment even when profile
# claims sandbox=False. This is a critical safety contract: the policy
# snapshot wins so a misconfigured profile cannot leak to live brokers.
# ---------------------------------------------------------------------------


def test_golden_route_snapshot_paper_overrides_profile_live(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()
    observed: dict[str, str] = {}

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        observed["broker_type"] = broker_type.value
        observed["environment"] = environment
        observed["credential_ref"] = credential_ref
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)

    signal = _signal()
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            # Profile says live-ish, but the immutable route snapshot says
            # paper. The snapshot remains authoritative.
            profile=_paper_profile(profile_sandbox=False),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
            },
            signal=signal,
        )
    )

    assert result.success is True
    # Critical safety contract — verbatim values, not heuristic checks.
    assert observed == {
        "broker_type": "coinbase",
        "environment": "paper",
        "credential_ref": "coinbase-paper",
    }
    assert broker.submit_calls == 1
    assert _strip_volatile(result.to_dict()) == {
        "success": True,
        "signal_id": signal.signal_id,
        "symbol": "BTC-USD",
        "execution_mode": "spot",
        "broker": "coinbase",
        "broker_account_id": 101,
        "orders_submitted": 1,
        "orders_filled": 1,
        "total_quantity": 1.0,
        "average_price": 100.0,
        "total_commission": 0.0,
        "order_results": [
            {
                "order_id": "o-1",
                "status": "filled",
                "filled_quantity": 1.0,
                "average_price": 100.0,
                "commission": 0.0,
                "commission_currency": None,
                "error": None,
            }
        ],
        "error_message": None,
    }


# ---------------------------------------------------------------------------
# Scenario 4 — SHORT signal on a spot-only broker is rejected by the risk
# guard with the exact message ``"SHORT blocked for broker=<broker>"`` BEFORE
# any order is built or submitted. This is a critical safety contract: the
# spot path must not silently downgrade or accept short positions.
# ---------------------------------------------------------------------------


def test_golden_spot_short_signal_blocked_by_risk_guard(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_settlement_currency",
        lambda _signal: "USD",
    )

    signal = _signal(action=SignalAction.SHORT)
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
            },
            signal=signal,
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    # The signal_id differs from the LONG case because the canonical
    # signal-id hash includes the action. Lock the rest verbatim.
    assert snapshot == {
        "success": False,
        "signal_id": signal.signal_id,
        "symbol": "BTC-USD",
        "execution_mode": "blocked",
        "broker": "coinbase",
        "broker_account_id": 101,
        "settlement_currency": "USD",
        "orders_submitted": 0,
        "orders_filled": 0,
        "total_quantity": 0.0,
        "average_price": 0.0,
        "total_commission": 0.0,
        "order_results": [],
        "error_message": "SHORT blocked for broker=coinbase",
    }
    # No broker call must have happened — the guard ran before submit.
    assert broker.submit_calls == 0


# ---------------------------------------------------------------------------
# Scenario 5 — Duplicate signal short-circuit. The dedup gate runs BEFORE
# any broker work, so a repeated signal_id returns success=True with
# execution_mode="dedup" and no broker side effects.
# ---------------------------------------------------------------------------


def test_golden_duplicate_signal_short_circuits_dedup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)

    signal = _signal()
    profile = _paper_profile()
    config = {"broker": "coinbase", "execution_mode": "spot", "mode": "paper"}

    # First call — fills normally and primes the dedup cache.
    asyncio.run(
        engine.handle_signal(
            user_id="user-1", profile=profile, user_strategy_config=config, signal=signal
        )
    )
    assert broker.submit_calls == 1

    # Second call with the same signal_id must short-circuit on dedup,
    # NOT increment submit_calls.
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1", profile=profile, user_strategy_config=config, signal=signal
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    assert snapshot["success"] is True
    assert snapshot["execution_mode"] == "dedup"
    assert snapshot["orders_submitted"] == 0
    assert snapshot["orders_filled"] == 0
    assert (snapshot["error_message"] or "").startswith("Duplicate execution request skipped")
    assert broker.submit_calls == 1  # unchanged


# ---------------------------------------------------------------------------
# Scenario 6 — HOLD signal records the decision but submits no order.
# Locks the contract that HOLD is success=True with no broker work.
# ---------------------------------------------------------------------------


def test_golden_hold_signal_records_no_op(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)

    signal = _signal(action=SignalAction.HOLD)
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={"broker": "coinbase", "execution_mode": "spot", "mode": "paper"},
            signal=signal,
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    assert snapshot["success"] is True
    assert snapshot["execution_mode"] == "spot"
    assert snapshot["orders_submitted"] == 0
    assert snapshot["orders_filled"] == 0
    assert snapshot["error_message"] is None
    # No-op reason lets RCA distinguish a HOLD no-op from a real fill (EX-2).
    assert snapshot["reason"] == "hold_no_action"
    assert broker.submit_calls == 0


# ---------------------------------------------------------------------------
# Scenario 7 — NOTIFY_ONLY execution mode records the signal but submits
# nothing. The execution_mode in the result MUST be "notify_only".
# ---------------------------------------------------------------------------


def test_golden_notify_only_mode_records_signal_no_submit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_settlement_currency",
        lambda _signal: "USD",
    )

    signal = _signal()
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "notify_only",
                "mode": "paper",
            },
            signal=signal,
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    assert snapshot["success"] is True
    assert snapshot["execution_mode"] == "notify_only"
    assert snapshot["orders_submitted"] == 0
    assert snapshot["error_message"] is None
    assert snapshot["broker_account_id"] == 101
    assert snapshot["settlement_currency"] == "USD"
    # NOTIFY_ONLY is a deliberate alert-only no-op, tagged for RCA (EX-2).
    assert snapshot["reason"] == "notify_only"
    assert broker.submit_calls == 0


# ---------------------------------------------------------------------------
# Scenario 8 — Asset score below the user's ``min_score`` threshold blocks
# the trade. Locks in: success=False, execution_mode="blocked",
# error_message starts with the score-threshold preamble.
# ---------------------------------------------------------------------------


def test_golden_score_below_min_score_blocks_trade(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = ExecutionEngine(
        order_builder=_FixedOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FilledBroker()

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    _install_owned_account_route(engine, monkeypatch)

    signal = _signal()
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_paper_profile(),
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
                "min_score": 0.9,  # asset_score below this must block
            },
            signal=signal,
            score_context={"asset_score": 0.4},
        )
    )

    snapshot = _strip_volatile(result.to_dict())
    assert snapshot["success"] is False
    assert snapshot["execution_mode"] == "blocked"
    assert snapshot["orders_submitted"] == 0
    error = snapshot["error_message"] or ""
    assert error.startswith("Score 0.4 below threshold 0.9"), error
    assert broker.submit_calls == 0
