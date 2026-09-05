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
