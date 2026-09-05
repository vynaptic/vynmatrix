"""Instrument resolution and fail-closed catalogue ownership."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from scoring_engine.models import SignalRecord
from scoring_engine.storage import AppScoreStore


def _store_with_btc() -> AppScoreStore:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with Session(store._engine) as s:
        s.add(
            app_models.Instrument(
                canonical="BTC/USD",
                asset_class="crypto",
                settlement_currency="USD",
            )
        )
        s.commit()
    return store


def test_symbol_variants_resolve_to_same_instrument() -> None:
    store = _store_with_btc()
    with Session(store._engine) as s:
        canonical_id = (
            s.query(app_models.Instrument.instr_id)
            .filter(app_models.Instrument.canonical == "BTC/USD")
            .scalar()
        )

    # Separator/case variants normalize to the same canonical form and must
    # resolve to the existing instrument rather than returning None / spawning
    # a duplicate.
    assert store.resolve_instrument_id("BTC/USD") == canonical_id
    assert store.resolve_instrument_id("BTCUSD") == canonical_id
    assert store.resolve_instrument_id("BTC-USD") == canonical_id
    assert store.resolve_instrument_id("btc-usd") == canonical_id


def test_distinct_assets_do_not_collide() -> None:
    store = _store_with_btc()
    # A genuinely different pair must not normalize-match BTC/USD.
    assert store.resolve_instrument_id("ETH/USD") is None
    assert store.resolve_instrument_id("BTC/USDT") is None


def _signal(*, strategy_id: str = "catalogued", sector: str | None = "crypto") -> SignalRecord:
    return SignalRecord(
        signal_id="catalogue-contract-signal",
        strategy_id=strategy_id,
        strategy_type="indicator",
        symbol="BTCUSD",
        action="long",
        confidence=0.8,
        timestamp=datetime(2026, 7, 25, tzinfo=UTC),
        sector=sector,
        asset_class="crypto",
        external_signal_id=f"catalogue-contract-{strategy_id}-{sector}",
    )


def test_signal_ingest_rejects_unknown_strategy_without_creating_it(
    provision_scoring_catalogue,
) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    provision_scoring_catalogue(store, strategy_ids=[])

    with pytest.raises(ValueError, match="Unknown strategy"):
        store.add_signal(_signal(strategy_id="not-provisioned"))

    with Session(store._engine) as session:
        assert session.query(app_models.Strategy).count() == 0


def test_signal_ingest_rejects_unknown_sector_without_creating_it(
    provision_scoring_catalogue,
) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    provision_scoring_catalogue(
        store,
        strategy_ids=["catalogued"],
        sector_code=None,
    )

    with pytest.raises(ValueError, match="Unknown sector"):
        store.add_signal(_signal(sector="not-provisioned"))

    with Session(store._engine) as session:
        assert session.query(app_models.Sector).count() == 0


def test_signal_ingest_rejects_missing_instrument_sector_assignment(
    provision_scoring_catalogue,
) -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    provision_scoring_catalogue(
        store,
        strategy_ids=["catalogued"],
        sector_code=None,
    )
    with Session(store._engine) as session:
        session.add(
            app_models.Sector(
                code="crypto",
                name="crypto",
                asset_class="crypto",
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="is not assigned to sector"):
        store.add_signal(_signal())

    with Session(store._engine) as session:
        assert session.query(app_models.CanonicalSignal).count() == 0


def test_scoring_store_exposes_no_runtime_catalogue_write_surface() -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")

    assert not hasattr(store, "upsert_instrument")
