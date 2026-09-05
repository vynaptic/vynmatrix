from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lib_data.bars import Bar
from lib_data.consolidation import BarConsolidator


def test_bar_consolidator_emits_at_period_close() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append)

    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for offset in range(15):
        ts = start + timedelta(minutes=offset)
        consolidator.update(
            Bar(
                symbol="BTCUSD",
                timestamp=ts,
                open=100.0 + offset,
                high=101.0 + offset,
                low=99.0 + offset,
                close=100.5 + offset,
                volume=1.0,
                timeframe="1m",
                source="coinbase_live",
            )
        )

    assert len(emitted) == 1
    assert emitted[0].timestamp == datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
    assert emitted[0].timeframe == "15m"


# --- IND-2: incomplete-period coverage ----------------------------------------


def _minute(ts: datetime) -> Bar:
    return Bar(
        symbol="BTCUSD",
        timestamp=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
        timeframe="1m",
        source="coinbase_live",
    )


def test_complete_period_is_full_coverage() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for offset in range(15):
        consolidator.update(_minute(start + timedelta(minutes=offset)))
    assert len(emitted) == 1
    assert emitted[0].metadata["constituent_bars"] == 15
    assert emitted[0].metadata["coverage"] == 1.0
    assert emitted[0].metadata["complete"] is True


def test_consolidated_bar_retains_exact_last_constituent_provenance() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for offset in range(15):
        bar = _minute(start + timedelta(minutes=offset))
        bar.metadata = {
            "price_id": 1000 + offset,
            "content_revision": 3,
        }
        consolidator.update(bar)

    assert len(emitted) == 1
    assert emitted[0].metadata["price_id"] == 1014
    assert emitted[0].metadata["content_revision"] == 3
    assert (
        emitted[0].metadata["source_price_ts"]
        == datetime(2026, 1, 1, 9, 14, tzinfo=UTC).isoformat()
    )
    assert emitted[0].metadata["source_timeframe"] == "1m"


def test_gappy_period_is_flagged_incomplete_but_still_emitted_by_default() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    # Only 5 of 15 minutes for the 09:00 period, then a bar in the next period.
    for offset in range(5):
        consolidator.update(_minute(start + timedelta(minutes=offset)))
    consolidator.update(_minute(datetime(2026, 1, 1, 9, 15, tzinfo=UTC)))
    assert len(emitted) == 1  # default min_coverage=0.0 still emits
    assert emitted[0].metadata["constituent_bars"] == 5
    assert emitted[0].metadata["coverage"] == round(5 / 15, 4)
    assert emitted[0].metadata["complete"] is False


def test_min_coverage_suppresses_severely_gappy_period() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append, min_coverage=0.6)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for offset in range(5):  # 5/15 = 0.33 < 0.6 -> dropped
        consolidator.update(_minute(start + timedelta(minutes=offset)))
    result = consolidator.update(_minute(datetime(2026, 1, 1, 9, 15, tzinfo=UTC)))
    assert result is None  # the under-covered 09:00 period is suppressed
    assert emitted == []


def test_min_coverage_keeps_near_complete_period() -> None:
    emitted: list[Bar] = []
    consolidator = BarConsolidator(period_minutes=15, on_bar=emitted.append, min_coverage=0.6)
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    for offset in range(14):  # 14/15 = 0.93 >= 0.6 -> kept
        consolidator.update(_minute(start + timedelta(minutes=offset)))
    consolidator.update(_minute(datetime(2026, 1, 1, 9, 15, tzinfo=UTC)))
    assert len(emitted) == 1
    assert emitted[0].metadata["complete"] is False


def test_min_coverage_out_of_range_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="min_coverage"):
        BarConsolidator(period_minutes=15, min_coverage=1.5)
