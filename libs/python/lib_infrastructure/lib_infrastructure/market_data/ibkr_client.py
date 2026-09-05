"""Interactive Brokers candle client — multi-asset (equities/options/futures/fx).

Implements :class:`IMarketDataProvider` over the IBKR **Client Portal Web API**
(the same REST gateway the IBKR broker adapter uses, ``https://localhost:5000``),
NOT the TWS socket API — so there is no ``ibapi``/``ib_insync`` dependency and the
auth model matches the broker side.

Auth is **session-based**: the IBKR Client Portal Gateway must be running and
browser-authenticated (username/password + 2FA). There is no API key/secret for
market data, and no account id is needed for candle history. Each fetch first
calls ``POST /v1/api/tickle``, which does double duty — it keeps the gateway
session alive (the gateway de-authenticates after inactivity) and reports auth
status — so an expired session fails loud instead of silently returning no data.

Configure the gateway via ``IBKR_GATEWAY_URL`` (default ``https://localhost:5000``);
``IBKR_CA_CERT`` pins the gateway certificate. Verification may be disabled only
for the self-signed loopback gateway; non-loopback gateways require a pinned
certificate.

If the gateway is unreachable / unauthenticated this raises
:class:`IBKRGatewayError` rather than silently returning no data.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from math import ceil
from typing import Any
from urllib.parse import urlparse

import httpx

from lib_common.logging import get_logger
from lib_data.market_data import CandleRow

from .models import validate_aware_range
from .ports import IMarketDataProvider

logger = get_logger(__name__)

# Coinbase-style granularity -> IBKR bar size.
_GRANULARITY_TO_BAR: dict[str, str] = {
    "ONE_MINUTE": "1min",
    "FIVE_MINUTE": "5min",
    "FIFTEEN_MINUTE": "15min",
    "THIRTY_MINUTE": "30min",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "ONE_DAY": "1d",
}
# Coinbase-style granularity -> canonical CandleRow.timeframe.
_GRANULARITY_TO_TIMEFRAME: dict[str, str] = {
    "ONE_MINUTE": "1m",
    "FIVE_MINUTE": "5m",
    "FIFTEEN_MINUTE": "15m",
    "THIRTY_MINUTE": "30m",
    "ONE_HOUR": "1h",
    "TWO_HOUR": "2h",
    "ONE_DAY": "1d",
}
_MAX_MINUTE_PERIOD = 30
_MAX_HOUR_PERIOD = 8
_MAX_DAY_PERIOD = 1000
_LOOPBACK_GATEWAY_HOSTS = {"localhost", "127.0.0.1", "::1"}


class IBKRGatewayError(RuntimeError):
    """The IBKR Client Portal Gateway is unreachable or unauthenticated."""


class IBKRCandleClient(IMarketDataProvider):
    """Historical candle provider over the IBKR Client Portal Web API.

    ``product_id`` must be an explicit numeric conid. Symbol search is
    deliberately not performed because the first result is unsafe for
    multi-exchange equities, dated futures, and option contracts.
    """

    DEFAULT_GATEWAY_URL = "https://localhost:5000"
    source = "ibkr"

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        url = gateway_url or os.getenv("IBKR_GATEWAY_URL") or self.DEFAULT_GATEWAY_URL
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            msg = "IBKR_GATEWAY_URL must be an absolute HTTPS URL"
            raise ValueError(msg)
        ca_cert = str(os.getenv("IBKR_CA_CERT") or "").strip()
        if parsed_url.hostname not in _LOOPBACK_GATEWAY_HOSTS and not ca_cert:
            msg = "A non-loopback IBKR gateway requires IBKR_CA_CERT certificate pinning"
            raise ValueError(msg)
        # The official local gateway ships with a self-signed certificate.
        # Remote gateways may not disable verification.
        verify: bool | str = ca_cert or False
        self._gateway_url = url
        self._client = client or httpx.Client(base_url=url, timeout=30.0, verify=verify)

    def close(self) -> None:
        self._client.close()

    def _unreachable_msg(self) -> str:
        return (
            f"IBKR Client Portal Gateway unreachable at {self._gateway_url} — start it "
            "and authenticate (browser login); set IBKR_GATEWAY_URL to override."
        )

    def _ensure_session(self) -> None:
        """Keep the Client Portal session alive and assert it is authenticated.

        ``POST /v1/api/tickle`` extends the gateway session (which de-authenticates
        after inactivity) and reports auth status via
        ``iserver.authStatus.authenticated``. Called before each fetch so a
        silently-expired session surfaces as :class:`IBKRGatewayError` instead of
        empty candle data — the single biggest IBKR end-to-end failure mode.
        """
        try:
            response = self._client.post("/v1/api/tickle")
        except httpx.RequestError as exc:
            raise IBKRGatewayError(self._unreachable_msg()) from exc

        authenticated = False
        if response.status_code == httpx.codes.OK:
            try:
                body = response.json()
            except ValueError:
                body = {}
            auth_status = (body.get("iserver") or {}).get("authStatus") or {}
            authenticated = bool(auth_status.get("authenticated"))

        if not authenticated:
            msg = (
                f"IBKR Client Portal session at {self._gateway_url} is not authenticated — "
                "open the gateway in a browser and log in (username/password + 2FA), then retry."
            )
            raise IBKRGatewayError(msg)

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
        conid_text = product_id.strip()
        if not conid_text.isdecimal() or int(conid_text) <= 0:
            msg = f"IBKR product_id must be an explicit numeric conid; got {product_id!r}"
            raise ValueError(msg)
        start_time, end_time = validate_aware_range(
            start_time,
            end_time,
            provider_name="IBKR",
        )
        self._ensure_session()
        gran = granularity.strip().upper()
        try:
            bar = _GRANULARITY_TO_BAR[gran]
            timeframe = _GRANULARITY_TO_TIMEFRAME[gran]
        except KeyError as exc:
            msg = f"Unsupported IBKR granularity: {granularity!r}"
            raise ValueError(msg) from exc

        period = self._period_for(start_time, end_time)
        try:
            response = self._client.get(
                "/v1/api/iserver/marketdata/history",
                params={
                    "conid": int(conid_text),
                    "period": period,
                    "bar": bar,
                    "outsideRth": "true",
                    "startTime": start_time.strftime("%Y%m%d-%H:%M:%S"),
                    "direction": "1",
                    "source": "Last",
                },
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise IBKRGatewayError(self._unreachable_msg()) from exc

        payload = response.json()
        return self._normalize(
            payload,
            instr_id=instr_id,
            product_id=product_id,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
        )

    def fetch_trading_schedule(self, *, product_id: str) -> dict[str, Any]:
        """Return IBKR's official bounded regular-session wire response."""
        conid_text = product_id.strip()
        if not conid_text.isdecimal() or int(conid_text) <= 0:
            msg = f"IBKR product_id must be an explicit numeric conid; got {product_id!r}"
            raise ValueError(msg)
        self._ensure_session()
        try:
            response = self._client.get(
                "/v1/api/contract/trading-schedule",
                params={"conid": conid_text},
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise IBKRGatewayError(self._unreachable_msg()) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            msg = f"IBKR trading-schedule response is not an object for conid {product_id!r}"
            raise TypeError(msg)
        return payload

    @staticmethod
    def _period_for(start_time: datetime, end_time: datetime) -> str:
        """Return the smallest supported IBKR period covering the window."""
        span_seconds = max(1.0, (end_time - start_time).total_seconds())
        minutes = max(1, ceil(span_seconds / 60))
        if minutes <= _MAX_MINUTE_PERIOD:
            return f"{minutes}min"
        hours = ceil(span_seconds / 3600)
        if hours <= _MAX_HOUR_PERIOD:
            return f"{hours}h"
        days = ceil(span_seconds / 86400)
        if days <= _MAX_DAY_PERIOD:
            return f"{days}d"
        msg = (
            "IBKR candle range exceeds the supported 1000-day period; "
            "page the request into smaller windows"
        )
        raise ValueError(msg)

    def _normalize(
        self,
        payload: dict[str, Any],
        *,
        instr_id: int,
        product_id: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CandleRow]:
        bars = payload.get("data")
        if not isinstance(bars, list):
            msg = f"IBKR history response has invalid data for conid {product_id!r}"
            raise TypeError(msg)
        start_naive = start_time.astimezone(UTC).replace(tzinfo=None)
        end_naive = end_time.astimezone(UTC).replace(tzinfo=None)
        rows: list[CandleRow] = []
        for bar in bars:
            try:
                # IBKR history bars: t (epoch ms), o/h/l/c, v (volume).
                ts = datetime.fromtimestamp(int(bar["t"]) / 1000, tz=UTC).replace(tzinfo=None)
                if not start_naive <= ts < end_naive:
                    continue
                rows.append(
                    CandleRow(
                        instr_id=instr_id,
                        ts=ts,
                        timeframe=timeframe,
                        open=float(bar["o"]),
                        high=float(bar["h"]),
                        low=float(bar["l"]),
                        close=float(bar["c"]),
                        volume=None if bar.get("v") is None else float(bar["v"]),
                        source=self.source,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed IBKR bar", product_id=product_id)
        rows.sort(key=lambda r: r.ts)
        return rows
