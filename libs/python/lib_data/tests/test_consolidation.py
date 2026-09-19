from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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


# Checkpoint recovery must retain both OHLCV and the latest constituent identity.
@pytest.mark.parametrize("offsets", [[], list(range(5)), list(range(15)), [0, 1, 2, 29]])
@pytest.mark.parametrize("coverage", [0.0, 0.6, 1.0])
def test_json_checkpoint_continues_partial_completed_and_gap_buckets(offsets, coverage):
    import json

    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    uninterrupted = BarConsolidator(15, min_coverage=coverage)
    for offset in offsets:
        bar = _minute(start + timedelta(minutes=offset))
        bar.high += offset
        bar.volume = offset + 1
        bar.metadata = {
            "price_id": 1000 + offset,
            "content_revision": 3,
            "feed": {"venue": "coinbase"},
        }
        uninterrupted.update(bar)
    snapshot = json.loads(json.dumps(uninterrupted.snapshot_state(), allow_nan=False))
    emitted = []
    recovered = BarConsolidator(15, emitted.append, min_coverage=coverage)
    recovered.restore_state(snapshot)
    assert emitted == []
    assert recovered.snapshot_state() == snapshot
    for offset in range(30, 61):
        bar = _minute(start + timedelta(minutes=offset))
        bar.metadata = {"price_id": 1000 + offset, "content_revision": 4}
        before_count = len(emitted)
        expected = uninterrupted.update(bar)
        actual = recovered.update(bar)
        assert actual == expected
        assert len(emitted) - before_count == int(actual is not None)
        assert recovered.snapshot_state() == uninterrupted.snapshot_state()


@pytest.mark.parametrize(
    "corruption",
    ["period", "coverage", "nan", "metadata", "count", "timezone", "boundary", "duration"],
)
def test_bad_consolidation_checkpoint_cannot_mutate_a_working_bucket(corruption):
    import copy

    consolidator = BarConsolidator(15, min_coverage=0.6)
    consolidator.update(_minute(datetime(2026, 1, 1, 9, tzinfo=UTC)))
    before = consolidator.snapshot_state()
    snapshot = copy.deepcopy(before)
    if corruption == "period":
        snapshot["period_minutes"] = 30
    elif corruption == "coverage":
        snapshot["min_coverage"] = 0
    elif corruption == "nan":
        snapshot["working_bar"]["high"] = float("nan")
    elif corruption == "metadata":
        snapshot["working_bar"]["metadata"]["invalid"] = float("inf")
    elif corruption == "duration":
        snapshot["source_minutes"] = 15
    elif corruption == "count":
        snapshot["bar_count"] = True
    elif corruption == "timezone":
        snapshot["working_bar"]["timestamp"] = "2026-01-01T09:01:00"
    else:
        snapshot["current_period_end"] = "2026-01-01T09:30:00+00:00"
    with pytest.raises(ValueError, match="checkpoint"):
        consolidator.restore_state(snapshot)
    assert consolidator.snapshot_state() == before
