from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
    ScoreContextSnapshot,
)


def test_execution_command_event_round_trip() -> None:
    event = ExecutionCommandEvent(
        run_id="run-1",
        correlation_id="sig-1",
        producer="scoring_engine",
        user_id="user-1",
        signal=CanonicalSignalSnapshot(
            signal_id="sig-1",
            strategy_id="swing_high_low_pmo_v1",
            strategy_type="indicator",
            symbol="BTCUSD",
            action="long",
            confidence=0.91,
            timestamp=datetime(2026, 3, 7, tzinfo=UTC),
            expires_at=datetime(2026, 3, 7, tzinfo=UTC) + timedelta(hours=1),
            asset_class="crypto",
            run_id="run-1",
            external_signal_id="ext-sig-1",
        ),
        score_context=ScoreContextSnapshot(
            asset_score=0.82,
            sector_score=0.8,
            market_score=0.74,
            recommended_mode="spot",
            score_target="BTCUSD",
        ),
        execution_policy=ExecutionPolicySnapshot(
            user_id="user-1",
            strategy_id="swing_high_low_pmo_v1",
            binding_id=17,
            autopilot=True,
            execution_mode="spot",
            execution_modes_allowed=["spot", "paper"],
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
        profile={"broker": "paper"},
        user_strategy_config={"execution_mode": "spot"},
    )

    payload = event.model_dump(mode="json")
    restored = ExecutionCommandEvent.model_validate(payload)

    assert restored.event_id == event.event_id
    assert restored.signal.signal_id == "sig-1"
    assert restored.signal.expires_at == datetime(2026, 3, 7, 1, tzinfo=UTC)
    assert restored.execution_policy.binding_id == 17
    assert restored.broker_route.broker == "paper"


def test_execution_command_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionCommandEvent.model_validate(
            {
                "user_id": "user-1",
                "signal": {
                    "signal_id": "sig-1",
                    "strategy_id": "swing_high_low_pmo_v1",
                    "strategy_type": "indicator",
                    "symbol": "BTCUSD",
                    "action": "long",
                    "confidence": 0.8,
                    "timestamp": "2026-03-07T00:00:00Z",
                },
                "score_context": {
                    "asset_score": 0.7,
                },
                "execution_policy": {
                    "user_id": "user-1",
                    "strategy_id": "swing_high_low_pmo_v1",
                },
                "broker_route": {
                    "route_source": "policy",
                    "live_enabled": False,
                    "sandbox": True,
                },
                "topic": "execution.commands",
                "event_type": "ExecutionCommand",
                "unexpected": True,
            }
        )


def test_event_contracts_canonicalize_asset_class_aliases() -> None:
    signal = CanonicalSignalSnapshot(
        signal_id="sig-taxonomy",
        strategy_id="taxonomy_contract",
        strategy_type="indicator",
        symbol="EUR/USD",
        action="hold",
        confidence=1.0,
        timestamp=datetime(2026, 3, 7, tzinfo=UTC),
        asset_class="forex",
        external_signal_id="external-taxonomy",
    )
    route = BrokerRouteSnapshot(
        broker="ibkr",
        broker_account_id=1,
        broker_environment="paper",
        asset_class="commodity",
    )

    assert signal.asset_class == "fx"
    assert route.asset_class == "commodities"


def test_event_contracts_reject_unknown_asset_classes() -> None:
    with pytest.raises(ValidationError, match="Unsupported asset_class"):
        BrokerRouteSnapshot(
            broker="paper",
            broker_account_id=1,
            broker_environment="paper",
            asset_class="stock",
        )
