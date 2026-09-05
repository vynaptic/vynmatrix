from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = PROJECT_ROOT / "apps" / "scoring_engine"
LIBS_DIR = PROJECT_ROOT / "libs" / "python"

for candidate in (
    APP_DIR,
    LIBS_DIR / "lib_common",
    LIBS_DIR / "lib_strategy",
    LIBS_DIR / "lib_application",
    LIBS_DIR / "lib_data",
    LIBS_DIR / "lib_infrastructure",
):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from lib_application.db import models as app_models
from scoring_engine.storage import AppScoreStore


@pytest.fixture
def provision_scoring_catalogue() -> Callable[..., None]:
    """Provision explicit control-plane catalogue rows for a scoring test."""

    def _provision(
        store: AppScoreStore,
        *,
        strategy_ids: Iterable[str],
        canonical: str = "BTC/USD",
        asset_class: str = "crypto",
        settlement_currency: str = "USD",
        sector_code: str | None = "crypto",
    ) -> None:
        with Session(store._engine) as session:
            instrument = (
                session.query(app_models.Instrument)
                .filter(
                    app_models.Instrument.asset_class == asset_class,
                    app_models.Instrument.canonical == canonical,
                )
                .one_or_none()
            )
            if instrument is None:
                instrument = app_models.Instrument(
                    canonical=canonical,
                    asset_class=asset_class,
                    settlement_currency=settlement_currency,
                    market_session_policy=(
                        "continuous" if asset_class == "crypto" else "scheduled"
                    ),
                )
                session.add(instrument)
                session.flush()

            if sector_code is not None:
                sector = (
                    session.query(app_models.Sector)
                    .filter(app_models.Sector.code == sector_code)
                    .one_or_none()
                )
                if sector is None:
                    sector = app_models.Sector(
                        code=sector_code,
                        name=sector_code,
                        asset_class=asset_class,
                    )
                    session.add(sector)
                    session.flush()
                assignment = (
                    session.query(app_models.InstrumentSector)
                    .filter(
                        app_models.InstrumentSector.instr_id == instrument.instr_id,
                        app_models.InstrumentSector.sector_id == sector.sector_id,
                    )
                    .one_or_none()
                )
                if assignment is None:
                    session.add(
                        app_models.InstrumentSector(
                            instr_id=instrument.instr_id,
                            sector_id=sector.sector_id,
                            weight=Decimal("1"),
                        )
                    )

            for strategy_id in strategy_ids:
                if session.get(app_models.Strategy, strategy_id) is None:
                    session.add(
                        app_models.Strategy(
                            strategy_id=strategy_id,
                            strategy_name=strategy_id,
                            asset_class=asset_class,
                        )
                    )
            session.commit()

    return _provision


@pytest.fixture
def app_store_with_btcusd() -> AppScoreStore:
    """Return a DB-backed store with the canonical signal-test catalogue."""
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with Session(store._engine) as session:
        instrument = app_models.Instrument(
            canonical="BTC/USD",
            asset_class="crypto",
            settlement_currency="USD",
            market_session_policy="continuous",
        )
        sector = app_models.Sector(code="crypto", name="crypto", asset_class="crypto")
        session.add_all([instrument, sector])
        session.flush()
        session.add(
            app_models.InstrumentSector(
                instr_id=instrument.instr_id,
                sector_id=sector.sector_id,
                weight=Decimal("1"),
            )
        )
        session.add_all(
            [
                app_models.Strategy(
                    strategy_id=strategy_id,
                    strategy_name=strategy_id,
                    asset_class="crypto",
                )
                for strategy_id in (
                    "test_strategy_alpha_v1",
                    "chatty",
                    "slow",
                    "old",
                    "mid",
                    "new",
                )
            ]
        )
        session.commit()
    return store
