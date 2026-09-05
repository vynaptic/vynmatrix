"""Versioned model-state contracts against frozen public Coinbase history."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.pure_strategy import (
    MarketState,
    ModelStateContractError,
    PureSignalStrategy,
)
from lib_strategy.signals.signal import Signal, SignalAction

_REPO = Path(__file__).resolve().parents[1]
_PUBLIC_FIXTURE = (
    _REPO / "tests" / "fixtures" / "market_data" / "coinbase_btcusd_1m_2026-06-10.json"
)
_STRATEGIES = (("SwingHighLowPMO", "SwingHighLowPMOCore"),)


class _CoreFactory(Protocol):
    def __call__(self, **kwargs: Any) -> PureSignalStrategy: ...


def _load_core(name: str, class_name: str) -> _CoreFactory:
    path = _REPO / "strategies" / "indicator" / name / "core.py"
    spec = importlib.util.spec_from_file_location(f"{name}_state_contract", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core = getattr(module, class_name)
    assert isinstance(core, type)
    assert issubclass(core, PureSignalStrategy)
    return cast("_CoreFactory", core)


def _config(name: str) -> tuple[str, dict[str, Any]]:
    payload = json.loads((_REPO / "strategies" / "indicator" / name / "config.json").read_text())
    parameters = dict(payload["parameters"])
    parameters["strategy_version"] = payload["strategy_version"]
    return str(payload["strategy_id"]), parameters


def _public_bars() -> tuple[MarketState, ...]:
    payload = json.loads(_PUBLIC_FIXTURE.read_text())
    assert payload["product"] == "BTC-USD"
    assert payload["source"] == "coinbase_exchange_public"
    assert payload["bar_count"] == 1501
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
        for row in payload["bars"]
    )


def _new_strategy(
    name: str,
    class_name: str,
) -> tuple[PureSignalStrategy, BacktestSignalEmitter]:
    strategy_id, config = _config(name)
    emitter = BacktestSignalEmitter()
    strategy = _load_core(name, class_name)(
        strategy_id=strategy_id,
        config=config,
        emitter=emitter,
    )
    strategy.bootstrap_history([])
    return strategy, emitter


def _feed_one(strategy: PureSignalStrategy, bar: MarketState) -> None:
    strategy.record_bar(bar.symbol)
    strategy.on_data(bar)


def _signal_contract(signal: Signal) -> tuple[Any, ...]:
    return (
        signal.external_signal_id,
        signal.strategy_id,
        signal.strategy_version,
        signal.symbol,
        signal.action,
        signal.timestamp,
        signal.entry_price,
        signal.stop_loss,
        signal.take_profit,
        signal.metadata,
    )


@pytest.mark.parametrize(("name", "class_name"), _STRATEGIES)
def test_restored_long_model_continues_without_repeating_entry(
    name: str,
    class_name: str,
) -> None:
    """A real-history long snapshot must produce the uninterrupted continuation."""
    bars = _public_bars()
    uninterrupted, uninterrupted_emitter = _new_strategy(name, class_name)

    entry_index: int | None = None
    for index, bar in enumerate(bars):
        _feed_one(uninterrupted, bar)
        if any(
            signal.action == SignalAction.LONG for signal in uninterrupted_emitter.get_signals()
        ):
            entry_index = index
            break
    assert entry_index is not None

    snapshot = uninterrupted.serialize_model_state()
    snapshot = json.loads(json.dumps(snapshot, allow_nan=False, sort_keys=True))
    assert snapshot["symbol_states"]["BTCUSD"]["position"] == 1

    restored, restored_emitter = _new_strategy(name, class_name)
    restored.bootstrap_history(list(bars[: entry_index + 1]))
    assert restored.state_for("BTCUSD").position == 0
    restored.restore_model_state(snapshot)
    assert restored.state_for("BTCUSD").position == 1

    uninterrupted_before = len(uninterrupted_emitter.get_signals())
    for bar in bars[entry_index + 1 :]:
        _feed_one(uninterrupted, bar)
        _feed_one(restored, bar)

    uninterrupted_tail = uninterrupted_emitter.get_signals()[uninterrupted_before:]
    restored_tail = restored_emitter.get_signals()
    assert restored_tail
    assert restored_tail[0].action == SignalAction.CLOSE
    assert [_signal_contract(signal) for signal in restored_tail] == [
        _signal_contract(signal) for signal in uninterrupted_tail
    ]


@pytest.mark.parametrize(("name", "class_name"), _STRATEGIES)
def test_restore_rejects_incompatible_strategy_version(
    name: str,
    class_name: str,
) -> None:
    strategy, _emitter = _new_strategy(name, class_name)
    snapshot = strategy.serialize_model_state()
    snapshot["strategy_version"] = "incompatible-version"

    with pytest.raises(ModelStateContractError, match="Incompatible model-state identity"):
        strategy.restore_model_state(snapshot)
