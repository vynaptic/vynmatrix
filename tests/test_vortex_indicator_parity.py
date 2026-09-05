"""Real-bar parity for the shared Vortex, SMA, and ATR indicator stack.

The checked-in reference was captured with Backtrader 1.9.78.123 over the
frozen Coinbase fixture. Backtrader is intentionally not a platform test
dependency: source, input, capture, and serialization identities are pinned in
the reference metadata, while full-series hashes verify every common warm
output produced by the platform indicators.  This is component coverage used
by any strategy that consumes :class:`lib_indicators.Vortex`; it is not a
retired-strategy runtime test.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from lib_indicators import AverageTrueRange, SimpleMovingAverage, Vortex

_FIXTURE_DIR = Path(__file__).parent / "fixtures/market_data"
_INPUT_PATH = _FIXTURE_DIR / "coinbase_btcusd_1m_2026-06-10.json"
_REFERENCE_PATH = _FIXTURE_DIR / "coinbase_btcusd_1m_2026-06-10_backtrader_vortex_reference.json"
_REFERENCE_SHA256 = "f55b3cfe47bacc2fb82895d6dfadad82367ae2f8c154f627895ebce1ae835a26"


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _calculate_platform_outputs(
    bars: list[dict[str, float]],
    *,
    vortex_period: int,
    sma_period: int,
    atr_period: int,
) -> tuple[list[list[int | float]], list[list[int | str]]]:
    vortex = Vortex(vortex_period)
    sma = SimpleMovingAverage(sma_period)
    atr = AverageTrueRange(atr_period)
    numeric_rows: list[list[int | float]] = []
    encoded_rows: list[list[int | str]] = []

    for bar_index, bar in enumerate(bars):
        vortex.update(bar["high"], bar["low"], bar["close"])
        sma.update(bar["close"])
        atr.update(bar["high"], bar["low"], bar["close"])
        if not (vortex.is_ready and sma.is_ready and atr.is_ready):
            continue

        assert vortex.vi_plus is not None
        assert vortex.vi_minus is not None
        assert sma.value is not None
        assert atr.value is not None
        values = [vortex.vi_plus, vortex.vi_minus, sma.value, atr.value]
        numeric_rows.append([bar_index, *values])
        encoded_rows.append([bar_index, *(value.hex() for value in values)])

    return numeric_rows, encoded_rows


def test_vortex_stack_matches_every_warmed_backtrader_output() -> None:
    reference_bytes = _REFERENCE_PATH.read_bytes()
    assert hashlib.sha256(reference_bytes).hexdigest() == _REFERENCE_SHA256
    reference = json.loads(reference_bytes)
    input_bytes = _INPUT_PATH.read_bytes()
    assert hashlib.sha256(input_bytes).hexdigest() == reference["input"]["sha256"]

    input_payload = json.loads(input_bytes)
    assert len(input_payload["bars"]) == reference["input"]["bar_count"]
    assert input_payload["product"] == reference["input"]["product"]
    assert input_payload["source"] == reference["input"]["source"]
    assert input_payload["granularity_seconds"] == reference["input"]["granularity_seconds"]

    parameters = reference["parameters"]
    numeric_rows, encoded_rows = _calculate_platform_outputs(
        input_payload["bars"],
        vortex_period=parameters["vortex_period"],
        sma_period=parameters["sma_period"],
        atr_period=parameters["atr_period"],
    )

    output_contract = reference["output_contract"]
    assert len(encoded_rows) == output_contract["common_output_count"]
    assert encoded_rows[0][0] == output_contract["first_common_ready_bar_index"]
    assert encoded_rows[-1][0] == output_contract["last_bar_index"]
    assert _json_sha256(encoded_rows) == reference["full_output_sha256"]

    for series_index, series_name in enumerate(output_contract["row_fields"][1:], start=1):
        encoded_series = [[row[0], row[series_index]] for row in encoded_rows]
        assert _json_sha256(encoded_series) == reference["series_sha256"][series_name]

    rows_by_index = {row[0]: row[1:] for row in numeric_rows}
    for checkpoint in reference["checkpoints"]:
        bar_index, *expected = checkpoint
        assert rows_by_index[bar_index] == pytest.approx(expected, rel=0.0, abs=0.0)
