"""SC-1: execution dedup keys on the STABLE signal identity.

The execution dedup / idempotency key is the durable duplicate-submission guard.
``signal_id`` is regenerated per POST (a fresh ``run_id`` is folded into it), so
keying on it lets a redelivered bar (worker restart, NOTIFY redelivery) place a
second order. Keying on the stable ``external_signal_id`` makes redelivery dedup
to the same key.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from execution_engine._dispatch import build_dispatch_context
from execution_engine.deduplication import ExecutionDeduplicator
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_execution_dedup_key

TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _signal(*, signal_id: str, run_id: str | None, external_signal_id: str | None) -> Signal:
    return Signal(
        strategy_id="swing_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.8,
        signal_id=signal_id,
        run_id=run_id,
        timestamp=TS,
        external_signal_id=external_signal_id,
    )


def _ctx(sig: Signal):
    return build_dispatch_context(user_id="user-1", profile={}, user_strategy_config={}, signal=sig)


def _key(identity: str) -> str:
    return ExecutionDeduplicator().get_execution_key(
        external_signal_id=identity,
        user_id="u",
        broker_account_id=1,
        symbol="BTCUSD",
        action="long",
    )


def test_dedup_identity_prefers_external_signal_id() -> None:
    ctx = _ctx(_signal(signal_id="sig-A", run_id="run-A", external_signal_id="ext-1"))
    assert ctx.dedup_identity == "ext-1"


def test_dispatch_refuses_signal_without_external_identity() -> None:
    with pytest.raises(ValueError, match="has no canonical external_signal_id"):
        _ctx(_signal(signal_id="sig-A", run_id="run-A", external_signal_id=None))


def test_dispatch_refuses_noncanonical_route_snapshot_before_execution() -> None:
    signal = _signal(signal_id="sig-A", run_id="run-A", external_signal_id="ext-1")

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        build_dispatch_context(
            user_id="user-1",
            profile={
                "_broker_route_snapshot": {
                    "broker": "paper",
                    "broker_account_id": 1,
                    "broker_environment": "paper",
                    "currency": "EUR",
                }
            },
            user_strategy_config={},
            signal=signal,
        )


def test_redelivery_with_fresh_run_id_dedups_to_same_key() -> None:
    # Same bar re-emitted -> fresh run_id -> different signal_id, but the stable
    # external_signal_id is unchanged, so the dedup key MUST be identical.
    first = _ctx(_signal(signal_id="sig-A", run_id="run-A", external_signal_id="ext-1"))
    second = _ctx(_signal(signal_id="sig-B", run_id="run-B", external_signal_id="ext-1"))

    assert first.signal_id != second.signal_id  # signal_id really did change
    assert _key(first.dedup_identity) == _key(second.dedup_identity)  # but the key is stable
    # Sanity: keying on the raw signal_id (the old behaviour) WOULD have differed.
    assert _key(first.signal_id) != _key(second.signal_id)


def test_get_execution_key_matches_shared_helper() -> None:
    # Scoring and execution must derive the key identically (one helper).
    assert _key("ext-1") == compute_execution_dedup_key("ext-1", "u", 1, "BTCUSD", "long")


@pytest.mark.parametrize(
    ("identity", "user_id", "symbol", "action", "component"),
    [
        ("", "u", "BTCUSD", "long", "identity"),
        ("ext-1", " ", "BTCUSD", "long", "user_id"),
        ("ext-1", "u", "\t", "long", "symbol"),
        ("ext-1", "u", "BTCUSD", "", "action"),
    ],
)
def test_execution_key_refuses_blank_components(
    identity: str,
    user_id: str,
    symbol: str,
    action: str,
    component: str,
) -> None:
    with pytest.raises(ValueError, match=rf"Execution dedup {component} must be non-empty"):
        compute_execution_dedup_key(identity, user_id, 1, symbol, action)


@pytest.mark.parametrize("broker_account_id", [0, -1, True])
def test_execution_key_refuses_invalid_broker_account_id(
    broker_account_id: int,
) -> None:
    with pytest.raises(ValueError, match="broker_account_id must be a positive integer"):
        compute_execution_dedup_key(
            "ext-1",
            "user-1",
            broker_account_id,
            "BTCUSD",
            "long",
        )
