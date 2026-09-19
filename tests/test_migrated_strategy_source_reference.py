"""Independent source decisions and documented corrections on recorded Coinbase bars.

The minute and daily fixtures establish component behavior only. The daily
Engulfing witness covers both directions; neither fixture qualifies economics.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
REFERENCE = INPUT.with_name("coinbase_btcusd_1m_2026-06-10_backtrader_ports_reference.json")


@pytest.fixture(scope="module")
def captured():
    contents = REFERENCE.read_bytes()
    assert (
        hashlib.sha256(contents).hexdigest()
        == "28076615af5622bada829a37e909443ce32b5a93a475b454715a86db3fafe5cb"
    )
    return json.loads(contents)


@pytest.fixture(scope="module")
def bars():
    return [
        MarketState(
            symbol="BTC-USD",
            timestamp=datetime.fromtimestamp(row["ts"], UTC),
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
        )
        for row in json.loads(INPUT.read_text())["bars"]
    ]


def _core(name):
    directory = ROOT / "strategies/indicator" / name
    config = json.loads((directory / "config.json").read_text())
    return load_pure_strategy_core(directory)(
        strategy_id=config["strategy_id"],
        strategy_type="indicator",
        config={**config["parameters"], "strategy_version": config["strategy_version"]},
    )


@pytest.fixture(scope="module")
def native_decisions(captured, bars):
    indexes = {bar.timestamp: index for index, bar in enumerate(bars)}
    return {
        record["name"]: [
            [indexes[signal.timestamp], signal.action.value]
            for signal in _core(record["name"]).run(bars)
        ]
        for record in captured["strategies"]
    }


def _source(captured, name):
    return next(record for record in captured["strategies"] if record["name"] == name)


def test_reference_binds_all_ten_reviewed_sources_and_identical_recorded_input(captured):
    assert hashlib.sha256(INPUT.read_bytes()).hexdigest() == captured["input"]["sha256"]
    assert captured["input"]["bar_count"] == 1501
    assert captured["environment"]["backtrader"] == "1.9.78.123"
    assert captured["environment"]["ta_lib"] == "0.6.8"
    assert captured["execution"]["runonce"] is True
    assert len(captured["strategies"]) == 10
    assert {"lib_strategy", "lib_indicators"} <= captured["environment"]["loaded_code"].keys()
    for record in captured["strategies"]:
        path = ROOT / "strategies/indicator" / record["name"] / "config.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["config_sha256"]
        assert json.loads(path.read_text())["metadata"]["source_sha256"] == record["source_sha256"]


@pytest.mark.parametrize(
    "name",
    ["ATRBreakout", "VortexTrendCapture", "VolatilityClusteringReversion", "ZScoreMeanReversion"],
)
def test_every_entry_matches_the_source_despite_documented_exit_changes(
    captured, native_decisions, name
):
    expected = [row for row in _source(captured, name)["source_decisions"] if row[1] != "CLOSE"]
    actual = [row for row in native_decisions[name] if row[1] != "CLOSE"]
    assert actual == expected
    assert {row[1] for row in actual} == {"LONG", "SHORT"}


def test_bb_corrects_percent_units_without_changing_any_common_obv_value(
    captured, native_decisions, bars
):
    reference = _source(captured, "BBSqueezeBreakout")
    witness = next(row for row in reference["trace_checkpoints"] if row["index"] == 59)
    assert reference["source_decisions"][0] == [59, "LONG"]
    assert witness["previous_rank"] == pytest.approx(0.85)
    assert witness["previous_rank"] > 0.10
    assert native_decisions["BBSqueezeBreakout"][0] == [170, "SHORT"]
    contract = reference["obv_series"]
    core = _core("BBSqueezeBreakout")
    observed = []
    for index, bar in enumerate(bars):
        core.run([bar])
        if index >= contract["first_index"]:
            indicators = core._indicators[bar.symbol]
            observed.append([index, indicators["obv"].hex(), indicators["obv_sma"].value.hex()])
    assert len(observed) == contract["count"]
    encoded = json.dumps(observed, separators=(",", ":"), allow_nan=False).encode()
    assert hashlib.sha256(encoded).hexdigest() == contract["sha256"]


def test_williams_uses_the_documented_histogram_instead_of_the_source_macd_line(
    captured, native_decisions
):
    reference = _source(captured, "WilliamsPullback")
    witness = next(row for row in reference["trace_checkpoints"] if row["index"] == 50)
    assert reference["source_decisions"][0] == [50, "SHORT"]
    assert witness["macd_line"] < 0 < witness["histogram"]
    assert native_decisions["WilliamsPullback"][0] == [80, "SHORT"]


def test_time_decay_recovers_from_the_sources_latched_order(captured, native_decisions):
    reference = _source(captured, "TimeDecayAdaptiveEMA")
    assert reference["source_decisions"] == [[20, "SHORT"]]
    final = next(row for row in reference["trace_checkpoints"] if row["index"] == 1500)
    assert final["pending_order"]
    assert not final["protective_order"]
    assert final["filter_samples"] == 2
    actual = native_decisions["TimeDecayAdaptiveEMA"]
    assert actual[0] == [21, "LONG"]
    assert {row[1] for row in actual[1:]} == {"LONG", "SHORT", "CLOSE"}


def test_hurst_matches_source_until_signal_bar_protection_changes_the_exit(
    captured, native_decisions
):
    expected = _source(captured, "HurstRegime")["source_decisions"]
    actual = native_decisions["HurstRegime"]
    assert [row for row in actual if row[0] < 554] == [row for row in expected if row[0] < 554]
    assert [554, "CLOSE"] in actual
    assert [559, "CLOSE"] in expected
    assert {row[1] for row in actual} == {"LONG", "SHORT", "CLOSE"}


def test_vwap_closes_model_before_a_subsequent_entry(captured, native_decisions):
    expected = _source(captured, "AdaptiveVWAPMeanReversion")["source_decisions"]
    actual = native_decisions["AdaptiveVWAPMeanReversion"]
    assert expected[0] == actual[0] == [39, "LONG"]
    assert expected[1] == [43, "LONG"]  # source stop fills before same-bar re-entry
    assert actual[1:3] == [[43, "CLOSE"], [44, "LONG"]]


def test_engulfing_recorded_fixture_has_only_flat_path_coverage(captured, native_decisions):
    assert _source(captured, "EngulfingPattern")["source_decisions"] == []
    assert native_decisions["EngulfingPattern"] == []


def test_source_stop_recursion_has_individual_fill_lineage(captured):
    fills = [
        row
        for row in _source(captured, "ZScoreMeanReversion")["fill_witnesses"]
        if row["index"] == 817
    ]
    assert len(fills) == 17
    assert len({row["order_id"] for row in fills}) == 17
    assert [row["post_fill_position"] for row in fills] == list(range(17))
    assert {row["position_at_notification"] for row in fills} == {16}


@pytest.fixture(scope="module")
def engulfing_daily():
    path = INPUT.with_name("coinbase_btc_usdc_1d_2019-10-01_2022-06-26.json")
    witness = path.with_name(path.stem + "_engulfing_reference.json")
    assert hashlib.sha256(witness.read_bytes()).hexdigest() == (
        "840f688c3467e01693ee7fbbb42cd3f65aa038a55ce9a831520f0fee1471f126"
    )
    reference = json.loads(witness.read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["input"]["sha256"]
    payload = json.loads(path.read_text())
    assert payload["timestamp_semantics"] == "period_end"
    assert payload["granularity_seconds"] == 86400
    assert len(payload["bars"]) == reference["input"]["bar_count"] == 1000
    config_path = ROOT / "strategies/indicator/EngulfingPattern/config.json"
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == reference["config_sha256"]
    assert (
        json.loads(config_path.read_text())["metadata"]["source_sha256"]
        == (reference["source_sha256"])
    )
    states = [
        MarketState(
            symbol=payload["product"],
            timestamp=datetime.fromtimestamp(row["ts"], UTC),
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
        )
        for row in payload["bars"]
    ]
    return reference, states


def test_daily_engulfing_entries_resume_after_source_protection_reopens_exposure(engulfing_daily):
    reference, states = engulfing_daily
    indexes = {bar.timestamp: index for index, bar in enumerate(states)}
    decisions = [
        [indexes[signal.timestamp], signal.action.value]
        for signal in _core("EngulfingPattern").run(states)
    ]
    assert reference["source_decisions"] == [[461, "SHORT"], [992, "LONG"]]
    assert decisions == [
        [461, "SHORT"],
        [462, "CLOSE"],
        [988, "LONG"],
        [989, "CLOSE"],
        [992, "LONG"],
        [993, "CLOSE"],
        [994, "LONG"],
        [995, "CLOSE"],
    ]
    fills = reference["fill_witnesses"]
    assert [(row["index"], row["order_type"], row["post_fill_position"]) for row in fills[:4]] == [
        (462, "Market", -1),
        (463, "StopTrail", 0),
        (465, "StopTrail", -1),
        (988, "StopTrail", -1),
    ]


@pytest.mark.parametrize("split", [462, 989])
def test_daily_engulfing_restart_preserves_open_protection_and_signal_identity(
    engulfing_daily, split
):
    _reference, states = engulfing_daily
    original = _core("EngulfingPattern")
    original.run(states[:split])
    snapshot = json.loads(json.dumps(original.serialize_model_state(), allow_nan=False))
    restarted = _core("EngulfingPattern")
    restarted.bootstrap_history([])
    restarted.restore_model_state(snapshot)
    continuation = states[split - 1 :]
    expected, actual = original.run(continuation), restarted.run(continuation)

    # signal_id is an envelope UUID; external_signal_id is the replay-stable key.
    def canonical_fields(signals):
        return [
            {key: value for key, value in signal.to_dict().items() if key != "signal_id"}
            for signal in signals
        ]

    assert all(signal.external_signal_id for signal in actual)
    assert canonical_fields(actual) == canonical_fields(expected)
    assert actual[0].action.value == "CLOSE"
    assert original.serialize_model_state() == restarted.serialize_model_state()
