from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine._dispatch import DispatchResolver
from execution_engine.alerts import AlertPublisher, ExecutionAlert
from execution_engine.brokers.base import AccountInfo, BrokerCapabilities, BrokerOrderResult
from execution_engine.brokers.paper import PaperBroker
from execution_engine.config import BrokerType
from execution_engine.engine import ExecutionEngine
from execution_engine.execution_result import ExecutionResult
from execution_engine.execution_routing import CurrentAuthorityError, ExecutionRouteResolver
from execution_engine.models import OrderIntent, TargetPositionQuantityOverride
from execution_engine.order_builder import OrderBuilder
from lib_application.db.models import (
    Base,
    Broker,
    BrokerCredential,
    Instrument,
    LinkedBrokerAccount,
    Strategy,
    User,
    UserStrategyBinding,
)
from lib_common.config_validation import ExecutionPaperConfig, ExecutionRuntimeConfig
from lib_strategy.signals.signal import Signal, SignalAction


class _FakeWebhookClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, "json": json})

    def close(self) -> None:
        return None


def _signal(symbol: str = "BTC-USD") -> Signal:
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol=symbol,
        asset_class="crypto",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=datetime.now(tz=UTC),
        instrument_id=1,
        entry_price=100.0,
        stop_loss=95.0,
        external_signal_id="ext-execution-controls",
    )


def _live_signal(symbol: str = "BTC-USD") -> Signal:
    return replace(_signal(symbol), timestamp=datetime.now(tz=UTC))


def _write_valid_marker(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "passed",
                "completed_at": datetime.now(tz=UTC).isoformat(),
                "commit": "abc123",
                "symbols": ["BTC-USD"],
                "paper_window_days": 14,
                "duplicate_submission_count": 0,
                "operator": "tester",
                "sandbox_smoke_evidence": (
                    "apps/execution_engine/tests/test_coinbase_sandbox_integration.py"
                ),
                "paper_soak_evidence": ".artifacts/paper-soak/summary.json",
                "reconciliation_summary": ".artifacts/paper-soak/reconciliation.json",
                "notes": "",
            }
        ),
        encoding="utf-8",
    )


def _runtime_with_marker(path: Path) -> ExecutionRuntimeConfig:
    return ExecutionRuntimeConfig(sandbox_certification_marker=path)


def _account_profile(
    *,
    user_id: str,
    account_id: int,
    broker: str,
    environment: str,
    live_enabled: bool,
) -> dict[str, object]:
    credential_ref = f"{broker}-account-{account_id}"
    account: dict[str, object] = {
        "user_id": user_id,
        "broker": broker,
        "environment": environment,
        "status": "connected",
        "base_ccy": "USD",
        # Production profiles (providers_db) carry the per-account credential
        # pointer here; credential resolution is strictly account-scoped.
        "credential_ref": credential_ref,
    }
    if environment == "paper":
        account.update(
            paper_initial_equity=10_000.0,
            paper_initial_cash=10_000.0,
        )
    return {
        "broker": broker,
        "broker_account_id": account_id,
        "base_ccy": "USD",
        "sandbox": environment == "paper",
        "live_enabled": live_enabled,
        "equity": 10_000.0,
        "available_cash": 10_000.0,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "accounts": {str(account_id): account},
        "brokers": {broker: {"credential_ref": credential_ref}},
        "_broker_route_snapshot": {
            "broker": broker,
            "broker_environment": environment,
            "broker_account_id": account_id,
            "credential_ref": credential_ref,
            "sandbox": environment == "paper",
        },
    }


def _install_owned_account_route(engine: ExecutionEngine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    currencies: dict[int, str] = {}

    def _resolve_account_id(
        *,
        user_id,
        broker_type,
        environment,
        profile,
    ):  # type: ignore[no-untyped-def]
        account_id = int(profile["broker_account_id"])
        route = profile["_broker_route_snapshot"]
        assert int(route["broker_account_id"]) == account_id
        account = profile["accounts"][str(account_id)]
        assert account["user_id"] == user_id
        assert account["broker"] == broker_type.value
        assert account["environment"] == environment
        assert account["status"] == "connected"
        currency = str(account["base_ccy"])
        assert currency
        currencies[account_id] = currency
        return account_id

    def _resolve_account_currency(*, user_id, account_id):  # type: ignore[no-untyped-def]
        del user_id
        return currencies[int(account_id)]

    monkeypatch.setattr(engine._route_resolver, "resolve_account_id", _resolve_account_id)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_account_currency",
        _resolve_account_currency,
    )


