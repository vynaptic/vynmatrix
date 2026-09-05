"""SwingHighLowPMO core contracts against frozen public Coinbase candles.

This is a deterministic historical replay of a strategy core, not a service or
end-to-end test. PostgreSQL persistence, scoring, outbox dispatch, paper
execution, P&L, and feedback are verified by their owning suites and the Docker
pipeline gate.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Protocol, cast

import pytest

from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import Signal, SignalAction

_REPO = Path(__file__).resolve().parent.parent
_CONFIG = _REPO / "strategies" / "indicator" / "SwingHighLowPMO" / "config.json"
_PUBLIC_FIXTURE = (
    _REPO / "tests" / "fixtures" / "market_data" / "coinbase_btcusd_1m_2026-06-10.json"
)
_EXPECTED_PUBLIC_BAR_COUNT = 1501


class _StrategyCoreFactory(Protocol):
    def __call__(self, **kwargs: object) -> PureSignalStrategy: ...


def _load_core() -> _StrategyCoreFactory:
    path = _REPO / "strategies" / "indicator" / "SwingHighLowPMO" / "core.py"
    spec = importlib.util.spec_from_file_location("SwingHighLowPMO_core", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = module.SwingHighLowPMOCore
    assert isinstance(candidate, type)
    assert issubclass(candidate, PureSignalStrategy)
    return cast("_StrategyCoreFactory", candidate)


def _strategy_config(**overrides: object) -> dict[str, object]:
    payload = json.loads(_CONFIG.read_text())
    parameters = payload["parameters"]
    assert isinstance(parameters, dict)
    config: dict[str, object] = dict(parameters)
    config["strategy_version"] = payload["strategy_version"]
    config.update(overrides)
    return config


def _public_coinbase_bars() -> tuple[MarketState, ...]:
    payload = json.loads(_PUBLIC_FIXTURE.read_text())
    assert payload["product"] == "BTC-USD"
    assert payload["source"] == "coinbase_exchange_public"
    assert payload["granularity_seconds"] == 60
    assert payload["bar_count"] == _EXPECTED_PUBLIC_BAR_COUNT

    rows = payload["bars"]
    assert isinstance(rows, list)
    assert len(rows) == _EXPECTED_PUBLIC_BAR_COUNT
    return tuple(
        MarketState(
            symbol="BTCUSD",
            timestamp=datetime.fromtimestamp(row["ts"], tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            metadata={
                "price_source": payload["source"],
                "price_timeframe": "1m",
            },
        )
        for row in rows
    )


def _run_replay(**overrides: object) -> list[Signal]:
    emitter = BacktestSignalEmitter()
    core = _load_core()(
        strategy_id="swing_high_low_pmo_v1",
        config=_strategy_config(**overrides),
        emitter=emitter,
    )
    return core.run(list(_public_coinbase_bars()))


def test_public_coinbase_replay_emits_only_long_and_close() -> None:
    bars = _public_coinbase_bars()

    signals = _run_replay()

    actions = [signal.action for signal in signals]
    assert actions
    assert actions[0] == SignalAction.LONG
    assert SignalAction.CLOSE in actions
    assert SignalAction.SHORT not in actions
    assert all(previous != current for previous, current in pairwise(actions))

    observed_timestamps = {bar.timestamp for bar in bars}
    assert all(signal.timestamp in observed_timestamps for signal in signals)
    assert all(signal.external_signal_id for signal in signals)
    assert len({signal.external_signal_id for signal in signals}) == len(signals)

    for entry in (signal for signal in signals if signal.action == SignalAction.LONG):
        assert entry.entry_price is not None
        assert entry.stop_loss is not None
        assert entry.take_profit is not None
        assert entry.stop_loss < entry.entry_price < entry.take_profit

    for close in (signal for signal in signals if signal.action == SignalAction.CLOSE):
        assert close.entry_price is not None
        assert close.metadata.get("exit_reason") in {
            "Stop Loss",
            "Take Profit",
            "Time Exit",
        }


def test_public_coinbase_replay_signal_identity_is_deterministic() -> None:
    first = _run_replay()
    second = _run_replay()

    assert [signal.external_signal_id for signal in first] == [
        signal.external_signal_id for signal in second
    ]


def test_strategy_rejects_negative_position_and_invalid_risk_controls() -> None:
    core_type = _load_core()
    core = core_type(
        strategy_id="swing_high_low_pmo_v1",
        config=_strategy_config(),
        emitter=BacktestSignalEmitter(),
    )
    core.initialize()
    core.state_for("BTCUSD").position = -1

    with pytest.raises(RuntimeError, match="long-only"):
        core.on_data(_public_coinbase_bars()[0])

    invalid_cases = (
        ({"trading_start_hour": "23", "trading_end_hour": "7"}, "trading hours"),
        ({"risk_reward_ratio": "nan"}, "risk_reward_ratio"),
        ({"max_swing_age_bars": "2"}, "max_swing_age_bars"),
        ({"max_stop_distance_pct": "nan"}, "max_stop_distance_pct"),
    )
    for overrides, message in invalid_cases:
        invalid = core_type(
            strategy_id="swing_high_low_pmo_v1",
            config=_strategy_config(**overrides),
            emitter=BacktestSignalEmitter(),
        )
        with pytest.raises(ValueError, match=message):
            invalid.initialize()


def test_strategy_config_is_explicit_long_only_signal_worker() -> None:
    payload = json.loads(_CONFIG.read_text())

    assert payload["runner_kind"] == "signal_worker"
    assert payload["trade_direction_mode"] == "long_only"
    assert payload["parameters"]["universe"] == "BTCUSDC,ETHUSDC,SOLUSDC"
    assert payload["parameters"]["trade_direction_mode"] == "long_only"
    assert payload["market_data"] == {
        "source": "coinbase_live",
        "timeframe": "1m",
        "consolidation_minutes": 15,
        "bootstrap_bars": 500,
    }
