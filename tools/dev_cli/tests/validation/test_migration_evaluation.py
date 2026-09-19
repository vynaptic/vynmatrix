"""Research orchestration guards; deterministic fixtures stay at unit boundaries."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from dev_cli.validation.evidence import evidence_sha256
from dev_cli.validation.migration_evaluation import (
    aligned_correlation,
    evaluation_windows,
    load_daily_bars,
)


def _payload():
    rows = [
        {
            "start": str(1577836800 + i * 86400),
            "open": "10",
            "high": "11",
            "low": "9",
            "close": "10",
            "volume": "1",
        }
        for i in range(2)
    ]
    return {
        "source": "coinbase_live",
        "bars": rows,
        "bars_sha256": evidence_sha256(rows),
        "request": {
            "requested_product": "BTC-USDC",
            "expected_canonical_book": "BTC-USD",
            "start": "2020-01-01T00:00:00+00:00",
            "end_exclusive": "2020-01-03T00:00:00+00:00",
            "granularity": "ONE_DAY",
            "timestamp_semantics": "period_start",
        },
        "product_metadata": {
            "requested_product_id": "BTC-USDC",
            "product_id": "BTC-USDC",
            "alias": "BTC-USD",
            "base_currency_id": "BTC",
            "quote_currency_id": "USDC",
        },
    }


def test_recorded_daily_input_retains_period_start_and_explicit_book_lineage():
    bars, audit = load_daily_bars(_payload())
    assert len(bars) == audit.observed_rows == 2
    assert audit.coverage == 1
    assert bars[0].timestamp == datetime(2020, 1, 1, tzinfo=UTC)
    assert bars[0].metadata["timestamp_semantics"] == "period_start"
    assert bars[0].metadata["canonical_product"] == "BTC-USD"
    assert bars[0].metadata["quote_currency"] == "USDC"


@pytest.mark.parametrize("corruption", ["hash", "gap", "alias", "ohlcv"])
def test_daily_data_cannot_silently_change_or_fill_missing_history(corruption):
    payload = _payload()
    if corruption == "hash":
        payload["bars"][0]["close"] = "10.5"
    elif corruption == "gap":
        payload["bars"].pop()
        payload["bars_sha256"] = evidence_sha256(payload["bars"])
    elif corruption == "alias":
        payload["product_metadata"]["alias"] = "ETH-USD"
    else:
        payload["bars"][0]["high"] = "8"
        payload["bars_sha256"] = evidence_sha256(payload["bars"])
    with pytest.raises(ValueError, match="Frozen"):
        load_daily_bars(payload)


def test_frozen_windows_use_completed_close_dates_without_overlap():
    windows = evaluation_windows()
    assert windows[0]["start"] == "2020-01-02T00:00:00+00:00"
    assert windows[-1]["end"] == "2026-01-02T00:00:00+00:00"
    assert [row["name"] for row in windows] == [
        "is_2020_2021",
        "oos_2022",
        "oos_2023",
        "oos_2024",
        "oos_2025",
    ]
    assert all(left["end"] == right["start"] for left, right in pairwise(windows))


def test_overlap_rejects_misaligned_grids_and_reports_undefined_constant_series():
    start = datetime(2022, 1, 2, tzinfo=UTC)
    first = [(start + timedelta(days=i), value) for i, value in enumerate([0.01, -0.02, 0.03])]
    assert aligned_correlation(first, first) == pytest.approx(1)
    assert aligned_correlation(first, [(ts, 0.0) for ts, _ in first]) is None
    with pytest.raises(ValueError, match="grid"):
        aligned_correlation(first, first[1:])


def test_terminal_benchmark_distinguishes_closed_statistics_from_whole_window():
    from dev_cli.validation.backtest.engine import BacktestConfig
    from dev_cli.validation.backtest.execution import ExecutionCostModel, FillPolicy
    from dev_cli.validation.benchmarks import BenchmarkDefinition, run_consolidated_benchmark
    from dev_cli.validation.migration_evaluation import _report_payload

    bars, _audit = load_daily_bars(_payload())
    config = BacktestConfig(
        symbol="BTC-USDC",
        consolidation_minutes=1440,
        execution_costs=ExecutionCostModel(commission_bps=0),
        fill_policy=FillPolicy.reference_v2(),
    )
    report = run_consolidated_benchmark(BenchmarkDefinition.buy_and_hold(), bars, config).report
    payload = _report_payload(report)
    assert payload["terminal_position"] is not None
    assert payload["turnover_notional"] == pytest.approx(95000)
    assert payload["turnover_ratio"] == 0
    assert payload["average_holding_period_days"] == 0
    assert "turnover_ratio" in payload["metric_scopes"]["closed_trades_only"]
    assert "average_holding_period_days" in payload["metric_scopes"]["closed_trades_only"]
    assert "turnover_notional" in payload["metric_scopes"]["includes_terminal_position"]
