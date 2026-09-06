import datetime as dt
import os

import pytest
from fastapi.testclient import TestClient

from scoring_engine.api import create_app
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore


@pytest.mark.integration
def test_scoring_api_with_postgres(provision_scoring_catalogue):
    """
    Integration smoke against a real Postgres instance.
    Requires env DATABASE_URL pointing to a reachable Postgres.
    Skips automatically if not set.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration smoke")

    store = AppScoreStore(db_url)
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    app = create_app(engine)
    client = TestClient(app)

    provision_scoring_catalogue(
        store,
        strategy_ids=["pg_integration_strat"],
    )

    # Send a signal
    payload = {
        "ts": dt.datetime.now(tz=dt.UTC).isoformat(),
        "strategy_id": "pg_integration_strat",
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.4, "confidence": 0.6, "horizon": "1D"},
        "context": {
            "asset_class": "crypto",
            "sector": "crypto",
            "industry": "layer1",
            "index": "crypto_index",
            "strategy_version": "1.0.0",
            "source": "lean",
            "entry_price": 31000,
            "stop_loss": 30000,
            "take_profit": 34000,
        },
    }
    resp = client.post("/api/v1/signals", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "BTCUSD"
    assert body["scope"] == "asset"

    # Check sector + market scores exist
    sector = store.get_latest_score("crypto", scope="sector")
    market = store.get_latest_score("crypto", scope="market")
    assert sector is not None
    assert market is not None


@pytest.mark.integration
def test_repeated_post_of_one_external_signal_id_keeps_one_canonical_row(
    provision_scoring_catalogue,
):
    """The PostgreSQL ``INSERT ... ON CONFLICT`` path deduplicates a redelivered signal.

    Strategy workers redeliver under the same ``external_signal_id`` after a
    reclaimed lease or a restart; this is the canonical boundary that turns
    at-least-once delivery into one canonical signal.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration smoke")

    from sqlalchemy import func, select

    from lib_application.db import models as app_models

    store = AppScoreStore(db_url)
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    client = TestClient(create_app(engine))
    provision_scoring_catalogue(store, strategy_ids=["pg_integration_dedup"])
    external_id = "pg-integration-dedup-1"
    payload = {
        "ts": dt.datetime.now(tz=dt.UTC).isoformat(),
        "strategy_id": "pg_integration_dedup",
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.4, "confidence": 0.6, "horizon": "1D"},
        "context": {
            "asset_class": "crypto",
            "sector": "crypto",
            "strategy_version": "1.0.0",
            "source": "lean",
            "entry_price": 31000,
            "stop_loss": 30000,
            "take_profit": 34000,
            "external_signal_id": external_id,
        },
    }
    try:
        for _ in range(2):
            assert client.post("/api/v1/signals", json=payload).status_code == 200
        with store.get_session() as session:
            count = session.execute(
                select(func.count())
                .select_from(app_models.CanonicalSignal)
                .where(app_models.CanonicalSignal.external_signal_id == external_id)
            ).scalar_one()
        assert count == 1
    finally:
        with store.get_session() as session:
            session.execute(
                app_models.CanonicalSignal.__table__.delete().where(
                    app_models.CanonicalSignal.external_signal_id == external_id
                )
            )
            session.commit()
