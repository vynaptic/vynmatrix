from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from validation_helpers import load_market_fixture

from dev_cli.validation.providers.daily_reconciliation import (
    audit_minute_daily_reconciliation,
)
from lib_data.bars import Bar

_ROOT = Path(__file__).parents[4]
_MINUTE_FIXTURE = _ROOT / "tests/fixtures/market_data/coinbase_btcusd_1m_2026-06-10.json"
_DAILY_FIXTURE = _ROOT / "tests/fixtures/market_data/coinbase_btcusdc_1d_2026-06-10.json"


def _real_coinbase_bars() -> tuple[list[Bar], list[Bar]]:
    minute_fixture = load_market_fixture(_MINUTE_FIXTURE)
    daily_fixture = load_market_fixture(_DAILY_FIXTURE)
    minute_bars = [
        Bar(
            symbol="BTC-USDC",
            timestamp=datetime.fromtimestamp(row["ts"], tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            timeframe="1m",
            source=str(minute_fixture["source"]),
        )
        for row in minute_fixture["bars"]
        if row["ts"] < daily_fixture["bar"]["start"] + 86400
    ]
    row = daily_fixture["bar"]
    daily_bars = [
        Bar(
            symbol="BTC-USDC",
            timestamp=datetime.fromtimestamp(row["start"], tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            timeframe="1d",
            source=str(daily_fixture["source"]),
        )
    ]
    return minute_bars, daily_bars


def test_real_coinbase_parity_reports_provider_volume_delta_deterministically() -> None:
    """The frozen Coinbase endpoints disagree on volume; the audit must expose it."""

    minute_bars, daily_bars = _real_coinbase_bars()
    first = audit_minute_daily_reconciliation(minute_bars, daily_bars, price_tick=0.01)
    second = audit_minute_daily_reconciliation(minute_bars, daily_bars, price_tick=0.01)

    assert first.to_dict() == second.to_dict()
    assert first.is_match is False
    assert first.matched_bars == 0
    assert first.mismatched_bars == 1
    comparison = first.bars[0]
    assert comparison.close_timestamp == datetime(2026, 6, 11, tzinfo=UTC)
    assert comparison.provider_start_timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert comparison.status == "field_mismatch"
    assert comparison.constituent_bars == 1440
    assert comparison.coverage == 1.0
    deltas = {delta.field: delta for delta in comparison.field_deltas}
    assert all(deltas[field].within_tolerance for field in ("open", "high", "low", "close"))
    assert deltas["volume"].absolute_delta == pytest.approx(13.30682016)
    assert deltas["volume"].tolerance == pytest.approx(0.00904095804487)
    assert deltas["volume"].within_tolerance is False


def test_real_coinbase_undercovered_day_is_reported_before_field_acceptance() -> None:
    minute_bars, daily_bars = _real_coinbase_bars()
    # Remove 100 real constituent candles without changing any observed price.
    undercovered = minute_bars[:700] + minute_bars[800:]

    audit = audit_minute_daily_reconciliation(undercovered, daily_bars, price_tick=0.01)

    assert audit.is_match is False
    assert audit.bars[0].status == "insufficient_coverage"
    assert audit.bars[0].constituent_bars == 1340
    assert audit.bars[0].coverage == round(1340 / 1440, 4)


def test_missing_provider_day_is_reported_by_close_timestamp() -> None:
    minute_bars, _ = _real_coinbase_bars()

    audit = audit_minute_daily_reconciliation(minute_bars, [], price_tick=0.01)

    assert audit.bars[0].status == "missing_provider"
    assert audit.bars[0].close_timestamp == datetime(2026, 6, 11, tzinfo=UTC)


def test_missing_consolidated_day_is_reported_from_provider_start() -> None:
    _, daily_bars = _real_coinbase_bars()

    audit = audit_minute_daily_reconciliation([], daily_bars, price_tick=0.01)

    assert audit.bars[0].status == "missing_consolidated"
    assert audit.bars[0].provider_start_timestamp == datetime(2026, 6, 10, tzinfo=UTC)
    assert audit.bars[0].close_timestamp == datetime(2026, 6, 11, tzinfo=UTC)
