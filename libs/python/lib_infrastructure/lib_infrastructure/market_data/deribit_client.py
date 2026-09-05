"""Deribit candle client — crypto options / futures / perpetuals (multi-asset).

Implements :class:`IMarketDataProvider` over Deribit's public TradingView chart
endpoint (no auth required for market data), normalizing the columnar response
to canonical :class:`CandleRow` rows. Private/trading calls use the Deribit
broker adapter + ``DERIBIT_CLIENT_ID``/``DERIBIT_CLIENT_SECRET``; market data
here is public.

Use ``testnet=True`` for the testnet. The flag also selects the immutable
``deribit_testnet`` provenance label.
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

# Coinbase-style granularity -> Deribit chart resolution (minutes, or "1D").
_GRANULARITY_TO_RESOLUTION: dict[str, str] = {
    "ONE_MINUTE": "1",
    "FIVE_MINUTE": "5",
    "FIFTEEN_MINUTE": "15",
    "THIRTY_MINUTE": "30",
    "ONE_HOUR": "60",
    "TWO_HOUR": "120",
    "SIX_HOUR": "360",
    "ONE_DAY": "1D",
}
# Coinbase-style granularity -> canonical CandleRow.timeframe.
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

_DERIBIT_OK = "ok"


class DeribitCandleClient(IMarketDataProvider):
    """Public-market-data candle provider for Deribit instruments."""

    BASE_URL = "https://www.deribit.com"
    source = "deribit"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        testnet: bool = False,
    ) -> None:
        self.source = "deribit_testnet" if testnet else "deribit"
        selected_base_url = "https://test.deribit.com" if testnet else self.BASE_URL
        self._client = client or httpx.Client(
            base_url=selected_base_url,
            timeout=20.0,
        )

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
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="Deribit",
        )
        normalized_granularity = granularity.strip().upper()
        try:
            resolution = _GRANULARITY_TO_RESOLUTION[normalized_granularity]
            timeframe = _GRANULARITY_TO_TIMEFRAME[normalized_granularity]
        except KeyError as exc:
            msg = f"Unsupported Deribit granularity: {granularity!r}"
            raise ValueError(msg) from exc
        response = self._client.get(
            "/api/v2/public/get_tradingview_chart_data",
            params={
                "instrument_name": product_id,
                "start_timestamp": int(start_time.timestamp() * 1000),
                "end_timestamp": int(end_time.timestamp() * 1000),
                "resolution": resolution,
            },
        )
        response.raise_for_status()
        result = response.json().get("result", {})
        return self._normalize(
            result, instr_id=instr_id, product_id=product_id, timeframe=timeframe
        )

    def _normalize(
        self,
        result: dict[str, Any],
        *,
        instr_id: int,
        product_id: str,
        timeframe: str,
    ) -> list[CandleRow]:
        if result.get("status") != _DERIBIT_OK:
            logger.warning(
                "Deribit chart status not ok",
                product_id=product_id,
                status=result.get("status"),
            )
            return []
        ticks = result.get("ticks") or []
        rows: list[CandleRow] = []
        for i, tick_ms in enumerate(ticks):
            try:
                ts = datetime.fromtimestamp(int(tick_ms) / 1000, tz=UTC).replace(tzinfo=None)
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=ts,
                        timeframe=timeframe,
                        open=float(result["open"][i]),
                        high=float(result["high"][i]),
                        low=float(result["low"][i]),
                        close=float(result["close"][i]),
                        volume=float(result["volume"][i]),
                        source=self.source,
                    )
                )
            except (KeyError, IndexError, TypeError, ValueError):
                logger.warning("Skipping malformed Deribit candle", product_id=product_id, index=i)
        rows.sort(key=lambda r: r.ts)
        return rows