def test_explicit_invalid_engine_mode_is_rejected() -> None:
    resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=None,
        canonical_execution_store=None,
    )

    with pytest.raises(ValueError, match="Unknown execution mode"):
        resolver.normalize_mode("papre")


def test_explicit_invalid_instrument_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown instrument execution_mode"):
        DispatchResolver._resolve_execution_mode(
            user_strategy_config={"execution_mode": "spoot"},
            score_ctx={},
            trace_ctx={},
        )


def _authority_session_factory(
    *,
    autopilot: bool,
    entries_enabled: bool,
    exits_enabled: bool,
) -> sessionmaker:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(User(user_id="user-1", email="user-1@example.invalid", base_ccy="USD"))
        session.add(Broker(broker_id=1, code="coinbase", name="Coinbase"))
        session.add_all(
            [
                Instrument(
                    instr_id=1,
                    asset_class="crypto",
                    canonical="BTC/USD",
                    settlement_currency="USD",
                ),
                Instrument(
                    instr_id=2,
                    asset_class="crypto",
                    canonical="ETH/USD",
                    settlement_currency="USD",
                ),
            ]
        )
        session.add(
            Strategy(
                strategy_id="swing_high_low_pmo_v1",
                strategy_name="Swing High Low PMO",
                asset_class="crypto",
            )
        )
        session.add(
            LinkedBrokerAccount(
                account_id=42,
                user_id="user-1",
                broker_id=1,
                environment="paper",
                display_name="Authority test account",
                base_ccy="USD",
                paper_initial_equity=Decimal("10000"),
                paper_initial_cash=Decimal("10000"),
                status="connected",
            )
        )
        session.flush()
        session.add(
            UserStrategyBinding(
                binding_id=7,
                user_id="user-1",
                strategy_id="swing_high_low_pmo_v1",
                broker_account_id=42,
                is_active=True,
                autopilot=autopilot,
                entries_enabled=entries_enabled,
                exits_enabled=exits_enabled,
                instruments_allowed=["BTC/USD"],
            )
        )
        session.add(
            BrokerCredential(
                cred_id=9,
                account_id=42,
                secret_ref="coinbase-account-42",
                status="active",
            )
        )
        session.commit()
    return sessions


def test_current_authority_allows_close_only_but_rejects_entry() -> None:
    sessions = _authority_session_factory(
        autopilot=False,
        entries_enabled=False,
        exits_enabled=True,
    )
    resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=sessions,
        canonical_execution_store=None,
    )

    authority = resolver.validate_current_authority(
        user_id="user-1",
        binding_id=7,
        strategy_id="swing_high_low_pmo_v1",
        account_id=42,
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-account-42",
        action="flat",
        instrument_id=1,
    )
    assert authority.credential_ref == "coinbase-account-42"

    # A read-only account refresh validates every revocable route boundary but
    # does not require entry authority merely because the model leg was LONG.
    route = resolver.validate_current_route(
        user_id="user-1",
        binding_id=7,
        strategy_id="swing_high_low_pmo_v1",
        account_id=42,
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-account-42",
        instrument_id=1,
    )
    assert route.credential_version == authority.credential_version

    with pytest.raises(CurrentAuthorityError, match="entry authority disabled"):
        resolver.validate_current_authority(
            user_id="user-1",
            binding_id=7,
            strategy_id="swing_high_low_pmo_v1",
            account_id=42,
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-account-42",
            action="long",
            instrument_id=1,
        )


def test_current_authority_rejects_revoked_credential() -> None:
    sessions = _authority_session_factory(
        autopilot=True,
        entries_enabled=True,
        exits_enabled=True,
    )
    resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=sessions,
        canonical_execution_store=None,
    )
    resolver.validate_current_authority(
        user_id="user-1",
        binding_id=7,
        strategy_id="swing_high_low_pmo_v1",
        account_id=42,
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-account-42",
        action="long",
        instrument_id=1,
    )
    with sessions() as session:
        credential = session.get(BrokerCredential, 9)
        assert credential is not None
        credential.status = "disabled"
        session.commit()

    with pytest.raises(CurrentAuthorityError, match="no usable current credential"):
        resolver.validate_current_authority(
            user_id="user-1",
            binding_id=7,
            strategy_id="swing_high_low_pmo_v1",
            account_id=42,
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-account-42",
            action="long",
            instrument_id=1,
        )


