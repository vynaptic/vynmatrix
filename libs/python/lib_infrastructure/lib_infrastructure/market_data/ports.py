"""Market-data provider port (multi-asset ingestion seam).

One implementation per data source (Coinbase spot, Deribit crypto
options/futures/perps, IBKR multi-asset, ...). The ingestor resolves the
provider by ``INGESTOR_SOURCE`` and consumes them uniformly through
``fetch_candle_rows``, so adding an asset class / venue is a new adapter + a
registry entry, not a scheduler rewrite. Granularity uses the Coinbase-style
enum (``ONE_MINUTE``, ...); each adapter maps it to its own API + to the
canonical ``CandleRow.timeframe``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from lib_data.market_data import CandleRow


class IMarketDataProvider(ABC):
    """Fetches OHLCV candles for one instrument and returns canonical rows.

    Implementations carry an intrinsic ``source`` tag (e.g. ``coinbase_live``,
    ``deribit``) which is stamped onto every emitted ``CandleRow`` so downstream
    price lookups can prefer or filter by source.
    """

    #: Source tag stamped onto emitted rows (set by each implementation).
    source: str = "unknown"

    @abstractmethod
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
        """Fetch + normalize candles in ``[start_time, end_time]`` for one
        instrument. ``instr_id`` is the DB instrument id stamped onto each row.
        ``broker_instrument_type`` carries an explicit provider enum when an opaque
        product id is insufficient (for example Saxo UIC + AssetType). Returns
        rows ascending by timestamp; empty on no data."""

    @abstractmethod
    def close(self) -> None:
        """Release any underlying network resources."""
