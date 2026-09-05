"""MD-5: malformed candles are rejected before upsert so they cannot corrupt
indicators (high<low, non-positive prices, negative volume, etc.)."""

from __future__ import annotations

from datetime import UTC, datetime

from lib_application.services.price_ingestion_service import _is_valid_candle
from lib_data.market_data import CandleRow

TS = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _candle(*, o=100.0, h=101.0, low=99.0, c=100.5, v=5.0) -> CandleRow:
    return CandleRow(
        instr_id=1, ts=TS, timeframe="1m", open=o, high=h, low=low, close=c, volume=v, source="x"
    )


def test_well_formed_candle_is_valid() -> None:
    assert _is_valid_candle(_candle()) is True


def test_zero_volume_is_valid() -> None:
    # A period with no trades is legitimate.
    assert _is_valid_candle(_candle(v=0.0)) is True


def test_high_below_other_legs_is_rejected() -> None:
    assert _is_valid_candle(_candle(h=100.0, o=100.5)) is False  # high < open


def test_low_above_other_legs_is_rejected() -> None:
    assert _is_valid_candle(_candle(low=101.0, c=100.5)) is False  # low > close


def test_non_positive_price_is_rejected() -> None:
    assert _is_valid_candle(_candle(c=0.0)) is False
    assert _is_valid_candle(_candle(o=-1.0)) is False


def test_negative_volume_is_rejected() -> None:
    assert _is_valid_candle(_candle(v=-1.0)) is False
