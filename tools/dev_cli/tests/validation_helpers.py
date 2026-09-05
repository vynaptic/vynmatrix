"""Shared constructors and immutable market-fixture loaders for validation tests."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib_data.bars import Bar

_MARKET_FIXTURE_CONTRACTS: dict[str, tuple[str, dict[str, object]]] = {
    "coinbase_btcusd_1m_2026-06-10.json": (
        "3e7a30264168dac798ff65af963a5952c1dcaceda71ff7a8acbeb91d948adfee",
        {
            "product": "BTC-USD",
            "granularity_seconds": 60,
            "source": "coinbase_exchange_public",
            "window_start": "2026-06-10T00:00:00+00:00",
            "window_end": "2026-06-11T01:00:00+00:00",
            "bar_count": 1501,
        },
    ),
    "coinbase_btcusdc_1d_2026-06-10.json": (
        "2db875d045015c91e966b243508f00ea73dd453a00545b8bde7f8016f3a049c0",
        {
            "requested_product": "BTC-USDC",
            "canonical_book": "BTC-USD",
            "granularity": "ONE_DAY",
            "source": "coinbase_advanced_trade_public",
            "retrieved_on": "2026-07-21",
            "request_url": (
                "https://api.coinbase.com/api/v3/brokerage/market/products/"
                "BTC-USDC/candles?start=1781049600&end=1781136000&"
                "granularity=ONE_DAY&limit=10"
            ),
            "timestamp_semantics": "period_start",
        },
    ),
}


def load_market_fixture(path: Path) -> dict[str, Any]:
    """Load a registered real-market fixture after hash and identity checks."""

    contract = _MARKET_FIXTURE_CONTRACTS.get(path.name)
    if contract is None:
        message = f"unregistered market fixture: {path.name}"
        raise AssertionError(message)
    expected_sha256, expected_metadata = contract
    payload_bytes = path.read_bytes()
    observed_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        message = (
            f"market fixture hash differs for {path.name}: {observed_sha256} != {expected_sha256}"
        )
        raise AssertionError(message)
    payload: dict[str, Any] = json.loads(payload_bytes)
    observed_metadata = {key: payload.get(key) for key in expected_metadata}
    if observed_metadata != expected_metadata:
        message = f"market fixture identity differs for {path.name}"
        raise AssertionError(message)
    return payload


def bars_from_ohlcv(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str = "1m",
    source: str | None = None,
) -> list[Bar]:
    """Build period-start-stamped bars from fixture OHLCV rows."""

    bars: list[Bar] = []
    for row in rows:
        metadata = dict(row.get("metadata") or {})
        metadata.setdefault("timestamp_semantics", "period_start")
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(int(row["ts"]), tz=UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
                timeframe=timeframe,
                source=source,
                metadata=metadata,
            )
        )
    return bars
