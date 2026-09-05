"""Deterministic scoring/risk replay from immutable raw-signal evidence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from dev_cli.validation.backtest.engine import RawSignalEvidence
from dev_cli.validation.scoring_replay import (
    ScoringReplayBindingState,
    replay_scoring_raw_signal_ledger,
)
from execution_engine.brokers.base import BrokerCapabilities
from execution_engine.config import BrokerType, ExecutionMode
from execution_engine.risk_guard import RiskGuard
from lib_strategy.signals.adapters.scoring import (
    build_scoring_view,
    evaluate_entry_signal_requirements,
)
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id

_GENERATED_AT = datetime(2025, 1, 2, 20, 0, tzinfo=UTC)
_VALID_UNTIL = _GENERATED_AT + timedelta(days=1)
_STRATEGY_ID = "registered_campaign_v1"


def _signal(
    action: SignalAction,
    *,
    stop_loss: float | None,
    expected_return: float | None = None,
    predicted_risk: float | None = None,
) -> Signal:
    external_signal_id = compute_external_signal_id(
        strategy_id=_STRATEGY_ID,
        symbol="BTCUSDC",
        action=action,
        bar_close_ts=_GENERATED_AT,
    )
    return Signal(
        signal_id="runtime-id-must-not-survive-reconstruction",
        strategy_id=_STRATEGY_ID,
        strategy_type="indicator",
        symbol="BTCUSDC",
        action=action,
        confidence=0.7,
        timestamp=_GENERATED_AT,
        entry_price=100.0,
        stop_loss=stop_loss,
        horizon="1D",
        expected_return=expected_return,
        predicted_risk=predicted_risk,
        external_signal_id=external_signal_id,
        expires_at=_VALID_UNTIL,
    )


def _replay(
    *evidence: RawSignalEvidence,
    active: bool = False,
    autopilot: bool = False,
) -> dict[str, dict[str, object]]:
    binding = ScoringReplayBindingState(
        binding_id="binding-campaign",
        active=active,
        autopilot=autopilot,
        execution_mode="paper",
    )
    return replay_scoring_raw_signal_ledger(
        evidence,
        require_stop_loss=True,
        require_explicit_scoring_inputs=True,
        binding=binding,
    )


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def test_raw_signal_evidence_strict_dict_round_trip() -> None:
    evidence = RawSignalEvidence.from_signal(
        _signal(
            SignalAction.LONG,
            stop_loss=94.0,
            expected_return=0.04,
            predicted_risk=0.02,
        )
    )

    restored = RawSignalEvidence.from_dict(evidence.to_dict())

    assert restored == evidence
    assert restored.to_dict() == evidence.to_dict()
    assert restored.to_signal().external_signal_id == evidence.external_signal_id


@pytest.mark.parametrize("field", ["canonical_json", "external_signal_id"])
def test_raw_signal_evidence_rejects_missing_exact_schema_field(field: str) -> None:
    row = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0)).to_dict()
    del row[field]

    with pytest.raises(ValueError, match="fields do not match the canonical schema"):
        RawSignalEvidence.from_dict(row)


def test_raw_signal_evidence_rejects_unknown_exact_schema_field() -> None:
    row = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0)).to_dict()
    row["runtime_metadata"] = {"must_not": "survive"}

    with pytest.raises(ValueError, match=r"unexpected=.*runtime_metadata"):
        RawSignalEvidence.from_dict(row)


def test_raw_signal_evidence_rejects_noncanonical_embedded_json() -> None:
    row = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0)).to_dict()
    row["canonical_json"] = f"{row['canonical_json']} "

    with pytest.raises(ValueError, match="canonical_json is not canonical"):
        RawSignalEvidence.from_dict(row)


def test_raw_signal_evidence_rejects_outer_canonical_mismatch() -> None:
    row = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0)).to_dict()
    row["entry_price"] = 101.0

    with pytest.raises(ValueError, match="outer fields do not match canonical_json"):
        RawSignalEvidence.from_dict(row)


@pytest.mark.parametrize("timestamp_field", ["generated_at", "valid_until"])
def test_raw_signal_evidence_rejects_naive_persisted_timestamps(timestamp_field: str) -> None:
    row = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0)).to_dict()
    canonical_payload = json.loads(str(row["canonical_json"]))
    canonical_payload[timestamp_field] = "2025-01-02T20:00:00"
    row[timestamp_field] = canonical_payload[timestamp_field]
    row["canonical_json"] = _canonical_json(canonical_payload)

    with pytest.raises(ValueError, match=f"{timestamp_field} must be timezone-aware"):
        RawSignalEvidence.from_dict(row)


def test_stop_only_entry_is_heuristic_and_blocked() -> None:
    signal = _signal(SignalAction.LONG, stop_loss=94.0)
    evidence = RawSignalEvidence.from_signal(signal)

    replay = _replay(evidence, active=True, autopilot=False)
    row = replay[evidence.external_signal_id]

    assert row["scoring_input_source"] == "stop_risk_reward_heuristic"
    assert row["explicit_values"] == {
        "expected_return": None,
        "predicted_risk": None,
        "horizon": "1D",
        "horizon_days": None,
    }
    assert row["entry_requirements"] == {
        "applicable": True,
        "allowed": False,
        "disposition": "blocked",
        "blocked_reason": "explicit_scoring_inputs_required",
        "message": (
            "Entry signal uses uncalibrated scoring inputs "
            "(stop_risk_reward_heuristic); explicit forecasts are required"
        ),
        "context": {
            "signal_id": evidence.external_signal_id,
            "symbol": "BTCUSDC",
            "scoring_input_source": "stop_risk_reward_heuristic",
        },
    }
    assert row["stop_status"] == {
        "applicable": True,
        "required": True,
        "present": True,
        "stop_loss": 94.0,
    }
    assert row["binding"] == {
        "binding_id": "binding-campaign",
        "active": True,
        "autopilot": False,
        "execution_mode": "paper",
        "activation_disposition": "binding_active",
        "approval_disposition": "manual_approval_required",
        "threshold_evaluation": "not_evaluated",
    }


def test_close_passes_with_concurrent_inactive_and_manual_binding_state() -> None:
    signal = _signal(SignalAction.CLOSE, stop_loss=None)
    evidence = RawSignalEvidence.from_signal(signal)

    row = _replay(evidence)[evidence.external_signal_id]

    assert row["entry_requirements"] == {
        "applicable": False,
        "allowed": True,
        "disposition": "not_applicable_non_entry",
        "blocked_reason": None,
        "message": None,
        "context": {},
    }
    assert row["binding"] == {
        "binding_id": "binding-campaign",
        "active": False,
        "autopilot": False,
        "execution_mode": "paper",
        "activation_disposition": "inactive_binding",
        "approval_disposition": "manual_approval_required",
        "threshold_evaluation": "not_evaluated",
    }


def test_replay_preserves_only_canonical_identity_and_exact_timestamps() -> None:
    evidence = RawSignalEvidence.from_signal(
        _signal(
            SignalAction.LONG,
            stop_loss=94.0,
            expected_return=0.04,
            predicted_risk=0.02,
        )
    )

    first = _replay(evidence)
    second = _replay(evidence)
    row = first[evidence.external_signal_id]
    reconstructed = evidence.to_signal()

    assert first == second
    assert list(first) == [evidence.external_signal_id]
    assert row["external_signal_id"] == evidence.external_signal_id
    assert row["generated_at"] == _GENERATED_AT.isoformat()
    assert row["valid_until"] == _VALID_UNTIL.isoformat()
    assert reconstructed.signal_id == reconstructed.external_signal_id
    assert reconstructed.signal_id == evidence.external_signal_id
    assert reconstructed.timestamp == _GENERATED_AT
    assert reconstructed.expires_at == _VALID_UNTIL
    assert reconstructed.source == "backtest"
    assert "source" not in json.loads(evidence.canonical_json)
    assert "runtime-id-must-not-survive-reconstruction" not in json.dumps(first)
    json.dumps(first, sort_keys=True, allow_nan=False)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda evidence: replace(evidence, external_signal_id=""), "missing canonical"),
        (
            lambda evidence: replace(evidence, external_signal_id="different-canonical-id"),
            "canonical mismatch",
        ),
        (
            lambda evidence: replace(evidence, canonical_json="{}"),
            "canonical mismatch",
        ),
    ],
)
def test_replay_fails_closed_on_missing_or_mismatched_canonical_identity(
    mutator: Callable[[RawSignalEvidence], RawSignalEvidence],
    message: str,
) -> None:
    evidence = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0))

    with pytest.raises(ValueError, match=message):
        _replay(mutator(evidence))


def test_replay_rejects_duplicate_canonical_ids() -> None:
    evidence = RawSignalEvidence.from_signal(_signal(SignalAction.LONG, stop_loss=94.0))

    with pytest.raises(ValueError, match="duplicate canonical external_signal_id"):
        _replay(evidence, evidence)


@pytest.mark.parametrize(
    ("signal", "require_stop", "require_explicit"),
    [
        (_signal(SignalAction.LONG, stop_loss=None), True, True),
        (_signal(SignalAction.LONG, stop_loss=94.0), True, True),
        (
            _signal(
                SignalAction.LONG,
                stop_loss=94.0,
                expected_return=0.04,
                predicted_risk=0.02,
            ),
            True,
            True,
        ),
        (_signal(SignalAction.CLOSE, stop_loss=None), True, True),
    ],
)
def test_risk_guard_delegation_preserves_shared_rule_decision(
    signal: Signal,
    require_stop: bool,
    require_explicit: bool,
) -> None:
    scoring_view = build_scoring_view(signal)
    signal.metadata["scoring_input_source"] = scoring_view.metadata["scoring_input_source"]
    shared = evaluate_entry_signal_requirements(
        signal,
        require_stop_loss=require_stop,
        require_explicit_scoring_inputs=require_explicit,
    )
    guard = RiskGuard().evaluate(
        user_id="research-user",
        signal=signal,
        profile={},
        user_strategy_config={
            "require_stop_loss": require_stop,
            "require_explicit_scoring_inputs": require_explicit,
        },
        account_state=SimpleNamespace(equity=100_000.0, daily_trades=0, open_positions=0),
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

    assert guard.allowed is shared.allowed
    assert guard.rule_code == shared.rule_code
    assert guard.message == shared.message
    assert guard.context == dict(shared.context)
