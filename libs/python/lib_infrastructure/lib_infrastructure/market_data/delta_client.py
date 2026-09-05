"""Delta Exchange public historical-candle client.

Delta exposes closed historical candles without authentication. Global and
India deployments use separate API hosts and therefore separate immutable
provenance labels; callers must select the correct source explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from lib_common.logging import get_logger
from lib_data.market_data import CandleRow

from .models import validate_aware_range
from .ports import IMarketDataProvider

logger = get_logger(__name__)

_GRANULARITY_TO_RESOLUTION: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "SIX_HOUR": "6h",
    "ONE_DAY": "1d",
}
_GRANULARITY_TO_TIMEFRAME: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "SIX_HOUR": "6h",
    "ONE_DAY": "1d",
}


class DeltaCandleClient(IMarketDataProvider):
    """Public candle provider for Delta global or Delta Exchange India."""

    GLOBAL_BASE_URL = "https://api.delta.exchange"
    INDIA_BASE_URL = "https://api.india.delta.exchange"
    source = "delta"

    def __init__(
        self,
        *,
        india: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.source = "delta_india" if india else "delta"
        base_url = self.INDIA_BASE_URL if india else self.GLOBAL_BASE_URL
        self._client = client or httpx.Client(base_url=base_url, timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def fetch_candle_rows(
        self,
        *,
        product_id: str,
        instr_id: int,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "ONE_MINUTE",
        broker_instrument_type: str | None = None,
    ) -> list[CandleRow]:
        del broker_instrument_type
        product_id = product_id.strip()
        if not product_id:
            msg = "Delta product_id must be non-empty"
            raise ValueError(msg)
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="Delta",
        )
        normalized_granularity = granularity.strip().upper()
        try:
            resolution = _GRANULARITY_TO_RESOLUTION[normalized_granularity]
            timeframe = _GRANULARITY_TO_TIMEFRAME[normalized_granularity]
        except KeyError as exc:
            msg = f"Unsupported Delta granularity: {granularity!r}"
            raise ValueError(msg) from exc

        response = self._client.get(
            "/v2/history/candles",
            params={
                "resolution": resolution,
                "symbol": product_id,
                "start": int(start_time.timestamp()),
                "end": int(end_time.timestamp()),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            msg = f"Delta candle response reported failure for {product_id!r}"
            raise RuntimeError(msg)
        candles = payload.get("result")
        if not isinstance(candles, list):
            msg = f"Delta candle response has invalid result for {product_id!r}"
            raise TypeError(msg)
        return self._normalize(
            candles,
            instr_id=instr_id,
            product_id=product_id,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
        )

    def _normalize(
        self,
        candles: list[Any],
        *,
        instr_id: int,
        product_id: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CandleRow]:
        start_naive = start_time.astimezone(UTC).replace(tzinfo=None)
        end_naive = end_time.astimezone(UTC).replace(tzinfo=None)
        rows: list[CandleRow] = []
        for index, candle in enumerate(candles):
            if not isinstance(candle, dict):
                logger.warning(
                    "Skipping malformed Delta candle",
                    product_id=product_id,
                    index=index,
                )
                continue
            try:
                ts = datetime.fromtimestamp(int(candle["time"]), tz=UTC).replace(tzinfo=None)
                if not start_naive <= ts < end_naive:
                    continue
                volume = candle.get("volume")
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=ts,
                        timeframe=timeframe,
                        open=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=None if volume is None else float(volume),
                        source=self.source,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipping malformed Delta candle",
                    product_id=product_id,
                    index=index,
                )
        rows.sort(key=lambda row: row.ts)
        return rows
