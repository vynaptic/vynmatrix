from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from execution_engine.api import create_app
from execution_engine.engine import ExecutionResult
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
    ScoreContextSnapshot,
)
from lib_strategy.signals.signal import SignalAction


class StubEngine:
    def __init__(self) -> None:
        self.received: dict[str, Any] = {}

    async def handle_signal(
        self,
        *,
        user_id: str,
        profile: dict[str, Any],
        user_strategy_config: dict[str, Any],
        signal: Any,
        credentials: dict[str, str] | None = None,
        score_context: dict[str, float] | None = None,
    ) -> ExecutionResult:
        self.received = {
            "user_id": user_id,
            "profile": profile,
            "user_strategy_config": user_strategy_config,
            "signal": signal,
            "credentials": credentials,
            "score_context": score_context,
        }
        return ExecutionResult(
            success=True,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            execution_mode="paper",
            broker="paper",
            orders_submitted=1,
            orders_filled=1,
            total_quantity=0.1,
            average_price=45000.0,
            total_commission=0.0,
        )

    def trading_halt_state(self):  # type: ignore[no-untyped-def]
        class _State:
            def to_dict(self) -> dict[str, Any]:
                return {"enabled": False}

        return _State()

    def set_trading_halt(  # type: ignore[no-untyped-def]
        self,
        *,
        enabled: bool,
        reason: str | None,
        updated_by: str | None,
        metadata: dict[str, Any] | None = None,
    ):
        del enabled, reason, updated_by, metadata
        return self.trading_halt_state()


def test_execute_command_endpoint_accepts_versioned_event() -> None:
    engine = StubEngine()
    app = create_app(engine)
    client = TestClient(app)

    command = ExecutionCommandEvent(
        run_id="run-exec-1",
        correlation_id="sig-exec-1",
        producer="scoring_engine",
        user_id="user-42",
        signal=CanonicalSignalSnapshot(
            signal_id="sig-exec-1",
            strategy_id="swing_high_low_pmo_v1",
            strategy_type="indicator",
            symbol="BTCUSD",
            action="long",
            confidence=0.95,
            timestamp=datetime(2026, 3, 7, tzinfo=UTC),
            asset_class="crypto",
            run_id="run-exec-1",
            external_signal_id="ext-exec-1",
        ),
        score_context=ScoreContextSnapshot(
            asset_score=0.88,
            sector_score=0.8,
            market_score=0.76,
            recommended_mode="spot",
            score_target="BTCUSD",
        ),
        execution_policy=ExecutionPolicySnapshot(
            user_id="user-42",
            strategy_id="swing_high_low_pmo_v1",
            binding_id=9,
            autopilot=True,
            execution_mode="spot",
            execution_modes_allowed=["spot"],
            allowed_brokers=["paper"],
            sizing={"position_size_pct": 0.02},
            risk_caps={"max_notional": 1000},
        ),
        broker_route=BrokerRouteSnapshot(
            broker="paper",
            broker_account_id=1,
            broker_environment="paper",
            allowed_brokers=["paper"],
            route_source="scoring_policy",
            live_enabled=False,
            sandbox=True,
            asset_class="crypto",
            execution_mode="spot",
        ),
        profile={"broker": "paper", "sandbox": True},
        user_strategy_config={"execution_mode": "spot"},
    )

    response = client.post(
        "/execute-command",
        json=command.model_dump(mode="json"),
        headers={"X-Run-ID": "run-exec-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["event_id"] == command.event_id
    assert engine.received["user_id"] == "user-42"
    assert engine.received["score_context"]["recommended_mode"] == "spot"
    assert engine.received["profile"]["_broker_route_snapshot"]["broker_environment"] == "paper"
    assert engine.received["user_strategy_config"]["_execution_policy_snapshot"]["binding_id"] == 9
    assert engine.received["signal"].run_id == "run-exec-1"
    assert engine.received["signal"].action == SignalAction.LONG


class _ResultEngine:
    """Engine stub that returns a fixed ExecutionResult (C-2 status routing)."""

    def __init__(self, *, success: bool, execution_mode: str) -> None:
        self._success = success
        self._execution_mode = execution_mode

    async def handle_signal(self, *, signal: Any, **_: Any) -> ExecutionResult:
        return ExecutionResult(
            success=self._success,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            execution_mode=self._execution_mode,
            broker="coinbase",
            orders_submitted=1,
            orders_filled=1 if self._success else 0,
            total_quantity=0.0,
            average_price=0.0,
            total_commission=0.0,
            error_message=None if self._success else "transient",
        )


def _build_command() -> ExecutionCommandEvent:
    return ExecutionCommandEvent(
        run_id="run-exec-2",
        correlation_id="sig-exec-2",
        producer="scoring_engine",
        user_id="user-42",
        signal=CanonicalSignalSnapshot(
            signal_id="sig-exec-2",
            strategy_id="swing_high_low_pmo_v1",
            strategy_type="indicator",
            symbol="BTCUSD",
            action="long",
            confidence=0.95,
            timestamp=datetime(2026, 3, 7, tzinfo=UTC),
            asset_class="crypto",
            run_id="run-exec-2",
            external_signal_id="ext-exec-2",
        ),
        score_context=ScoreContextSnapshot(
            asset_score=0.88, sector_score=0.8, market_score=0.76, score_target="BTCUSD"
        ),
        execution_policy=ExecutionPolicySnapshot(
            user_id="user-42",
            strategy_id="swing_high_low_pmo_v1",
            binding_id=9,
            autopilot=True,
            execution_mode="spot",
            execution_modes_allowed=["spot"],
            allowed_brokers=["coinbase"],
        ),
        broker_route=BrokerRouteSnapshot(
            broker="coinbase",
            broker_account_id=1,
            broker_environment="paper",
            allowed_brokers=["coinbase"],
            route_source="scoring_policy",
            live_enabled=False,
            sandbox=True,
            asset_class="crypto",
            execution_mode="spot",
        ),
        profile={"broker": "coinbase", "sandbox": True},
        user_strategy_config={"execution_mode": "spot"},
    )


def test_execute_command_transient_failure_returns_502_for_retry() -> None:
    # A non-success result in a non-terminal mode is a transient infra failure:
    # the endpoint must answer 5xx so the outbox relay retries (not silently drop).
    app = create_app(_ResultEngine(success=False, execution_mode="spot"))
    client = TestClient(app)
    response = client.post("/execute-command", json=_build_command().model_dump(mode="json"))
    assert response.status_code == 502
    assert response.json()["status"] == "retry"


def test_execute_command_terminal_block_returns_200() -> None:
    # A deliberate policy block is terminal: 200 (no retry).
    app = create_app(_ResultEngine(success=False, execution_mode="blocked"))
    client = TestClient(app)
    response = client.post("/execute-command", json=_build_command().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_admin_trading_halt_requires_admin_key_in_production(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY", "service-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    engine = StubEngine()
    app = create_app(engine)
    client = TestClient(app)

    missing_admin = client.get("/admin/trading-halt", headers={"X-API-Key": "service-key"})
    valid_admin = client.get(
        "/admin/trading-halt",
        headers={"X-API-Key": "service-key", "X-Admin-API-Key": "admin-key"},
    )

    assert missing_admin.status_code == 403
    assert valid_admin.status_code == 200


def test_production_auth_fails_closed_when_service_key_missing(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("API_KEY", raising=False)
    engine = StubEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.get("/admin/trading-halt")

    assert response.status_code == 503