def test_current_authority_rejects_changed_binding_instrument_scope() -> None:
    sessions = _authority_session_factory(
        autopilot=True,
        entries_enabled=True,
        exits_enabled=True,
    )
    resolver = ExecutionRouteResolver(
        default_mode="paper",
        session_factory=sessions,
        canonical_execution_store=None,
    )

    def _validate() -> None:
        resolver.validate_current_authority(
            user_id="user-1",
            binding_id=7,
            strategy_id="swing_high_low_pmo_v1",
            account_id=42,
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-account-42",
            action="long",
            instrument_id=1,
        )

    _validate()

    with sessions() as session:
        binding = session.get(UserStrategyBinding, 7)
        assert binding is not None
        binding.instruments_allowed = ["ETH/USD"]
        session.commit()

    with pytest.raises(CurrentAuthorityError, match="no longer authorizes instrument BTC/USD"):
        _validate()

    # Read-only portfolio reconciliation may inspect the incumbent and a
    # separately proven internal CLOSE may liquidate it after scope removal.
    account_route = resolver.validate_current_rebalance_account_route(
        user_id="user-1",
        binding_id=7,
        strategy_id="swing_high_low_pmo_v1",
        account_id=42,
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-account-42",
        instrument_id=1,
    )
    reduction = resolver.validate_current_rebalance_reduction_authority(
        user_id="user-1",
        binding_id=7,
        strategy_id="swing_high_low_pmo_v1",
        account_id=42,
        broker_type=BrokerType.COINBASE,
        environment="paper",
        credential_ref="coinbase-account-42",
        action="close",
        instrument_id=1,
    )
    assert account_route.credential_version == reduction.credential_version

    # A generic CLOSE still has no allowlist exception, and the dedicated seam
    # cannot be repurposed for an entry.
    with pytest.raises(CurrentAuthorityError, match="no longer authorizes instrument BTC/USD"):
        resolver.validate_current_authority(
            user_id="user-1",
            binding_id=7,
            strategy_id="swing_high_low_pmo_v1",
            account_id=42,
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-account-42",
            action="close",
            instrument_id=1,
        )
    with pytest.raises(CurrentAuthorityError, match="requires exit execution semantics"):
        resolver.validate_current_rebalance_reduction_authority(
            user_id="user-1",
            binding_id=7,
            strategy_id="swing_high_low_pmo_v1",
            account_id=42,
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-account-42",
            action="long",
            instrument_id=1,
        )


def test_revoked_command_is_blocked_before_broker_resolution(monkeypatch) -> None:
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    # Production construction supplies a database session factory. The unit
    # flow keeps the existing lightweight stores while exercising the exact
    # pre-I/O authority boundary.
    engine._session_factory = object()
    _install_owned_account_route(engine, monkeypatch)
    broker_resolution_calls = 0

    def _reject_current_authority(**_kwargs):  # type: ignore[no-untyped-def]
        raise CurrentAuthorityError("Broker account 42 credential was revoked")

    async def _unexpected_broker_resolution(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal broker_resolution_calls
        broker_resolution_calls += 1
        return _FakeBroker()

    monkeypatch.setattr(
        engine._route_resolver,
        "validate_current_authority",
        _reject_current_authority,
    )
    monkeypatch.setattr(engine, "_get_broker", _unexpected_broker_resolution)

    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config={
                "binding_id": 7,
                "execution_mode": "spot",
            },
            signal=_signal(),
        )
    )

    assert result.execution_mode == "blocked"
    assert result.block_reason == "current_authorization_rejected"
    assert broker_resolution_calls == 0


class _FakeOrderBuilder(OrderBuilder):
    def build(self, req, account, market_data):  # type: ignore[no-untyped-def]
        return [
            OrderIntent(
                broker_code="coinbase",
                symbol=req.signal.symbol,
                side="BUY",
                quantity=1.0,
                order_type="market",
                metadata={"signal_id": req.signal.signal_id},
            )
        ]


class _TargetOverrideOrderBuilder(OrderBuilder):
    def __init__(self, *, malformed_reduction: bool = False) -> None:
        super().__init__()
        self._malformed_reduction = malformed_reduction

    def build(self, req, account, market_data):  # type: ignore[no-untyped-def]
        del account, market_data
        override = req.target_position_override
        assert isinstance(override, TargetPositionQuantityOverride)
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
        if reduction:
            metadata["reduce_only"] = not self._malformed_reduction
        return [
            OrderIntent(
                broker_code="coinbase",
                symbol=req.signal.symbol,
                side="SELL" if reduction else "BUY",
                quantity=float(abs(delta)),
                order_type="market",
                metadata=metadata,
            )
        ]


