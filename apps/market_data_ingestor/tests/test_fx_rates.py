from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from lib_data.market_data import CandleRow
from lib_infrastructure.market_data.ecb_client import ECBReferenceRate
from market_data_ingestor.fx_rates import FXRateIngestionError, FXRateIngestor

_NOW = datetime(2024, 12, 31, 16, 30, tzinfo=UTC)


class _ECBClient:
    def fetch_rates(self, *, quote_currencies: set[str]) -> list[ECBReferenceRate]:
        assert quote_currencies == {"USD", "INR"}
        return [
            ECBReferenceRate(
                quote_currency="USD",
                rate=Decimal("1.0389"),
                observed_at=datetime(2024, 12, 31, 15, 0, tzinfo=UTC),
            ),
            ECBReferenceRate(
                quote_currency="INR",
                rate=Decimal("88.9335"),
                observed_at=datetime(2024, 12, 31, 15, 0, tzinfo=UTC),
            ),
        ]

    def close(self) -> None:
        return None


class _CoinbaseClient:
    calls: list[dict[str, object]]

    def __init__(self) -> None:
        self.calls = []

    def fetch_candle_rows(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        start = kwargs["start_time"]
        return [
            CandleRow(
                instr_id=kwargs["instr_id"],
                ts=start.replace(tzinfo=None),
                timeframe="1h",
                open=0.9652,
                high=0.9673,
                low=0.9646,
                close=0.9661,
                volume=2126861.76,
                source="coinbase_live",
            )
        ]

    def close(self) -> None:
        return None


class _IngestionService:
    def __init__(self) -> None:
        self.rows: list[CandleRow] = []

    def load_instrument_map(self, *, asset_class: str):  # type: ignore[no-untyped-def]
        assert asset_class == "fx"
        return {"EURUSD": 10, "EURINR": 14, "USDCEUR": 16}

    def latest_candle_ts(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def oldest_candle_ts(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def upsert_candles(self, rows):  # type: ignore[no-untyped-def]
        self.rows = list(rows)
        return len(self.rows)


def _ingestor() -> tuple[FXRateIngestor, _IngestionService, _CoinbaseClient]:
    coinbase = _CoinbaseClient()
    ingestor = FXRateIngestor(
        session_factory=lambda: None,
        quote_currencies={"USD", "INR"},
        history_days=1,
        coinbase_product="USDC-EUR",
        ecb_client=_ECBClient(),  # type: ignore[arg-type]
        coinbase_client=coinbase,  # type: ignore[arg-type]
    )
    service = _IngestionService()
    ingestor._ingestion_svc = service  # type: ignore[assignment]
    return ingestor, service, coinbase


def test_run_once_persists_real_source_observations_with_provenance() -> None:
    ingestor, service, coinbase = _ingestor()

    summary = ingestor.run_once(now=_NOW)

    assert summary.ecb_observations == 2
    assert summary.coinbase_observations > 0
    assert summary.rows_upserted == len(service.rows)
    assert {(row.instr_id, row.source, row.timeframe) for row in service.rows} >= {
        (10, "ecb_reference", "1d"),
        (14, "ecb_reference", "1d"),
        (16, "coinbase_live", "1h"),
    }
    assert len(coinbase.calls) == 1
    assert coinbase.calls[0]["product_id"] == "USDC-EUR"
    assert ingestor.is_fresh() is True


def test_incremental_sync_reuses_durable_hourly_watermark() -> None:
    ingestor, service, coinbase = _ingestor()
    durable_latest = _NOW - timedelta(hours=2)
    service.latest_candle_ts = lambda *_args, **_kwargs: durable_latest  # type: ignore[method-assign]
    service.oldest_candle_ts = lambda *_args, **_kwargs: _NOW - timedelta(days=2)  # type: ignore[method-assign]

    ingestor.run_once(now=_NOW)

    assert len(coinbase.calls) == 1
    assert coinbase.calls[0]["start_time"] == durable_latest - timedelta(hours=1)


def test_missing_required_fx_instrument_fails_closed() -> None:
    ingestor, service, _coinbase = _ingestor()
    service.load_instrument_map = lambda **_kwargs: {"EURUSD": 10, "USDCEUR": 16}  # type: ignore[method-assign]

    with pytest.raises(FXRateIngestionError, match="missing EUR/INR"):
        ingestor.run_once(now=_NOW)


def test_rejects_noncanonical_stablecoin_leg() -> None:
    with pytest.raises(ValueError, match="must be Coinbase USDC-EUR"):
        FXRateIngestor(
            session_factory=lambda: None,
            quote_currencies={"USD"},
            coinbase_product="USDC-USD",
        )
