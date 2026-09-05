"""Saxo OpenAPI chart client with environment-exact provenance.

Saxo identifies chart instruments by the pair ``(Uic, AssetType)``. Both values
must come from an explicit platform mapping; this adapter never searches by
symbol or chooses the first reference-data match.

The live and simulation endpoints are distinct and produce distinct source
labels. Saxo documents that simulation market data may be unavailable or
synthetic, so ``saxo_simulation`` must never be treated as live provenance.
Access-token refresh is intentionally outside this read adapter: callers inject
the current OAuth token and its expiry, and expired/near-expiry credentials fail
closed before a request is sent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from urllib.parse import quote

import httpx

from lib_common.logging import get_logger
from lib_data.market_data import CandleRow

from .models import validate_aware_range
from .ports import IMarketDataProvider

logger = get_logger(__name__)

_GRANULARITY_TO_HORIZON: dict[str, int] = {
    "ONE_MINUTE": 1,
    "FIVE_MINUTE": 5,
    "FIFTEEN_MINUTE": 15,
    "THIRTY_MINUTE": 30,
    "ONE_HOUR": 60,
    "TWO_HOUR": 120,
    "SIX_HOUR": 360,
    "ONE_DAY": 1440,
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
_MAX_SAMPLES = 1200
_EXPIRY_SAFETY_MARGIN = timedelta(seconds=30)


class SaxoTokenExpiredError(RuntimeError):
    """The configured Saxo OAuth token is expired or too close to expiry."""


class SaxoChartShapeError(RuntimeError):
    """The chart response cannot be represented as canonical last-trade OHLC."""


class SaxoDelayedMarketDataError(RuntimeError):
    """A live-labelled request returned delayed rather than live market data."""


class SaxoCandleClient(IMarketDataProvider):
    """Authenticated Saxo v3 chart provider for live or simulation data."""

    LIVE_BASE_URL = "https://gateway.saxobank.com/openapi"
    SIMULATION_BASE_URL = "https://gateway.saxobank.com/sim/openapi"

    def __init__(
        self,
        *,
        environment: str,
        access_token: str | None,
        access_token_expires_at: str | datetime | None,
        account_key: str | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"live", "simulation"}:
            msg = "Saxo market-data environment must be 'live' or 'simulation'"
            raise ValueError(msg)
        normalized_token = str(access_token or "").strip()
        if not normalized_token:
            msg = "Saxo market data requires an OAuth access_token"
            raise ValueError(msg)

        self.source = "saxo_live" if normalized_environment == "live" else "saxo_simulation"
        self._expires_at = _parse_expiry(access_token_expires_at)
        self._account_key = str(account_key or "").strip() or None
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {normalized_token}",
        }
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        base_url = (
            self.LIVE_BASE_URL if normalized_environment == "live" else self.SIMULATION_BASE_URL
        )
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
        uic_text = product_id.strip()
        if not uic_text.isdecimal() or int(uic_text) <= 0:
            msg = f"Saxo product_id must be a positive numeric UIC; got {product_id!r}"
            raise ValueError(msg)
        asset_type = str(broker_instrument_type or "").strip()
        if not asset_type:
            msg = "Saxo market data requires an explicit broker_instrument_type"
            raise ValueError(msg)
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="Saxo",
        )
        normalized_granularity = granularity.strip().upper()
        try:
            horizon = _GRANULARITY_TO_HORIZON[normalized_granularity]
            timeframe = _GRANULARITY_TO_TIMEFRAME[normalized_granularity]
        except KeyError as exc:
            msg = f"Unsupported Saxo granularity: {granularity!r}"
            raise ValueError(msg) from exc

        self._require_current_token()
        requested_samples = ceil((end_time - start_time).total_seconds() / (horizon * 60))
        params: dict[str, str | int | bool] = {
            "AssetType": asset_type,
            "Count": min(_MAX_SAMPLES, max(1, requested_samples + 1)),
            "ExtendedHoursEnabled": True,
            "FieldGroups": "Data",
            "Horizon": horizon,
            "Mode": "From",
            "Time": _format_saxo_time(start_time),
            "Uic": int(uic_text),
        }
        if self._account_key is not None:
            params["AccountKey"] = self._account_key

        response = self._client.get(
            "/chart/v3/charts",
            params=params,
            headers=self._headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = f"Saxo chart response is not an object for UIC {uic_text!r}"
            raise TypeError(msg)
        chart_info = payload.get("ChartInfo")
        if not isinstance(chart_info, dict):
            msg = f"Saxo chart response has invalid ChartInfo for UIC {uic_text!r}"
            raise TypeError(msg)
        if chart_info.get("Horizon") is None or int(chart_info["Horizon"]) != horizon:
            msg = f"Saxo chart horizon mismatch for UIC {uic_text!r}"
            raise SaxoChartShapeError(msg)
        delayed_by = chart_info.get("DelayedByMinutes")
        if delayed_by is not None:
            try:
                delayed_minutes = int(delayed_by)
            except (TypeError, ValueError) as exc:
                msg = f"Saxo chart delay metadata is invalid for UIC {uic_text!r}"
                raise SaxoChartShapeError(msg) from exc
            if delayed_minutes < 0:
                msg = f"Saxo chart delay metadata is invalid for UIC {uic_text!r}"
                raise SaxoChartShapeError(msg)
        else:
            delayed_minutes = None
        if self.source == "saxo_live" and delayed_minutes != 0:
            msg = (
                "Saxo live market-data source requires DelayedByMinutes=0; "
                f"got {delayed_by!r} for UIC {uic_text!r}"
            )
            raise SaxoDelayedMarketDataError(msg)
        samples = payload.get("Data")
        if not isinstance(samples, list):
            msg = f"Saxo chart response has invalid Data for UIC {uic_text!r}"
            raise TypeError(msg)
        return self._normalize(
            samples,
            instr_id=instr_id,
            product_id=uic_text,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
        )

    def fetch_trading_schedule(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> dict[str, Any]:
        """Return Saxo's official bounded instrument-session wire response."""
        uic_text = product_id.strip()
        if not uic_text.isdecimal() or int(uic_text) <= 0:
            msg = f"Saxo product_id must be a positive numeric UIC; got {product_id!r}"
            raise ValueError(msg)
        asset_type = str(broker_instrument_type or "").strip()
        if not asset_type or not asset_type.replace("_", "").isalnum():
            msg = "Saxo market calendar requires an explicit broker_instrument_type"
            raise ValueError(msg)
        self._require_current_token()
        response = self._client.get(
            f"/ref/v1/instruments/tradingschedule/{int(uic_text)}/{quote(asset_type, safe='')}",
            headers=self._headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = f"Saxo trading-schedule response is not an object for UIC {uic_text!r}"
            raise TypeError(msg)
        return payload

    def _require_current_token(self) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            msg = "Saxo credential clock must return a timezone-aware datetime"
            raise ValueError(msg)
        if now.astimezone(UTC) + _EXPIRY_SAFETY_MARGIN >= self._expires_at:
            msg = "Saxo OAuth access token is expired or expires within 30 seconds"
            raise SaxoTokenExpiredError(msg)

    def _normalize(
        self,
        samples: list[Any],
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
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                logger.warning(
                    "Skipping malformed Saxo chart sample",
                    product_id=product_id,
                    index=index,
                )
                continue
            if any(key.endswith(("Bid", "Ask")) for key in sample):
                msg = "Saxo bid/ask chart samples cannot be persisted as canonical last-trade OHLC"
                raise SaxoChartShapeError(msg)
            if not {"Open", "High", "Low", "Close"}.issubset(sample):
                logger.warning(
                    "Skipping Saxo chart sample without complete OHLC",
                    product_id=product_id,
                    index=index,
                )
                continue
            try:
                observed_at = _parse_saxo_time(sample["Time"])
                observed_naive = observed_at.astimezone(UTC).replace(tzinfo=None)
                if not start_naive <= observed_naive < end_naive:
                    continue
                volume = sample.get("Volume")
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=observed_naive,
                        timeframe=timeframe,
                        open=float(sample["Open"]),
                        high=float(sample["High"]),
                        low=float(sample["Low"]),
                        close=float(sample["Close"]),
                        volume=None if volume is None else float(volume),
                        source=self.source,
                    )
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping malformed Saxo chart sample",
                    product_id=product_id,
                    index=index,
                )
        rows.sort(key=lambda row: row.ts)
        return rows


def _parse_expiry(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value or "").strip()
        if not normalized:
            msg = "Saxo market data requires access_token_expires_at"
            raise ValueError(msg)
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            msg = "Saxo access_token_expires_at must be an RFC3339 datetime"
            raise ValueError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "Saxo access_token_expires_at must include a UTC offset"
        raise ValueError(msg)
    return parsed.astimezone(UTC)


def _parse_saxo_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        msg = f"Invalid Saxo chart timestamp: {value!r}"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "Saxo chart timestamp must include a UTC offset"
        raise ValueError(msg)
    return parsed


def _format_saxo_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