class _FakeBroker:
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
            realized_pnl=0.0,
            positions=[],
            currency="USD",
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


class _FailingAccountBroker(_FakeBroker):
    async def get_account_info(self) -> AccountInfo:
        msg = "broker unavailable"
        raise RuntimeError(msg)


def _target_authority_override(*, reduction: bool) -> TargetPositionQuantityOverride:
    observed_at = datetime.now(tz=UTC)
    return TargetPositionQuantityOverride(
        account_plan_id="a" * 64,
        plan_leg_id="b" * 64,
        symbol="BTC-USD",
        target_allocation=Decimal("0.10"),
        target_quantity=Decimal("1" if reduction else "2"),
        strategy_quantity=Decimal("2" if reduction else "1"),
        broker_quantity=Decimal("2" if reduction else "1"),
        target_weight_drift_fraction=Decimal("0"),
        broker_observed_at=observed_at,
        reference_price=Decimal("100"),
        quote_observed_at=observed_at,
    )


def _run_authority_boundary_flow(
    monkeypatch,
    *,
    order_builder: OrderBuilder,
    signal: Signal,
    target_position_override: TargetPositionQuantityOverride | None,
) -> tuple[ExecutionResult, list[str]]:
    engine = ExecutionEngine(
        order_builder=order_builder,
        default_mode="paper",
        allow_live=False,
    )
    engine._session_factory = object()
    engine._risk_guard_enabled = False
    _install_owned_account_route(engine, monkeypatch)
    monkeypatch.setattr(
        engine._route_resolver,
        "resolve_settlement_currency",
        lambda _signal: "USD",
    )
    broker = _FakeBroker()

    async def _get_broker(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return broker

    monkeypatch.setattr(engine, "_get_broker", _get_broker)
    authority_calls: list[str] = []

    def _current_authority(**_kwargs):  # type: ignore[no-untyped-def]
        authority_calls.append("generic")
        return SimpleNamespace(credential_version="generation-1")

    def _reduction_authority(**_kwargs):  # type: ignore[no-untyped-def]
        authority_calls.append("reduction")
        return SimpleNamespace(credential_version="generation-1")

    monkeypatch.setattr(
        engine._route_resolver,
        "validate_current_authority",
        _current_authority,
    )
    monkeypatch.setattr(
        engine._route_resolver,
        "validate_current_rebalance_reduction_authority",
        _reduction_authority,
    )
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config={"binding_id": 7, "execution_mode": "spot"},
            signal=signal,
            target_position_override=target_position_override,
        )
    )
    return result, authority_calls


def test_exact_target_reduction_uses_dedicated_authority_at_both_boundaries(
    monkeypatch,
) -> None:
    override = _target_authority_override(reduction=True)
    result, calls = _run_authority_boundary_flow(
        monkeypatch,
        order_builder=_TargetOverrideOrderBuilder(),
        signal=replace(
            _signal(),
            action=SignalAction.CLOSE,
            external_signal_id="exact-target-reduction-authority",
        ),
        target_position_override=override,
    )

    assert result.success is True
    assert calls == ["reduction", "reduction"]


def test_malformed_target_reduction_falls_back_at_submission_boundary(monkeypatch) -> None:
    override = _target_authority_override(reduction=True)
    result, calls = _run_authority_boundary_flow(
        monkeypatch,
        order_builder=_TargetOverrideOrderBuilder(malformed_reduction=True),
        signal=replace(
            _signal(),
            action=SignalAction.CLOSE,
            external_signal_id="malformed-target-reduction-authority",
        ),
        target_position_override=override,
    )

    assert result.success is True
    assert calls == ["reduction", "generic"]


def test_positive_target_uses_generic_authority_at_both_boundaries(monkeypatch) -> None:
    override = _target_authority_override(reduction=False)
    result, calls = _run_authority_boundary_flow(
        monkeypatch,
        order_builder=_TargetOverrideOrderBuilder(),
        signal=replace(
            _signal(),
            external_signal_id="positive-target-generic-authority",
        ),
        target_position_override=override,
    )

    assert result.success is True
    assert calls == ["generic", "generic"]


