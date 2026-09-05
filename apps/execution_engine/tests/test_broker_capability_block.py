"""Live capability mismatches block instead of silently downgrading to paper (M-11).

A user who configured a live options trade on a broker that cannot execute options
(or an unknown broker) must get an explicit block — never a silent paper fill that
makes them believe they hold a live position.
"""

from __future__ import annotations

import pytest

from execution_engine.broker_resolver import BrokerCapabilityError, BrokerResolver
from execution_engine.config import BrokerType, ExecutionMode

_OPTIONS_MODE = ExecutionMode.BULL_CALL  # is_options(...) is True


def _resolve(broker: str, *, environment: str, exec_mode: ExecutionMode = _OPTIONS_MODE):
    return BrokerResolver.resolve_broker_type(
        exec_mode=exec_mode,
        profile={"broker": broker, "options_broker_crypto": broker},
        user_strategy_config={},
        asset_class="crypto",
        environment=environment,
    )


def test_live_options_on_spot_only_broker_blocks() -> None:
    # Coinbase is spot-only → options in live must raise (→ blocked result).
    with pytest.raises(BrokerCapabilityError):
        _resolve("coinbase", environment="live")


def test_paper_options_on_spot_only_broker_downgrades_to_paper() -> None:
    # In paper the safe fallback is kept (no real money at stake).
    assert _resolve("coinbase", environment="paper") == BrokerType.PAPER


def test_live_deribit_options_block_until_authenticated_certification() -> None:
    with pytest.raises(BrokerCapabilityError, match="certification workflow"):
        _resolve("deribit", environment="live")


def test_live_single_option_on_zerodha_blocks_without_exact_fills() -> None:
    with pytest.raises(BrokerCapabilityError, match="exact fill"):
        BrokerResolver.resolve_broker_type(
            exec_mode=ExecutionMode.OPTIONS_SINGLE,
            profile={"broker": "zerodha", "options_broker_equity": "zerodha"},
            user_strategy_config={},
            asset_class="equity",
            environment="live",
        )


def test_live_single_option_on_saxo_blocks_without_exact_fills() -> None:
    with pytest.raises(BrokerCapabilityError, match="exact fill"):
        BrokerResolver.resolve_broker_type(
            exec_mode=ExecutionMode.OPTIONS_SINGLE,
            profile={"broker": "saxo", "options_broker_equity": "saxo"},
            user_strategy_config={},
            asset_class="equity",
            environment="live",
        )


@pytest.mark.parametrize("broker", ["delta", "saxo", "zerodha"])
def test_live_multi_leg_options_require_native_atomic_support(broker: str) -> None:
    with pytest.raises(BrokerCapabilityError, match="does not support that mode"):
        _resolve(broker, environment="live", exec_mode=ExecutionMode.BULL_CALL)


def test_live_delta_spot_blocks_via_capability_matrix() -> None:
    with pytest.raises(BrokerCapabilityError, match="does not support that mode"):
        _resolve("delta", environment="live", exec_mode=ExecutionMode.SPOT)


def test_paper_delta_spot_falls_back_to_local_paper() -> None:
    assert _resolve("delta", environment="paper", exec_mode=ExecutionMode.SPOT) == BrokerType.PAPER


def test_live_delta_futures_blocks_without_order_scoped_exact_fills() -> None:
    with pytest.raises(BrokerCapabilityError, match="exact fill"):
        _resolve("delta", environment="live", exec_mode=ExecutionMode.FUTURES)


def test_live_coinbase_futures_blocks_not_only_options() -> None:
    with pytest.raises(BrokerCapabilityError, match="does not support that mode"):
        _resolve("coinbase", environment="live", exec_mode=ExecutionMode.FUTURES)


def test_live_unknown_broker_blocks() -> None:
    with pytest.raises(BrokerCapabilityError):
        _resolve("not_a_real_broker", environment="live", exec_mode=ExecutionMode.SPOT)


def test_paper_unknown_broker_blocks() -> None:
    with pytest.raises(BrokerCapabilityError):
        _resolve("not_a_real_broker", environment="paper", exec_mode=ExecutionMode.SPOT)
