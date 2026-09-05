"""H3: every PureSignalStrategy emission carries a deterministic external_signal_id.

The scoring store only dedups canonical signals when ``external_signal_id`` is
set. The framework defaults the key inside ``emit_signal`` so every retained
core is idempotent by construction (worker restart, NOTIFY redelivery, and
dual-runtime overlap deduplicate to the same row).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "python" / "lib_strategy"))

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id

TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


class _Strat(PureSignalStrategy):
    def initialize(self) -> None:  # pragma: no cover - not exercised here
        ...

    def on_data(self, state: MarketState) -> None:  # pragma: no cover - not exercised here
        ...


def _strat(strategy_id: str = "test_strategy_v1") -> _Strat:
    return _Strat(strategy_id=strategy_id, strategy_type="indicator", config={})


def test_emit_defaults_external_signal_id_when_absent() -> None:
    sig = _strat().emit_signal(action=SignalAction.LONG, symbol="BTCUSD", timestamp=TS)
    assert sig.external_signal_id, "framework must default external_signal_id"


def test_external_signal_id_is_deterministic_for_same_bar() -> None:
    a = _strat().emit_signal(action=SignalAction.LONG, symbol="BTCUSD", timestamp=TS)
    b = _strat().emit_signal(action=SignalAction.LONG, symbol="BTCUSD", timestamp=TS)
    assert a.external_signal_id == b.external_signal_id


def test_external_signal_id_differs_by_bar_and_action() -> None:
    base = _strat().emit_signal(action=SignalAction.LONG, symbol="BTCUSD", timestamp=TS)
    later = _strat().emit_signal(
        action=SignalAction.LONG,
        symbol="BTCUSD",
        timestamp=datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
    )
    other_action = _strat().emit_signal(action=SignalAction.SHORT, symbol="BTCUSD", timestamp=TS)
    assert base.external_signal_id != later.external_signal_id
    assert base.external_signal_id != other_action.external_signal_id


def test_explicit_external_signal_id_is_preserved() -> None:
    sig = _strat().emit_signal(
        action=SignalAction.LONG,
        symbol="BTCUSD",
        timestamp=TS,
        external_signal_id="explicit-id",
    )
    assert sig.external_signal_id == "explicit-id"


def test_default_matches_canonical_helper_with_exit_reason() -> None:
    sig = _strat().emit_close(symbol="BTCUSD", timestamp=TS, reason="trailing_stop")
    expected = compute_external_signal_id(
        strategy_id="test_strategy_v1",
        symbol="BTCUSD",
        action=SignalAction.CLOSE,
        bar_close_ts=TS,
        strategy_version=None,
        reason="trailing_stop",
    )
    assert sig.external_signal_id == expected


def test_config_attribution_defaults_apply_to_entry_and_exit() -> None:
    strat = _Strat(
        strategy_id="test_strategy_v1",
        strategy_type="indicator",
        config={
            "asset_class": "crypto",
            "strategy_version": "1.2.3",
            "signal_source": "paper",
        },
    )

    entry = strat.emit_long(symbol="BTCUSDC", timestamp=TS, entry_price=100.0)
    exit_signal = strat.emit_close(symbol="BTCUSDC", timestamp=TS, exit_price=101.0, reason="test")

    for signal in (entry, exit_signal):
        assert signal.asset_class == "crypto"
        assert signal.strategy_version == "1.2.3"
        assert signal.source == "paper"
    expected_exit_id = compute_external_signal_id(
        strategy_id="test_strategy_v1",
        symbol="BTCUSDC",
        action=SignalAction.CLOSE,
        bar_close_ts=TS,
        strategy_version="1.2.3",
        reason="test",
    )
    assert exit_signal.external_signal_id == expected_exit_id


def test_explicit_attribution_overrides_config_defaults() -> None:
    strat = _Strat(
        strategy_id="test_strategy_v1",
        strategy_type="indicator",
        config={
            "asset_class": "crypto",
            "strategy_version": "1.2.3",
            "signal_source": "paper",
        },
    )
    signal = strat.emit_long(
        symbol="SPY",
        timestamp=TS,
        asset_class="etf",
        strategy_version="2.0.0",
        source="backtest",
    )
    assert signal.asset_class == "etf"
    assert signal.strategy_version == "2.0.0"
    assert signal.source == "backtest"


def test_explicit_forecast_and_expiry_fields_are_preserved() -> None:
    expiry = TS + timedelta(hours=1)
    signal = _strat().emit_long(
        symbol="BTCUSDC",
        timestamp=TS,
        expected_return=0.012,
        predicted_risk=0.025,
        horizon_days=5.0,
        expires_at=expiry,
    )

    assert signal.expected_return == 0.012
    assert signal.predicted_risk == 0.025
    assert signal.horizon_days == 5.0
    assert signal.expires_at == expiry
