"""The migrated strategies load through the same schema and loader as production."""

import json
from pathlib import Path

import jsonschema
import pytest

from lib_strategy.signals.loading import load_pure_strategy_core

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "ATRBreakout",
    "BBSqueezeBreakout",
    "WilliamsPullback",
    "VortexTrendCapture",
    "TimeDecayAdaptiveEMA",
    "ZScoreMeanReversion",
    "EngulfingPattern",
    "AdaptiveVWAPMeanReversion",
    "VolatilityClusteringReversion",
    "HurstRegime",
)


@pytest.mark.parametrize("name", NAMES)
def test_disabled_native_config_is_schema_valid_and_initializes(name):
    directory = ROOT / "strategies/indicator" / name
    config = json.loads((directory / "config.json").read_text())
    schema = json.loads((ROOT / "config/schemas/indicator_strategy_config.schema.json").read_text())
    jsonschema.validate(config, schema)
    assert config["enabled"] is False
    strategy = load_pure_strategy_core(directory)(
        strategy_id=config["strategy_id"],
        strategy_type="indicator",
        config={**config["parameters"], "strategy_version": config["strategy_version"]},
    )
    strategy.initialize()
    assert strategy.warmup_bars_needed <= config["market_data"]["bootstrap_bars"]
    assert strategy.supports_flat_model_boundary


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("split", [12, 700])
def test_recorded_bar_restart_needs_no_indicator_bootstrap(name, split):
    from datetime import UTC, datetime

    from lib_strategy.signals.pure_strategy import MarketState

    directory = ROOT / "strategies/indicator" / name
    config = json.loads((directory / "config.json").read_text())
    core_type = load_pure_strategy_core(directory)
    kwargs = {"config": {**config["parameters"], "strategy_version": config["strategy_version"]}}
    rows = json.loads(
        (ROOT / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json").read_text()
    )["bars"]
    bars = [
        MarketState(
            symbol="BTC-USD",
            timestamp=datetime.fromtimestamp(row["ts"], UTC),
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
        )
        for row in rows
    ]
    original = core_type(**kwargs)
    original.run(bars[:split])
    snapshot = json.loads(json.dumps(original.serialize_model_state(), allow_nan=False))
    restarted = core_type(**kwargs)
    restarted.bootstrap_history([])
    restarted.restore_model_state(snapshot)
    assert restarted.warmup_complete("BTC-USD") == original.warmup_complete("BTC-USD")
    # Redelivered last bar must not advance state or repeat a transition.
    continuation = bars[split - 1 : split + 100]
    expected = original.run(continuation)
    actual = restarted.run(continuation)

    def fields(signals):
        return [
            (s.action, s.timestamp, s.entry_price, s.stop_loss, s.external_signal_id)
            for s in signals
        ]

    assert fields(actual) == fields(expected)
    assert restarted.serialize_model_state() == original.serialize_model_state()


@pytest.mark.parametrize("name", NAMES)
def test_a_bootstrapped_core_decides_exactly_like_one_that_never_restarted(name):
    """A cold start replays history through ``bootstrap_history`` instead of
    ``run``. Every piece of indicator state must rebuild from those bars, or the
    worker decides differently after a restart than before it. The daily fixture
    is used because these ports consolidate to one bar a day."""
    from datetime import UTC, datetime

    from lib_strategy.signals.pure_strategy import MarketState

    directory = ROOT / "strategies/indicator" / name
    config = json.loads((directory / "config.json").read_text())
    core_type = load_pure_strategy_core(directory)
    kwargs = {"config": {**config["parameters"], "strategy_version": config["strategy_version"]}}
    rows = json.loads(
        (
            ROOT / "tests/fixtures/market_data/coinbase_btc_usdc_1d_2019-10-01_2022-06-26.json"
        ).read_text()
    )["bars"]
    bars = [
        MarketState(
            symbol="BTC-USDC",
            timestamp=datetime.fromtimestamp(row["ts"], UTC),
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
        )
        for row in rows
    ]
    # Several cut points, because state that fails to rebuild can still agree by
    # luck at one of them: a counter of consecutive conditions only diverges when
    # the window happens to end while that condition holds.
    for split in (79, 120, 200, 260, 330, 400, 500):
        warm = bars[:split]
        continuous = core_type(**kwargs)
        continuous.run(warm)
        bootstrapped = core_type(**kwargs)
        bootstrapped.bootstrap_history(warm)

        assert bootstrapped.warmup_complete("BTC-USDC") == continuous.warmup_complete("BTC-USDC")
        # Indicator streams must be identical. Position state deliberately is
        # not: replay suppresses entries, and a restart restores a position from
        # the durable checkpoint rather than from the bars it replays.
        assert (
            bootstrapped.serialize_model_state()["streams"]
            == continuous.serialize_model_state()["streams"]
        ), f"indicator state diverges after replaying {split} bars"
        assert bootstrapped.state_for("BTC-USDC").position == 0


@pytest.mark.parametrize(
    "corruption",
    ["missing_stream", "nonfinite", "period", "timestamp", "count", "entry_after_checkpoint"],
)
def test_checkpoint_corruption_cannot_partially_restore_live_state(corruption):
    import copy
    from datetime import UTC, datetime, timedelta

    from lib_strategy.signals.pure_strategy import MarketState, ModelStateContractError

    directory = ROOT / "strategies/indicator/ATRBreakout"
    strategy = load_pure_strategy_core(directory)(config={"atr_period": "2", "lookback": "2"})
    start = datetime(2026, 6, 10, tzinfo=UTC)
    strategy.run(
        [
            MarketState(
                "BTC-USD", start + timedelta(minutes=i), price, price + 1, price - 1, price, 1
            )
            for i, price in enumerate([10, 11, 9, 12, 11, 30])
        ]
    )
    before = strategy.serialize_model_state()
    snapshot = copy.deepcopy(before)
    stream = snapshot["streams"]["symbols"]["BTC-USD"]
    if corruption == "missing_stream":
        del stream["indicators"]["atr"]
    elif corruption == "nonfinite":
        stream["indicators"]["atr"]["state"]["_value"] = float("nan")
    elif corruption == "period":
        stream["indicators"]["atr"]["config"]["_period"] = 99
    elif corruption == "timestamp":
        stream["last_timestamp"] = "2026-06-10T00:00:00"
    elif corruption == "count":
        stream["accepted_bars"] = True
    else:
        model = snapshot["symbol_states"]["BTC-USD"]
        model.update(
            position=1,
            entry_price=30,
            entry_time=(start + timedelta(days=1)).isoformat(),
            custom={"stop_loss": 20},
        )
    with pytest.raises(ModelStateContractError):
        strategy.restore_model_state(snapshot)
    assert strategy.serialize_model_state() == before
