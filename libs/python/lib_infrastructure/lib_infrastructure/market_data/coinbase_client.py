"""Shared Coinbase candle-fetch client.

The canonical ``market_data_ingestor`` uses this client for HTTP fetch and row
normalization. Feedback consumes the persisted, provenance-matched candles.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from lib_common.logging import get_logger
from lib_data.market_data import CandleRow
from lib_infrastructure.brokers.adapters.coinbase_auth import build_coinbase_auth_headers
from lib_infrastructure.market_data.models import (
    GRANULARITY_TO_TIMEFRAME,
    validate_aware_range,
)
from lib_infrastructure.market_data.ports import IMarketDataProvider

logger = get_logger(__name__)


class CoinbaseMarketDataError(RuntimeError):
    """Raised when Coinbase returns a malformed market-data response."""


class CoinbaseCandleClient(IMarketDataProvider):
    """Stateless Coinbase candle fetcher with row normalisation.

    Responsibilities (shared):
      - HTTP fetch from the authenticated Advanced Trade candles endpoint when
        credentials are configured, otherwise from the documented public
        ``/api/v3/brokerage/market/products/{id}/candles`` endpoint
      - Raw candle → :class:`CandleRow` normalisation
      - Product-symbol → canonical-symbol mapping

    Does **not** own DB upsert or scheduling — those belong in the
    service layer (``PriceIngestionService``) or orchestration app.
    """

    BASE_URL = "https://api.coinbase.com"
    source = "coinbase_live"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if bool(api_key) != bool(api_secret):
            msg = "Coinbase candle access requires both api_key and api_secret, or neither"
            raise ValueError(msg)
        self._api_key = api_key
        self._api_secret = api_secret
        self._authenticated = bool(api_key and api_secret)
        self._client = client or httpx.Client(base_url=self.BASE_URL, timeout=20.0)
        self._request_host = httpx.URL(self.BASE_URL).host

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
        """IMarketDataProvider entrypoint: fetch + normalize in one call."""
        del broker_instrument_type
        normalized_granularity = granularity.strip().upper()
        try:
            timeframe = GRANULARITY_TO_TIMEFRAME[normalized_granularity]
        except KeyError as exc:
            msg = f"Unsupported Coinbase granularity: {granularity!r}"
            raise ValueError(msg) from exc
        raw = self.fetch_candles(
            product_id=product_id,
            start_time=start_time,
            end_time=end_time,
            granularity=normalized_granularity,
        )
        return self.normalize_candles(
            instr_id=instr_id,
            product_id=product_id,
            candles=raw,
            timeframe=timeframe,
        )

    def fetch_candles(
        self,
        *,
        product_id: str,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "ONE_MINUTE",
    ) -> list[dict[str, Any]]:
        """Fetch raw candle dicts from Coinbase for a single product.

        Args:
            product_id: Coinbase product ID (e.g. "BTC-USD")
            start_time: Inclusive start (UTC)
            end_time: Inclusive end (UTC)
            granularity: Coinbase granularity enum string

        Returns:
            List of raw candle dicts as returned by the Coinbase API.
        """
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="Coinbase",
        )
        path_prefix = (
            "/api/v3/brokerage/products"
            if self._authenticated
            else ("/api/v3/brokerage/market/products")
        )
        path = f"{path_prefix}/{product_id}/candles"
        params = {
            "start": str(int(start_time.timestamp())),
            "end": str(int(end_time.timestamp())),
            "granularity": granularity.strip().upper(),
            # Coinbase treats aligned start/end bounds inclusively. A 300-minute
            # window can therefore contain 301 bars; use the documented public
            # maximum so the oldest boundary candle is not truncated.
            "limit": "350",
        }
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
        if self._authenticated:
            assert self._api_key is not None
            assert self._api_secret is not None
            headers.update(
                build_coinbase_auth_headers(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    method="GET",
                    host=self._request_host,
                    path=path,
                )
            )
        response = self._client.get(path, params=params, headers=headers)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            msg = "Coinbase candle response was not valid JSON"
            raise CoinbaseMarketDataError(msg) from exc
        if not isinstance(payload, dict) or "candles" not in payload:
            msg = "Coinbase candle response omitted the candles field"
            raise CoinbaseMarketDataError(msg)
        candles = payload["candles"]
        if not isinstance(candles, list):
            msg = "Coinbase candle response candles field was not a list"
            raise CoinbaseMarketDataError(msg)
        return list(candles)

    @staticmethod
    def normalize_candles(
        *,
        instr_id: int,
        product_id: str,
        candles: Iterable[dict[str, Any]],
        timeframe: str,
    ) -> list[CandleRow]:
        """Convert raw Coinbase candle dicts to :class:`CandleRow` objects.

        Malformed candles are logged and skipped.

        Args:
            instr_id: DB instrument ID for the product
            product_id: Original Coinbase product ID (for logging)
            candles: Raw candle dicts from :meth:`fetch_candles`
            timeframe: Normalised timeframe string (e.g. "1m", "15m")

        Returns:
            Sorted list of CandleRow (ascending by timestamp).
        """
        rows: list[CandleRow] = []
        for candle in candles:
            try:
                ts = datetime.fromtimestamp(int(candle["start"]), tz=UTC).replace(tzinfo=None)
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=ts,
                        timeframe=timeframe,
                        open=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=float(candle["volume"]),
                        source=CoinbaseCandleClient.source,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipping malformed Coinbase candle",
                    product_id=product_id,
                    candle=candle,
                )
        rows.sort(key=lambda r: r.ts)
        return rows
