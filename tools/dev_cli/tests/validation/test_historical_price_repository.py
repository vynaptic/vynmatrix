"""Database-backed half-open historical-price validation contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from dev_cli.validation.historical_dataset import DatasetValidationError
from dev_cli.validation.persistence.historical_price_repository import (
    HistoricalPriceRepository,
)
from lib_application.db.models import Base, Instrument, InstrumentPrice


@pytest.fixture
def price_repository() -> tuple[HistoricalPriceRepository, int, object]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        instrument = Instrument(
            asset_class="crypto",
            canonical="BTCUSDC",
            exchange="coinbase",
            settlement_currency="USDC",
        )
        session.add(instrument)
        session.flush()
        for day, close in enumerate((100.0, 101.0, 102.0)):
            session.add(
                InstrumentPrice(
                    instr_id=instrument.instr_id,
                    ts=datetime(2025, 1, 1) + timedelta(days=day),
                    timeframe="1d",
                    open=Decimal(str(close)),
                    high=Decimal(str(close + 1)),
                    low=Decimal(str(close - 1)),
                    close=Decimal(str(close)),
                    volume=Decimal("10"),
                    source="coinbase_validation_v1",
                )
            )
        session.commit()
        instr_id = instrument.instr_id
    return HistoricalPriceRepository(factory), int(instr_id), factory


def test_loader_uses_half_open_end_and_preserves_authoritative_metadata(
    price_repository: tuple[HistoricalPriceRepository, int, object],
) -> None:
    repository, instr_id, _factory = price_repository
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars, audit = repository.load_validated_bars(
        instr_id,
        start=start,
        end=start + timedelta(days=2),
        timeframe="1d",
        source="coinbase_validation_v1",
        metadata={
            "requested_product": "BTC-USDC",
            "canonical_product": "BTC-USD",
            "source": "spoofed",
            "timeframe": "1m",
            "timestamp_semantics": "period_close",
        },
    )

    assert len(bars) == 2
    assert bars[0].timestamp == start
    assert bars[-1].timestamp == datetime(2025, 1, 2, tzinfo=UTC)
    assert bars[0].metadata["requested_product"] == "BTC-USDC"
    assert bars[0].metadata["source"] == "coinbase_validation_v1"
    assert bars[0].metadata["timeframe"] == "1d"
    assert bars[0].metadata["price_timeframe"] == "1d"
    assert bars[0].metadata["timestamp_semantics"] == "period_start"
    assert audit.coverage == 1.0


def test_loader_fails_when_required_interval_has_a_gap(
    price_repository: tuple[HistoricalPriceRepository, int, object],
) -> None:
    repository, instr_id, _factory = price_repository
    start = datetime(2025, 1, 1, tzinfo=UTC)

    with pytest.raises(DatasetValidationError):
        repository.load_validated_bars(
            instr_id,
            start=start,
            end=start + timedelta(days=4),
            timeframe="1d",
            source="coinbase_validation_v1",
        )


@pytest.mark.parametrize("field", ["open", "high", "low", "volume"])
def test_loader_rejects_missing_ohlcv_without_repair(
    price_repository: tuple[HistoricalPriceRepository, int, object],
    field: str,
) -> None:
    repository, instr_id, factory = price_repository
    with factory() as session:
        row = (
            session.execute(select(InstrumentPrice).where(InstrumentPrice.instr_id == instr_id))
            .scalars()
            .first()
        )
        assert row is not None
        setattr(row, field, None)
        session.commit()

    start = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match=rf"missing required OHLCV fields: {field}"):
        repository.load_validated_bars(
            instr_id,
            start=start,
            end=start + timedelta(days=1),
            timeframe="1d",
            source="coinbase_validation_v1",
        )


def test_loader_does_not_replace_zero_price_with_close(
    price_repository: tuple[HistoricalPriceRepository, int, object],
) -> None:
    repository, instr_id, factory = price_repository
    with factory() as session:
        row = (
            session.execute(select(InstrumentPrice).where(InstrumentPrice.instr_id == instr_id))
            .scalars()
            .first()
        )
        assert row is not None
        row.open = Decimal("0")
        session.commit()

    start = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(DatasetValidationError) as exc_info:
        repository.load_validated_bars(
            instr_id,
            start=start,
            end=start + timedelta(days=1),
            timeframe="1d",
            source="coinbase_validation_v1",
        )

    assert exc_info.value.audit.malformed_rows == 1
