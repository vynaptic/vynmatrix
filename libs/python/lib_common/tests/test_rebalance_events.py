"""Tests for deterministic model-rebalance and account-plan event contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    CanonicalSignalSnapshot,
    ExecutionPolicySnapshot,
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    RebalanceExecutionCommandEvent,
    RebalanceExecutionLegSnapshot,
    compute_account_plan_id,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
    compute_rebalance_execution_content_sha256,
)

_CUTOFF = datetime(2025, 12, 31, 21, 0, tzinfo=UTC)
_EXECUTE_AT = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
_SESSION = date(2025, 12, 31)
_CONFIG_HASH = "a" * 64
_INPUT_HASH = "b" * 64
_RANK_HASH = "c" * 64
_AUTHORITY_HASH = "d" * 64
_SESSION_HASH = "e" * 64
_DATA_USE_SCOPE = "historical_validation"


def _signal(*, symbol: str, action: str, signal_id: str) -> CanonicalSignalSnapshot:
    return CanonicalSignalSnapshot(
        signal_id=signal_id,
        strategy_id="us_quality_compounder_v1",
        strategy_type="indicator",
        symbol=symbol,
        action=action,
        confidence=0.8,
        timestamp=_CUTOFF,
        size_hint=0.04,
        external_signal_id=f"external:{signal_id}",
    )


def _model_legs() -> list[ModelRebalanceLegSnapshot]:
    return [
        ModelRebalanceLegSnapshot(
            leg_id="leg:exit:IBM",
            sequence=0,
            phase="exit",
            signal=_signal(symbol="IBM", action="close", signal_id="signal:ibm"),
            allocation_hint=0,
            factor_snapshot_id="1" * 64,
            rank_snapshot_id=_RANK_HASH,
            current_rank_row_present=True,
            current_rank_factor_snapshot_id="1" * 64,
        ),
        ModelRebalanceLegSnapshot(
            leg_id="leg:entry:MSFT",
            sequence=1,
            phase="entry",
            signal=_signal(symbol="MSFT", action="long", signal_id="signal:msft"),
            rank=1,
            allocation_hint=0.04,
            factor_snapshot_id="2" * 64,
            rank_snapshot_id=_RANK_HASH,
            current_rank_row_present=True,
            current_rank_factor_snapshot_id="2" * 64,
        ),
    ]


def _model_event() -> ModelRebalanceSubmissionEvent:
    legs = _model_legs()
    content_hash = compute_model_rebalance_content_sha256(
        strategy_id="us_quality_compounder_v1",
        strategy_version="1.0.0",
        effective_session=_SESSION,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=_SESSION_HASH,
        data_use_scope=_DATA_USE_SCOPE,
        provider_authority_sha256=_AUTHORITY_HASH,
        configuration_sha256=_CONFIG_HASH,
        input_snapshot_sha256=_INPUT_HASH,
        rank_snapshot_sha256=_RANK_HASH,
        intentional_cash_slots=0,
        legs=legs,
    )
    return ModelRebalanceSubmissionEvent(
        rebalance_id=compute_model_rebalance_id(
            strategy_id="us_quality_compounder_v1",
            strategy_version="1.0.0",
            effective_session=_SESSION,
            execute_not_before=_EXECUTE_AT,
            execution_session_sha256=_SESSION_HASH,
            data_use_scope=_DATA_USE_SCOPE,
            provider_authority_sha256=_AUTHORITY_HASH,
            configuration_sha256=_CONFIG_HASH,
            input_snapshot_sha256=_INPUT_HASH,
            content_sha256=content_hash,
        ),
        strategy_id="us_quality_compounder_v1",
        strategy_version="1.0.0",
        effective_session=_SESSION,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=_SESSION_HASH,
        data_use_scope=_DATA_USE_SCOPE,
        provider_authority_sha256=_AUTHORITY_HASH,
        configuration_sha256=_CONFIG_HASH,
        input_snapshot_sha256=_INPUT_HASH,
        rank_snapshot_sha256=_RANK_HASH,
        content_sha256=content_hash,
        expected_leg_count=2,
        intentional_cash_slots=0,
        legs=legs,
    )


def test_model_rebalance_round_trip_preserves_deterministic_identity() -> None:
    event = _model_event()
    restored = ModelRebalanceSubmissionEvent.model_validate_json(event.model_dump_json())

    assert restored.rebalance_id == event.rebalance_id
    assert restored.content_sha256 == event.content_sha256
    assert [leg.phase for leg in restored.legs] == ["exit", "entry"]


def test_model_rebalance_rejects_partial_or_reordered_content() -> None:
    payload = _model_event().model_dump(mode="json")
    payload["expected_leg_count"] = 1
    with pytest.raises(ValidationError, match="expected_leg_count"):
        ModelRebalanceSubmissionEvent.model_validate(payload)


def test_model_leg_allows_provenanced_universe_exit_without_current_rank_row() -> None:
    leg = ModelRebalanceLegSnapshot(
        leg_id="leg:exit:former-member",
        sequence=0,
        phase="exit",
        signal=_signal(symbol="FORMER", action="close", signal_id="signal:former"),
        allocation_hint=0,
        factor_snapshot_id="3" * 64,
        rank_snapshot_id=_RANK_HASH,
        current_rank_row_present=False,
        current_rank_factor_snapshot_id=None,
        membership_change_reason="left_effective_universe",
    )

    assert leg.membership_change_reason == "left_effective_universe"

    with pytest.raises(ValidationError, match="missing current rank row is valid only"):
        ModelRebalanceLegSnapshot.model_validate(
            {
                **leg.model_dump(mode="json"),
                "phase": "entry",
                "signal": _signal(
                    symbol="FORMER",
                    action="long",
                    signal_id="signal:former-entry",
                ).model_dump(mode="json"),
            }
        )

    payload = _model_event().model_dump(mode="json")
    payload["legs"] = list(reversed(payload["legs"]))
    with pytest.raises(ValidationError, match="contiguous and ordered"):
        ModelRebalanceSubmissionEvent.model_validate(payload)

    payload = _model_event().model_dump(mode="json")
    payload["data_use_scope"] = "paper_forward"
    with pytest.raises(ValidationError, match="content_sha256"):
        ModelRebalanceSubmissionEvent.model_validate(payload)


def test_account_plan_rejects_cross_tenant_policy_and_forward_dependency() -> None:
    model_event = _model_event()
    expiry = _EXECUTE_AT + timedelta(days=3)
    legs = [
        RebalanceExecutionLegSnapshot(
            plan_leg_id="plan-leg:exit:IBM",
            sequence=0,
            phase="exit",
            model_leg_id=model_event.legs[0].leg_id,
            model_signal_snapshot_sha256=canonical_json_hash(
                model_event.legs[0].signal.model_dump(mode="json")
            ),
            external_signal_id=model_event.legs[0].signal.external_signal_id,
            symbol="IBM",
            allocation_hint=0,
        ),
        RebalanceExecutionLegSnapshot(
            plan_leg_id="plan-leg:entry:MSFT",
            sequence=1,
            phase="entry",
            model_leg_id=model_event.legs[1].leg_id,
            model_signal_snapshot_sha256=canonical_json_hash(
                model_event.legs[1].signal.model_dump(mode="json")
            ),
            external_signal_id=model_event.legs[1].signal.external_signal_id,
            symbol="MSFT",
            rank=1,
            allocation_hint=0.04,
            depends_on_sequences=[0],
        ),
    ]
    policy = ExecutionPolicySnapshot(
        user_id="tenant-a",
        strategy_id=model_event.strategy_id,
        binding_id=7,
    )
    route = BrokerRouteSnapshot(
        broker="local-paper",
        broker_account_id=11,
        broker_environment="paper",
    )
    plan_id = compute_account_plan_id(
        model_rebalance_id=model_event.rebalance_id,
        user_id="tenant-a",
        binding_id=7,
        broker_account_id=11,
        data_use_scope=_DATA_USE_SCOPE,
        provider_authority_sha256=_AUTHORITY_HASH,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=_SESSION_HASH,
    )
    content_hash = compute_rebalance_execution_content_sha256(
        account_plan_id=plan_id,
        model_rebalance_id=model_event.rebalance_id,
        user_id="tenant-a",
        strategy_id=model_event.strategy_id,
        binding_id=7,
        broker_account_id=11,
        data_use_scope=_DATA_USE_SCOPE,
        provider_authority_sha256=_AUTHORITY_HASH,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=_SESSION_HASH,
        execution_policy=policy,
        broker_route=route,
        expires_at=expiry,
        intentional_cash_slots=0,
        legs=legs,
    )
    command = RebalanceExecutionCommandEvent(
        account_plan_id=plan_id,
        model_rebalance_id=model_event.rebalance_id,
        user_id="tenant-a",
        strategy_id=model_event.strategy_id,
        binding_id=7,
        data_use_scope=_DATA_USE_SCOPE,
        provider_authority_sha256=_AUTHORITY_HASH,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=_SESSION_HASH,
        execution_policy=policy,
        broker_route=route,
        expires_at=expiry,
        content_sha256=content_hash,
        expected_leg_count=2,
        intentional_cash_slots=0,
        legs=legs,
    )
    assert command.broker_route.live_enabled is False

    payload = command.model_dump(mode="json")
    payload["execution_policy"]["user_id"] = "tenant-b"
    with pytest.raises(ValidationError, match="does not own"):
        RebalanceExecutionCommandEvent.model_validate(payload)

    payload = command.model_dump(mode="json")
    payload["legs"][0]["depends_on_sequences"] = [1]
    with pytest.raises(ValidationError, match="dependencies"):
        RebalanceExecutionCommandEvent.model_validate(payload)

    payload = command.model_dump(mode="json")
    payload["execution_policy"]["autopilot"] = False
    with pytest.raises(ValidationError, match="content_sha256"):
        RebalanceExecutionCommandEvent.model_validate(payload)

    payload = command.model_dump(mode="json")
    payload["broker_route"]["broker"] = "different-paper-route"
    with pytest.raises(ValidationError, match="content_sha256"):
        RebalanceExecutionCommandEvent.model_validate(payload)


def test_rebalance_events_reject_naive_or_noncausal_execution_timing() -> None:
    payload = _model_event().model_dump(mode="json")
    payload["execute_not_before"] = payload["decision_cutoff"]
    with pytest.raises(ValidationError, match="later than decision_cutoff"):
        ModelRebalanceSubmissionEvent.model_validate(payload)

    payload = _model_event().model_dump(mode="python")
    payload["decision_cutoff"] = _CUTOFF.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        ModelRebalanceSubmissionEvent.model_validate(payload)
