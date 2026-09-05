"""H2: build_scoring_view must not yield a zero expected_return for directional
signals, regardless of how complete the price ladder is.

A placeholder ``expected_return`` of 0.0 (the whole deployable fleet, whose
emit helpers expose no expected_return) collapses ``sharpe_raw`` and the asset
score to 0 so the signal can never clear a binding threshold. The adapter now
derives mu/sigma from a full ladder, an entry+stop-only ladder, or — failing
both — a confidence-based synthesis.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "python" / "lib_strategy"))

from lib_strategy.signals.adapters.scoring import build_scoring_view
from lib_strategy.signals.signal import Signal, SignalAction

TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _signal(**overrides: object) -> Signal:
    base: dict[str, object] = {
        "strategy_id": "test_strategy_v1",
        "strategy_type": "indicator",
        "symbol": "BTCUSD",
        "action": SignalAction.LONG,
        "confidence": 0.75,
        "timestamp": TS,
        "horizon": "1D",
    }
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


def test_entry_only_signal_is_non_zero() -> None:
    view = build_scoring_view(_signal(entry_price=100.0))
    assert view.expected_return > 0.0
    assert view.sharpe_raw != 0.0


def test_no_ladder_signal_is_non_zero() -> None:
    view = build_scoring_view(_signal())
    assert view.expected_return > 0.0
    assert view.sharpe_raw != 0.0


def test_entry_plus_stop_only_derives_from_stop_and_risk_reward() -> None:
    # stop distance = 5%, risk_reward 2.0, confidence 0.75 -> mu = 0.05*2*0.75
    view = build_scoring_view(
        _signal(entry_price=100.0, stop_loss=95.0, metadata={"risk_reward": 2.0})
    )
    assert view.predicted_risk == 0.05
    assert abs(view.expected_return - (0.05 * 2.0 * 0.75)) < 1e-9


def test_full_ladder_unchanged_mu_from_take_profit() -> None:
    # target distance = 10%, confidence 0.75 -> mu = 0.10*0.75 ; sigma = 5%
    view = build_scoring_view(_signal(entry_price=100.0, stop_loss=95.0, take_profit=110.0))
    assert view.predicted_risk == 0.05
    assert abs(view.expected_return - (0.10 * 0.75)) < 1e-9


def test_explicit_expected_return_is_respected() -> None:
    view = build_scoring_view(_signal(entry_price=100.0, expected_return=0.123))
    assert view.expected_return == 0.123


def test_explicit_risk_with_synthesized_return_is_marked_mixed() -> None:
    view = build_scoring_view(_signal(predicted_risk=0.04))

    assert view.predicted_risk == 0.04
    assert view.expected_return > 0.0
    assert view.metadata["scoring_input_source"] == "mixed_explicit_and_heuristic"
