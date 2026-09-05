"""Shared market-data timing and summary models used by venue adapters.

``CandleRow`` and ``normalize_product_symbol`` moved to ``lib_data.market_data``
(the neutral data layer) so application/scoring code no longer imports them from
this adapter layer; import them from ``lib_data`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

GRANULARITY_TO_TIMEFRAME: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "SIX_HOUR": "6h",
    "ONE_DAY": "1d",
}

# Period length in seconds, used to detect the still-forming candle (its period
# end is in the future) so it is not ingested before it finalizes.
GRANULARITY_TO_SECONDS: dict[str, int] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


@dataclass
class IngestionSummary:
    """Aggregated result of an ingestion cycle."""

    products_processed: int = 0
    candles_received: int = 0
    rows_upserted: int = 0


def validate_aware_range(
    start_time: datetime,
    end_time: datetime,
    *,
    provider_name: str,
) -> tuple[datetime, datetime]:
    """Return an ordered UTC range, rejecting host-timezone interpretation."""

    if start_time.tzinfo is None or start_time.utcoffset() is None:
        msg = f"{provider_name} start_time must be timezone-aware"
        raise ValueError(msg)
    if end_time.tzinfo is None or end_time.utcoffset() is None:
        msg = f"{provider_name} end_time must be timezone-aware"
        raise ValueError(msg)
    start_utc = start_time.astimezone(UTC)
    end_utc = end_time.astimezone(UTC)
    if start_utc >= end_utc:
        msg = f"{provider_name} candle range requires start_time < end_time"
        raise ValueError(msg)
    return start_utc, end_utc