def test_generic_close_uses_generic_authority_at_both_boundaries(monkeypatch) -> None:
    result, calls = _run_authority_boundary_flow(
        monkeypatch,
        order_builder=_FakeOrderBuilder(),
        signal=replace(
            _signal(),
            action=SignalAction.CLOSE,
            external_signal_id="generic-close-authority",
        ),
        target_position_override=None,
    )

    assert result.success is True
    assert calls == ["generic", "generic"]


def test_alert_publisher_posts_webhook_when_enabled() -> None:
    client = _FakeWebhookClient()
    publisher = AlertPublisher(
        enabled=True,
        webhook_url="https://alerts.example.test/hook",
        client=client,
        dispatch_async=False,
    )

    publisher.publish(
        ExecutionAlert(
            event_type="circuit_breaker_open",
            severity="error",
            message="breaker opened",
            payload={"broker": "coinbase"},
        )
    )

    assert client.calls == [
        {
            "url": "https://alerts.example.test/hook",
            "json": {
                "event_type": "circuit_breaker_open",
                "severity": "error",
                "message": "breaker opened",
                "payload": {"broker": "coinbase"},
                "source": "execution_engine",
                "text": (
                    "[ERROR] execution_engine circuit_breaker_open\n"
                    "breaker opened\nbroker: coinbase"
                ),
            },
        }
    ]


def test_engine_logs_error_when_breaker_opens(caplog) -> None:
    engine = ExecutionEngine(order_builder=OrderBuilder())

    with caplog.at_level(logging.ERROR):
        engine.open_circuit_breaker(
            scope="broker",
            breaker_key="broker:coinbase:paper",
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            broker="coinbase",
            environment="paper",
            reason="service unavailable",
        )

    assert "Circuit breaker opened" in caplog.text


def test_engine_loads_valid_sandbox_certification_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        runtime_config=_runtime_with_marker(marker),
    )
    monkeypatch.setenv(
        "EXECUTION_SANDBOX_CERTIFICATION_MARKER",
        str(tmp_path / "changed-after-startup.json"),
    )

    payload = engine._gatekeeper.load_sandbox_certification_marker()

    assert payload["status"] == "passed"


def test_engine_rejects_invalid_sandbox_certification_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    marker.write_text('{"status": "failed"}', encoding="utf-8")
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        runtime_config=_runtime_with_marker(marker),
    )

    try:
        engine._gatekeeper.load_sandbox_certification_marker()
    except ValueError as exc:
        assert "status must be 'passed'" in str(exc)  # noqa: PT017
    else:
        raise AssertionError("Expected invalid marker error")


def test_broker_global_breaker_blocks_all_submissions_for_same_broker_environment(
    monkeypatch,
) -> None:
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

    async def _run():  # type: ignore[no-untyped-def]
        config = {"execution_mode": "spot"}
        result_one = await engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=41,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config=config,
            signal=_signal("BTC-USD"),
        )
        result_two = await engine.handle_signal(
            user_id="user-2",
            profile=_account_profile(
                user_id="user-2",
                account_id=42,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config=config,
            signal=_signal("ETH-USD"),
        )
        return result_one, result_two

    first, second = asyncio.run(_run())

    assert first.execution_mode == "blocked"
    assert second.execution_mode == "blocked"
    assert "Broker-global circuit breaker" in (first.error_message or "")
    assert "Broker-global circuit breaker" in (second.error_message or "")


def test_handle_signal_uses_route_snapshot_paper_environment_even_if_profile_is_live(
    monkeypatch,
) -> None:
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FakeBroker()
    observed: dict[str, str] = {}
    _install_owned_account_route(engine, monkeypatch)

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

    profile = _account_profile(
        user_id="user-1",
        account_id=42,
        broker="coinbase",
        environment="paper",
        live_enabled=False,
    )
    route = profile["_broker_route_snapshot"]
    assert isinstance(route, dict)
    route.update(
        allowed_brokers=["coinbase"],
        route_source="scoring_policy",
        live_enabled=False,
        asset_class="crypto",
        execution_mode="spot",
    )

    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=profile,
            user_strategy_config={
                "broker": "coinbase",
                "execution_mode": "spot",
                "mode": "paper",
            },
            signal=_signal(),
        )
    )

    assert result.success is True
    assert observed["broker_type"] == "coinbase"
    assert observed["environment"] == "paper"
    assert observed["credential_ref"] == "coinbase-account-42"
    assert broker.submit_calls == 1


