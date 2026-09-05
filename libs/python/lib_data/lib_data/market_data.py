"""Neutral market-data primitives shared across layers.

``normalize_product_symbol`` and ``CandleRow`` are pure, dependency-free data
helpers used by the data plane (ingestion adapters), the application services
(price upsert) and the scoring engine (symbol matching). They live in
``lib_data`` — the lowest data layer — so application/scoring code does not have
to import them from ``lib_infrastructure`` (which would invert the dependency
direction: a use-case/domain layer depending on an adapter layer).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lib_common.asset_classes import normalize_asset_class


def normalize_product_symbol(symbol: str) -> str:
    """Normalize exchange product IDs (e.g. BTC-USD) to canonical form (BTCUSD)."""
    return symbol.replace("-", "").replace("/", "").replace("_", "").strip().upper()


@dataclass
class CandleRow:
    """A single OHLCV row ready for DB upsert."""

    instr_id: int
    ts: datetime
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instr_id": self.instr_id,
            "ts": self.ts,
            "timeframe": self.timeframe,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class MarketDataInstrument:
    """Canonical instrument plus the exact identifier required by one venue.

    ``canonical_symbol`` is the database and downstream identity. ``product_id``
    is the broker/exchange identifier sent to the provider. Keeping both
    prevents Saxo UICs, IBKR conids, and Kite instrument tokens from leaking
    into watermarks or PostgreSQL notifications.
    """

    canonical_symbol: str
    product_id: str
    asset_class: str
    broker_instrument_type: str | None = None

    def __post_init__(self) -> None:
        canonical_symbol = self.canonical_symbol.strip()
        product_id = self.product_id.strip()
        asset_class = normalize_asset_class(
            self.asset_class,
            field_name="market-data asset_class",
        )
        broker_instrument_type = (
            self.broker_instrument_type.strip() if self.broker_instrument_type is not None else None
        )
        if not canonical_symbol:
            msg = "Market-data canonical_symbol must be non-empty"
            raise ValueError(msg)
        if not product_id:
            msg = "Market-data product_id must be non-empty"
            raise ValueError(msg)
        object.__setattr__(self, "canonical_symbol", canonical_symbol)
        object.__setattr__(self, "product_id", product_id)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "broker_instrument_type", broker_instrument_type or None)
