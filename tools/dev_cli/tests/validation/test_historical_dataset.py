"""Deterministic historical-dataset quality and digest contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dev_cli.validation.historical_dataset import (
    DatasetValidationError,
    audit_bars,
    validate_bars,
)
from lib_data.bars import Bar

_START = datetime(2025, 1, 1, tzinfo=UTC)


def _bar(index: int, *, close: float = 100.0) -> Bar:
    return Bar(
        symbol="BTCUSDC",
        timestamp=_START + timedelta(days=index),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=10.0,
        timeframe="1d",
        source="coinbase_validation_v1",
    )


def test_complete_dataset_has_stable_digest_and_full_coverage() -> None:
    bars = [_bar(0), _bar(1, close=101.0), _bar(2, close=99.0)]
    kwargs = {
        "symbol": "BTCUSDC",
        "source": "coinbase_validation_v1",
        "timeframe": "1d",
        "start": _START,
        "end": _START + timedelta(days=3),
    }

    first = validate_bars(bars, **kwargs)
    second = validate_bars(list(reversed(bars)), **kwargs)

    assert first.coverage == 1.0
    assert first.expected_rows == 3
    assert first.ohlcv_sha256 == second.ohlcv_sha256


def test_changed_ohlcv_changes_content_digest() -> None:
    original = [_bar(0), _bar(1)]
    changed = [_bar(0), _bar(1, close=101.0)]
    kwargs = {
        "symbol": "BTCUSDC",
        "source": "coinbase_validation_v1",
        "timeframe": "1d",
        "start": _START,
        "end": _START + timedelta(days=2),
    }

    assert audit_bars(original, **kwargs).ohlcv_sha256 != audit_bars(changed, **kwargs).ohlcv_sha256


def test_missing_bar_is_reported_and_fails_full_coverage_gate() -> None:
    bars = [_bar(0), _bar(2)]
    kwargs = {
        "symbol": "BTCUSDC",
        "source": "coinbase_validation_v1",
        "timeframe": "1d",
        "start": _START,
        "end": _START + timedelta(days=3),
    }

    with pytest.raises(DatasetValidationError) as exc_info:
        validate_bars(bars, **kwargs)

    assert exc_info.value.audit.coverage == pytest.approx(2 / 3)
    assert exc_info.value.audit.missing_timestamps == 1
    assert exc_info.value.audit.missing_timestamp_sample == (_START + timedelta(days=1),)


def test_duplicates_malformed_rows_and_wrong_lineage_fail_closed() -> None:
    malformed = _bar(1)
    malformed.high = 98.0
    malformed.source = "coinbase_live"
    bars = [_bar(0), _bar(0), malformed]

    with pytest.raises(DatasetValidationError) as exc_info:
        validate_bars(
            bars,
            symbol="BTCUSDC",
            source="coinbase_validation_v1",
            timeframe="1d",
            start=_START,
            end=_START + timedelta(days=2),
            minimum_coverage=0.5,
        )

    audit = exc_info.value.audit
    assert audit.duplicate_timestamps == 1
    assert audit.malformed_rows == 1


def test_off_grid_timestamp_is_unexpected_and_cannot_pass_partial_coverage_gate() -> None:
    off_grid = _bar(0)
    off_grid.timestamp += timedelta(hours=12)
    kwargs = {
        "symbol": "BTCUSDC",
        "source": "coinbase_validation_v1",
        "timeframe": "1d",
        "start": _START,
        "end": _START + timedelta(days=2),
        "minimum_coverage": 0.0,
    }

    with pytest.raises(DatasetValidationError) as exc_info:
        validate_bars([off_grid], **kwargs)

    audit = exc_info.value.audit
    assert audit.unexpected_timestamps == 1
    assert audit.unique_in_range_rows == 0
    assert audit.missing_timestamps == 2
    assert audit.coverage == 0.0


def test_naive_bar_timestamp_is_audited_and_digest_is_order_independent() -> None:
    naive = _bar(0)
    naive.timestamp = naive.timestamp.replace(tzinfo=None)
    valid = _bar(1)
    kwargs = {
        "symbol": "BTCUSDC",
        "source": "coinbase_validation_v1",
        "timeframe": "1d",
        "start": _START,
        "end": _START + timedelta(days=2),
    }

    first = audit_bars([naive, valid], **kwargs)
    second = audit_bars([valid, naive], **kwargs)

    assert first.malformed_rows == 1
    assert first.coverage == 0.5
    assert first.ohlcv_sha256 == second.ohlcv_sha256
    with pytest.raises(DatasetValidationError) as exc_info:
        validate_bars([naive, valid], **kwargs, minimum_coverage=0.5)
    assert exc_info.value.audit.ohlcv_sha256 == first.ohlcv_sha256