def test_handle_signal_does_not_treat_signal_source_as_runtime_mode(
    monkeypatch,
    caplog,
) -> None:
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        default_mode="paper",
        allow_live=False,
    )
    broker = _FakeBroker()
    _install_owned_account_route(engine, monkeypatch)

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
    sig = replace(_signal(), source="signal_api")

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            engine.handle_signal(
                user_id="user-1",
                profile=_account_profile(
                    user_id="user-1",
                    account_id=42,
                    broker="coinbase",
                    environment="paper",
                    live_enabled=False,
                ),
                user_strategy_config={"execution_mode": "spot"},
                signal=sig,
            )
        )

    assert result.success is True
    assert "Unknown execution mode 'signal_api'" not in caplog.text


def test_handle_signal_claim_blocks_duplicate_submission(monkeypatch) -> None:
    engine = ExecutionEngine(order_builder=_FakeOrderBuilder())
    broker = _FakeBroker()
    _install_owned_account_route(engine, monkeypatch)

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
    sig = _signal()

    first = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config={"execution_mode": "spot"},
            signal=sig,
        )
    )
    second = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="paper",
                live_enabled=False,
            ),
            user_strategy_config={"execution_mode": "spot"},
            signal=sig,
        )
    )

    assert first.success is True
    assert second.execution_mode == "dedup"
    assert second.broker_account_id == 42
    assert broker.submit_calls == 1


def test_live_global_trading_halt_blocks_before_broker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        allow_live=True,
        runtime_config=_runtime_with_marker(marker),
    )
    broker = _FakeBroker()
    engine.set_trading_halt(enabled=True, reason="operator stop", updated_by="tester")

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
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile={
                "broker": "coinbase",
                "broker_account_id": 42,
                "sandbox": False,
                "live_enabled": True,
            },
            user_strategy_config={"execution_mode": "spot", "mode": "live", "live_enabled": True},
            signal=_live_signal(),
        )
    )

    assert result.execution_mode == "blocked"
    assert "global trading halt" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_live_account_state_failure_blocks_before_order_build(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        allow_live=True,
        runtime_config=_runtime_with_marker(marker),
    )
    engine.mark_reconciliation_health(healthy=True)
    broker = _FailingAccountBroker()
    _install_owned_account_route(engine, monkeypatch)

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
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="live",
                live_enabled=True,
            ),
            user_strategy_config={"execution_mode": "spot", "mode": "live", "live_enabled": True},
            signal=_live_signal(),
        )
    )

    assert result.execution_mode == "blocked"
    assert "account state unavailable" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_live_stale_market_data_blocks_before_submission(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        allow_live=True,
        runtime_config=_runtime_with_marker(marker),
    )
    engine.mark_reconciliation_health(healthy=True)
    broker = _FakeBroker()
    _install_owned_account_route(engine, monkeypatch)

    async def _fake_get_broker(
        broker_type,
        environment,
        credential_ref,
        credentials=None,  # type: ignore[no-untyped-def]
        user_id=None,
        **_kwargs,
    ):
        return broker

    async def _stale_market_data(
        symbol: str,
        *,
        user_id: str,
        environment: str,
    ):
        del symbol, user_id, environment
        return {
            "price": 100.0,
            "timestamp": datetime.now(tz=UTC) - timedelta(seconds=120),
        }

    monkeypatch.setattr(engine, "_get_broker", _fake_get_broker)
    monkeypatch.setattr(engine, "_get_market_data", _stale_market_data)
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile=_account_profile(
                user_id="user-1",
                account_id=42,
                broker="coinbase",
                environment="live",
                live_enabled=True,
            ),
            user_strategy_config={"execution_mode": "spot", "mode": "live", "live_enabled": True},
            signal=_live_signal(),
        )
    )

    assert result.execution_mode == "blocked"
    assert "market data age" in (result.error_message or "")
    assert broker.submit_calls == 0


