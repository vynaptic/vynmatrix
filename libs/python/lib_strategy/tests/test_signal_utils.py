from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import ensure_utc, extract_price_provenance


def test_ensure_utc_attaches_utc_to_naive_timestamp() -> None:
    timestamp = datetime(2030, 1, 1, 12, 30)

    assert ensure_utc(timestamp) == timestamp.replace(tzinfo=UTC)


def test_ensure_utc_converts_offset_timestamp() -> None:
    timestamp = datetime(2030, 1, 1, 3, 30, tzinfo=timezone(timedelta(hours=2)))

    assert ensure_utc(timestamp) == datetime(2030, 1, 1, 1, 30, tzinfo=UTC)


def test_extract_price_provenance_prefers_canonical_keys() -> None:
    assert extract_price_provenance(
        {
            "price_source": " coinbase_live ",
            "source": "other",
            "price_timeframe": " 1m ",
            "timeframe": "1h",
        }
    ) == {
        "price_source": "coinbase_live",
        "price_timeframe": "1m",
    }


def test_extract_price_provenance_accepts_bar_aliases() -> None:
    assert extract_price_provenance(
        {
            "source": "coinbase_exchange_public",
            "timeframe": "1m",
        }
    ) == {
        "price_source": "coinbase_exchange_public",
        "price_timeframe": "1m",
    }


def test_extract_price_provenance_carries_complete_durable_identity() -> None:
    assert extract_price_provenance(
        {
            "source": "coinbase_live",
            "timeframe": "1m",
            "price_id": 41,
            "content_revision": 3,
            "source_price_ts": datetime(2030, 1, 1, 12, 30),
        }
    ) == {
        "price_source": "coinbase_live",
        "price_timeframe": "1m",
        "source_price_id": 41,
        "source_content_revision": 3,
        "source_price_ts": "2030-01-01T12:30:00+00:00",
    }


def test_extract_price_provenance_omits_partial_durable_identity() -> None:
    assert extract_price_provenance(
        {
            "source": "coinbase_live",
            "timeframe": "1m",
            "price_id": 41,
        }
    ) == {
        "price_source": "coinbase_live",
        "price_timeframe": "1m",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"price_source": "coinbase_live"},
        {"price_timeframe": "1m"},
        {"price_source": "", "price_timeframe": "1m"},
        {"price_source": 123, "price_timeframe": "1m"},
    ],
)
def test_extract_price_provenance_omits_incomplete_or_invalid_pairs(
    metadata: dict[str, object] | None,
) -> None:
    assert extract_price_provenance(metadata) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ETF", "etf"),
        ("INDEX", "index"),
        ("forex", "fx"),
        ("commodity", "commodities"),
    ],
)
def test_signal_normalizes_asset_class_at_the_domain_boundary(
    value: str,
    expected: str,
) -> None:
    signal = Signal(
        strategy_id="taxonomy_contract",
        strategy_type="indicator",
        symbol="INSTRUMENT",
        action=SignalAction.HOLD,
        confidence=1.0,
        asset_class=value,
    )

    assert signal.asset_class == expected
    assert signal.to_dict()["asset_class"] == expected


def test_signal_rejects_an_unknown_asset_class() -> None:
    with pytest.raises(ValueError, match="Unsupported asset_class"):
        Signal(
            strategy_id="taxonomy_contract",
            strategy_type="indicator",
            symbol="INSTRUMENT",
            action=SignalAction.HOLD,
            confidence=1.0,
            asset_class="stock",
        )
