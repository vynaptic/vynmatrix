"""Zerodha Kite Connect historical-candle client.

Kite historical data is keyed by the immutable numeric ``instrument_token``.
The client deliberately requires that token as ``product_id`` rather than
guessing from a trading symbol that may be ambiguous across exchanges.

Kite access tokens are interactive, account-scoped, and expire daily. This
adapter never attempts to manufacture or silently refresh a session: an
operator-provided current token is required and an expired token fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from lib_common.logging import get_logger
from lib_data.market_data import CandleRow

from .models import validate_aware_range
from .ports import IMarketDataProvider

logger = get_logger(__name__)

_GRANULARITY_TO_INTERVAL: dict[str, str] = {
    "ONE_MINUTE": "minute",
    "FIVE_MINUTE": "5minute",
    "FIFTEEN_MINUTE": "15minute",
    "THIRTY_MINUTE": "30minute",
    "ONE_HOUR": "60minute",
    "ONE_DAY": "day",
}
_GRANULARITY_TO_TIMEFRAME: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "ONE_DAY": "1d",
}
_MIN_CANDLE_FIELDS = 6
_INDIA_TIME_ZONE = ZoneInfo("Asia/Kolkata")


class ZerodhaCandleClient(IMarketDataProvider):
    """Authenticated historical-candle provider for Zerodha instruments."""

    BASE_URL = "https://api.kite.trade"
    source = "zerodha"

    def __init__(
        self,
        *,
        api_key: str | None,
        access_token: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        normalized_key = str(api_key or "").strip()
        normalized_token = str(access_token or "").strip()
        if not normalized_key or not normalized_token:
            msg = "Zerodha market data requires both api_key and a current access_token"
            raise ValueError(msg)
        self._client = client or httpx.Client(base_url=self.BASE_URL, timeout=20.0)
        self._headers = {
            "Authorization": f"token {normalized_key}:{normalized_token}",
            "X-Kite-Version": "3",
        }

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
        instrument_token = product_id.strip()
        if not instrument_token.isdecimal() or int(instrument_token) <= 0:
            msg = f"Zerodha product_id must be the numeric instrument_token; got {product_id!r}"
            raise ValueError(msg)
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="Zerodha",
        )
        normalized_granularity = granularity.strip().upper()
        try:
            interval = _GRANULARITY_TO_INTERVAL[normalized_granularity]
            timeframe = _GRANULARITY_TO_TIMEFRAME[normalized_granularity]
        except KeyError as exc:
            msg = f"Unsupported Zerodha granularity: {granularity!r}"
            raise ValueError(msg) from exc

        response = self._client.get(
            f"/instruments/historical/{instrument_token}/{interval}",
            params={
                "from": _format_kite_time(start_time),
                "to": _format_kite_time(end_time),
                "continuous": "0",
                "oi": "0",
            },
            headers=self._headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success":
            msg = f"Zerodha candle response reported failure for token {instrument_token!r}"
            raise RuntimeError(msg)
        data = payload.get("data")
        candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(candles, list):
            msg = f"Zerodha candle response has invalid data for token {instrument_token!r}"
            raise TypeError(msg)
        return self._normalize(
            candles,
            instr_id=instr_id,
            product_id=instrument_token,
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
            if not isinstance(candle, list) or len(candle) < _MIN_CANDLE_FIELDS:
                logger.warning(
                    "Skipping malformed Zerodha candle",
                    product_id=product_id,
                    index=index,
                )
                continue
            try:
                observed_at = _parse_kite_timestamp(candle[0])
                observed_naive = observed_at.astimezone(UTC).replace(tzinfo=None)
                if not start_naive <= observed_naive < end_naive:
                    continue
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=observed_naive,
                        timeframe=timeframe,
                        open=float(candle[1]),
                        high=float(candle[2]),
                        low=float(candle[3]),
                        close=float(candle[4]),
                        volume=float(candle[5]),
                        source=self.source,
                    )
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping malformed Zerodha candle",
                    product_id=product_id,
                    index=index,
                )
        rows.sort(key=lambda row: row.ts)
        return rows


def _format_kite_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "Zerodha candle range boundaries must be timezone-aware"
        raise ValueError(msg)
    # Kite's wire contract omits an offset and its historical examples use
    # Indian-exchange wall time. Convert explicitly rather than letting the
    # service host's local timezone shift the requested window.
    return value.astimezone(_INDIA_TIME_ZONE).strftime("%Y-%m-%d %H:%M:%S")


def _parse_kite_timestamp(value: object) -> datetime:
    observed_at = datetime.fromisoformat(str(value))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        msg = "Zerodha candle timestamp must include an offset"
        raise ValueError(msg)
    return observed_at