def test_get_broker_uses_resolver_owned_paper_account_for_alternate_broker(
    monkeypatch,
) -> None:
    def reject_bridge(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Alternate paper routing must not construct BrokerBridge")

    monkeypatch.setattr("execution_engine.broker_resolver.BrokerBridge", reject_bridge)
    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        runtime_config=ExecutionRuntimeConfig(
            paper=ExecutionPaperConfig(
                use_local_broker=True,
                slippage_pct=0.002,
                commission_pct=0.003,
            )
        ),
    )
    monkeypatch.setenv("EXECUTION_USE_LOCAL_PAPER_BROKER", "false")
    monkeypatch.setenv("PAPER_BROKER_SLIPPAGE_PCT", "0.5")
    monkeypatch.setenv("PAPER_BROKER_COMMISSION_PCT", "0.5")

    broker = asyncio.run(
        engine._get_broker(
            BrokerType.DELTA,
            "paper",
            "delta-paper",
            user_id="user-1",
            broker_account_id=42,
            account_currency="EUR",
            settlement_currency="USD",
            paper_initial_equity=50_000.0,
            paper_initial_cash=50_000.0,
        )
    )

    assert isinstance(broker, PaperBroker)
    account = asyncio.run(broker.get_account_info())
    assert account.account_id == "42"
    assert account.currency == "EUR"
    assert account.equity == 50_000.0
    assert broker._slippage_pct == 0.002
    assert broker._commission_pct == 0.003


# ── H1: circuit-breaker blocks must be visible at the block site ──
# ── H2: reduce-only (CLOSE/flatten) bypasses the strategy breaker ──


def _open_strategy_breaker(engine: ExecutionEngine, strategy_breaker_key: str) -> None:
    engine.open_circuit_breaker(
        scope="strategy",
        breaker_key=strategy_breaker_key,
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker="paper",
        environment="paper",
        reason="reconciliation drift",
    )


def test_check_circuit_breakers_logs_block_at_block_site(caplog) -> None:  # type: ignore[no-untyped-def]
    """H1: an open breaker logs a WARNING at the block site (not just a silent DB
    risk-breach row), with the trace context and a block_reason."""
    engine = ExecutionEngine(order_builder=OrderBuilder())
    strategy_breaker_key = engine._breaker_manager.strategy_key(
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker_code="paper",
        credential_ref="paper-paper",
    )
    broker_breaker_key = engine._breaker_manager.broker_key(
        broker_code="paper",
        environment="paper",
    )
    _open_strategy_breaker(engine, strategy_breaker_key)

    with caplog.at_level(logging.WARNING):
        msg = engine._breaker_manager.check_execution(
            user_id="user-1",
            strategy_id="test_strategy_alpha_v1",
            broker_type=BrokerType.PAPER,
            environment="paper",
            credential_ref="paper-paper",
            strategy_breaker_key=strategy_breaker_key,
            broker_breaker_key=broker_breaker_key,
            reduce_only=False,
            trace_ctx={
                "run_id": "r-1",
                "signal_id": "s-1",
                "strategy_id": "test_strategy_alpha_v1",
                "symbol": "ETHUSD",
                "user_id": "user-1",
            },
        )

    assert msg == "Circuit breaker is open for this user/strategy/broker"
    block_records = [
        r for r in caplog.records if r.getMessage() == "Execution blocked: circuit breaker open"
    ]
    assert block_records, "breaker block must emit a stdout WARNING at the block site"
    assert getattr(block_records[0], "block_reason", None) == "strategy_breaker_open"
    assert getattr(block_records[0], "breaker_key", None) == strategy_breaker_key
    # trace context rides along so the outage is attributable per signal/tenant.
    assert getattr(block_records[0], "signal_id", None) == "s-1"


def test_reduce_only_close_bypasses_open_strategy_breaker(caplog) -> None:  # type: ignore[no-untyped-def]
    """H2: a reduce-only (CLOSE) order flattens even while the strategy breaker is
    open — a breaker must never trap an open position (the soak's stuck-3 case)."""
    engine = ExecutionEngine(order_builder=OrderBuilder())
    strategy_breaker_key = engine._breaker_manager.strategy_key(
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker_code="paper",
        credential_ref="paper-paper",
    )
    broker_breaker_key = engine._breaker_manager.broker_key(
        broker_code="paper",
        environment="paper",
    )
    _open_strategy_breaker(engine, strategy_breaker_key)

    with caplog.at_level(logging.WARNING):
        msg = engine._breaker_manager.check_execution(
            user_id="user-1",
            strategy_id="test_strategy_alpha_v1",
            broker_type=BrokerType.PAPER,
            environment="paper",
            credential_ref="paper-paper",
            strategy_breaker_key=strategy_breaker_key,
            broker_breaker_key=broker_breaker_key,
            reduce_only=True,
        )

    assert msg is None
    assert "allowing reduce-only order to flatten" in caplog.text


