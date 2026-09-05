"""Resolve a market-data source name to an :class:`IMarketDataProvider`.

Single dispatch point so the ingestor picks its provider from ``INGESTOR_SOURCE``
without hardwiring a vendor. Adding a venue = a new adapter + one branch here.
"""

from __future__ import annotations

from .coinbase_client import CoinbaseCandleClient
from .delta_client import DeltaCandleClient
from .deribit_client import DeribitCandleClient
from .eodhd_client import EODHDClient
from .ibkr_client import IBKRCandleClient
from .ports import IMarketDataProvider
from .saxo_client import SaxoCandleClient
from .zerodha_client import ZerodhaCandleClient

_COINBASE_SOURCE = "coinbase_live"
_DERIBIT_SOURCE = "deribit"
_DERIBIT_TESTNET_SOURCE = "deribit_testnet"
_DELTA_SOURCE = "delta"
_DELTA_INDIA_SOURCE = "delta_india"
_EODHD_SOURCE = "eodhd"
_IBKR_SOURCE = "ibkr"
_SAXO_LIVE_SOURCE = "saxo_live"
_SAXO_SIMULATION_SOURCE = "saxo_simulation"
_ZERODHA_SOURCE = "zerodha"
_SUPPORTED_SOURCES = (
    _COINBASE_SOURCE,
    _DERIBIT_SOURCE,
    _DERIBIT_TESTNET_SOURCE,
    _DELTA_SOURCE,
    _DELTA_INDIA_SOURCE,
    _EODHD_SOURCE,
    _IBKR_SOURCE,
    _SAXO_LIVE_SOURCE,
    _SAXO_SIMULATION_SOURCE,
    _ZERODHA_SOURCE,
)


def get_market_data_provider(
    source: str,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    access_token: str | None = None,
    access_token_expires_at: str | None = None,
    account_key: str | None = None,
) -> IMarketDataProvider:
    """Return a candle provider for ``source``.

    - ``coinbase_live`` -> Coinbase spot (creds optional for public candles;
      required for higher rate limits).
    - ``deribit`` / ``deribit_testnet`` -> Deribit crypto options/futures/perps
      (public market data, no creds needed).
    - ``delta`` / ``delta_india`` -> the venue's regional public candle API.
    - ``ibkr`` -> IBKR equities/options/futures/fx via the
      Client Portal Web API gateway (session-auth; the gateway must be running +
      browser-authenticated — see IBKRCandleClient). Fetches fail loud with
      IBKRGatewayError if the gateway is unreachable.
    - ``zerodha`` -> Kite historical data, requiring an API key plus the
      account's current daily access token. ``product_id`` is the immutable
      numeric instrument token.
    - ``saxo_live`` / ``saxo_simulation`` -> Saxo OpenAPI v3 charts with
      immutable environment provenance. A current OAuth token and its RFC3339
      expiry are mandatory; ``product_id`` and ``broker_instrument_type`` are the
      explicit UIC and AssetType mapping.
    - ``eodhd`` -> EODHD daily equities/index bars (raw OHLC plus provider
      split-adjusted volume, ``ONE_DAY`` only; requires an API token passed as
      ``api_key``).

    Source labels are exact canonical provenance values, not aliases. Raises
    ``ValueError`` for an unknown or non-canonical source.
    """
    key = (source or "").strip()
    provider: IMarketDataProvider
    if key == _COINBASE_SOURCE:
        provider = CoinbaseCandleClient(api_key=api_key, api_secret=api_secret)
    elif key == _DERIBIT_SOURCE:
        provider = DeribitCandleClient()
    elif key == _DERIBIT_TESTNET_SOURCE:
        provider = DeribitCandleClient(testnet=True)
    elif key == _DELTA_SOURCE:
        provider = DeltaCandleClient()
    elif key == _DELTA_INDIA_SOURCE:
        provider = DeltaCandleClient(india=True)
    elif key == _IBKR_SOURCE:
        provider = IBKRCandleClient()
    elif key in {_SAXO_LIVE_SOURCE, _SAXO_SIMULATION_SOURCE}:
        provider = SaxoCandleClient(
            environment="live" if key == _SAXO_LIVE_SOURCE else "simulation",
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
            account_key=account_key,
        )
    elif key == _ZERODHA_SOURCE:
        provider = ZerodhaCandleClient(
            api_key=api_key,
            access_token=access_token,
        )
    elif key == _EODHD_SOURCE:
        if not api_key:
            msg = "EODHD requires an API token (pass it as api_key)"
            raise ValueError(msg)
        provider = EODHDClient(api_token=api_key)
    else:
        supported = ", ".join(_SUPPORTED_SOURCES)
        msg = f"Unknown market-data source: {source!r}; expected one of: {supported}"
        raise ValueError(msg)

    if provider.source != key:
        msg = (
            f"Market-data provider/source mismatch: configured {key!r}, "
            f"provider emits {provider.source!r}"
        )
        raise ValueError(msg)
    return provider