def test_reduce_only_does_not_bypass_broker_global_breaker() -> None:
    """The broker-global breaker stays a hard stop even for reduce-only: a failing
    broker connection cannot fill anything, flatten or not."""
    engine = ExecutionEngine(order_builder=OrderBuilder())
    strategy_breaker_key = engine._breaker_manager.strategy_key(
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker_code="paper",
        credential_ref="paper-paper",
    )
    broker_breaker_key = engine._breaker_manager.broker_key(
        broker_code="paper",
        environment="paper",
    )
    engine.open_circuit_breaker(
        scope="broker",
        breaker_key=broker_breaker_key,
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker="paper",
        environment="paper",
        reason="service unavailable",
    )

    msg = engine._breaker_manager.check_execution(
        user_id="user-1",
        strategy_id="test_strategy_alpha_v1",
        broker_type=BrokerType.PAPER,
        environment="paper",
        credential_ref="paper-paper",
        strategy_breaker_key=strategy_breaker_key,
        broker_breaker_key=broker_breaker_key,
        reduce_only=True,
    )

    assert msg == "Broker-global circuit breaker is open for this broker/environment"


def test_live_route_blocked_when_engine_mode_is_paper(monkeypatch, tmp_path: Path) -> None:
    """D1: a live-environment broker route arriving while the engine's own mode
    is paper must be BLOCKED fail-closed — never submitted live, never silently
    downgraded to paper — even with EXECUTION_ENGINE_ALLOW_LIVE=true and the
    user opted into live."""
    marker = tmp_path / "coinbase_sandbox_certified.json"
    _write_valid_marker(marker)
    engine = ExecutionEngine(
        order_builder=_FakeOrderBuilder(),
        default_mode="paper",
        allow_live=True,
        runtime_config=_runtime_with_marker(marker),
    )
    broker = _FakeBroker()

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
    result = asyncio.run(
        engine.handle_signal(
            user_id="user-1",
            profile={
                "broker": "coinbase",
                "broker_account_id": 42,
                "sandbox": False,
                "live_enabled": True,
                "_broker_route_snapshot": {
                    "broker": "coinbase",
                    "broker_environment": "live",
                    "broker_account_id": 42,
                    "sandbox": False,
                },
            },
            # No "mode" key: resolves to the engine default (EXECUTION_MODE=paper).
            user_strategy_config={"execution_mode": "spot", "live_enabled": True},
            signal=_live_signal(),
        )
    )

    assert result.success is False
    assert result.execution_mode == "blocked"
    message = result.error_message or ""
    # The block message must name BOTH modes (live route vs paper engine mode).
    assert "LIVE environment" in message
    assert "'paper'" in message
    assert broker.submit_calls == 0


def test_per_user_live_gate_keys_on_environment_not_mode() -> None:
    """D1b: the per-user live_enabled gate fires on a LIVE environment (so a
    live route can never skip it), and is not applicable to paper routes."""
    engine = ExecutionEngine(order_builder=OrderBuilder(), allow_live=True)

    error = engine._gatekeeper.check_live_mode(
        environment="live",
        mode="live",
        profile={},
        user_strategy_config={},
        trace_ctx={},
    )
    assert error is not None
    assert "live_enabled" in error

    # A paper-environment route never trips the live-only per-user gate, even
    # when a config claims mode=live.
    assert (
        engine._gatekeeper.check_live_mode(
            environment="paper",
            mode="live",
            profile={},
            user_strategy_config={},
            trace_ctx={},
        )
        is None
    )


def test_live_route_mode_mismatch_blocks_even_with_user_live_enabled() -> None:
    """D1: mode/environment mismatch is checked directly at the gate helper."""
    engine = ExecutionEngine(order_builder=OrderBuilder(), allow_live=True)

    error = engine._gatekeeper.check_live_mode(
        environment="live",
        mode="paper",
        profile={"live_enabled": True},
        user_strategy_config={"live_enabled": True},
        trace_ctx={},
    )

    assert error is not None
    assert "LIVE environment" in error
    assert "'paper'" in error


def test_allow_live_with_paper_default_mode_warns_at_startup(caplog) -> None:
    """D1: ALLOW_LIVE=true with a paper engine mode is a config footgun and must
    be surfaced loudly at engine construction."""
    with caplog.at_level(logging.WARNING):
        ExecutionEngine(order_builder=OrderBuilder(), allow_live=True, default_mode="paper")
    assert "EXECUTION_ENGINE_ALLOW_LIVE=true" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        ExecutionEngine(order_builder=OrderBuilder(), allow_live=True, default_mode="live")
    assert "EXECUTION_ENGINE_ALLOW_LIVE=true" not in caplog.text
