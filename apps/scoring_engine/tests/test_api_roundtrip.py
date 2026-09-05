import datetime as dt

from fastapi.testclient import TestClient

from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id
from scoring_engine.api import create_app
from scoring_engine.domain import MarketContext, MarketRegime
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore


def _set_observed_context(engine: ScoreEngine) -> None:
    engine.pipeline.set_market_context(
        "BTCUSD",
        MarketContext(
            regime=MarketRegime.BULL_QUIET,
            as_of=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1),
            realized_vol_20d=0.01,
        ),
    )


def test_api_roundtrip_sqlite(provision_scoring_catalogue):
    """POST to /api/v1/signals and verify asset/sector/market scores round-trip."""
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    _set_observed_context(engine)

    provision_scoring_catalogue(
        store,
        strategy_ids=["api_roundtrip_strat"],
    )

    app = create_app(engine)
    client = TestClient(app)

    signal_ts = dt.datetime.now(tz=dt.UTC)
    payload = {
        "ts": signal_ts.isoformat(),
        "strategy_id": "api_roundtrip_strat",
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.5, "confidence": 0.9, "horizon": "1D"},
        "context": {
            "asset_class": "crypto",
            "sector": "crypto",
            "industry": "layer1",
            "index": "crypto_index",
            "strategy_version": "1.0.0",
            "source": "lean",
            "entry_price": 30000,
            "stop_loss": 29000,
            "take_profit": 33000,
            "expires_at": "2030-01-01T01:00:00+00:00",
        },
    }

    resp = client.post("/api/v1/signals", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "BTCUSD"
    assert body["scope"] == "asset"
    assert body["score"] != 0.0

    # Verify persisted signal fields
    persisted = store.list_signals("BTCUSD", limit=1)[0]
    assert persisted.asset_class == "crypto"
    assert persisted.sector == "crypto"
    assert persisted.industry == "layer1"
    assert persisted.index == "crypto_index"
    assert (
        persisted.strategy_version == "1.0.0"
        or persisted.metadata.get("strategy_version") == "1.0.0"
    )
    assert persisted.source == "lean" or persisted.metadata.get("source") == "lean"
    assert persisted.entry_price == 30000
    assert persisted.stop_loss == 29000
    assert persisted.take_profit == 33000
    assert persisted.expires_at == dt.datetime(2030, 1, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)
    assert persisted.external_signal_id == compute_external_signal_id(
        strategy_id="api_roundtrip_strat",
        strategy_version="1.0.0",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        bar_close_ts=signal_ts,
    )

    # Sector and market scores should exist
    assert store.get_latest_score("crypto", scope="sector") is not None
    assert store.get_latest_score("crypto", scope="market") is not None


def test_insight_signal_rejects_malformed_timestamp() -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    client = TestClient(create_app(engine))

    response = client.post(
        "/api/v1/signals",
        json={
            "ts": "not-a-timestamp",
            "strategy_id": "timestamp_validation",
            "symbol": "BTCUSD",
            "insight": {
                "direction": "Up",
                "magnitude": 0.5,
                "confidence": 0.9,
                "horizon": "1D",
            },
            "context": {},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid signal timestamp"


def test_insight_signal_rejects_blank_external_identity() -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    client = TestClient(create_app(engine))

    response = client.post(
        "/api/v1/signals",
        json={
            "ts": "2026-03-07T00:00:00Z",
            "strategy_id": "identity_validation",
            "symbol": "BTCUSD",
            "insight": {
                "direction": "Up",
                "magnitude": 0.5,
                "confidence": 0.9,
                "horizon": "1D",
            },
            "context": {"external_signal_id": "   "},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid external_signal_id"


def test_api_derives_expected_return_from_price_ladder_when_magnitude_is_placeholder_zero(
    provision_scoring_catalogue,
):
    """Entry signals with TP/SL must not be forced to zero-return by HTTP translation."""
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    _set_observed_context(engine)

    provision_scoring_catalogue(
        store,
        strategy_ids=["api_rr_ladder_strat"],
    )

    app = create_app(engine)
    client = TestClient(app)

    payload = {
        "ts": dt.datetime.now(tz=dt.UTC).isoformat(),
        "strategy_id": "api_rr_ladder_strat",
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.0, "confidence": 0.75, "horizon": "1D"},
        "context": {
            "strategy_version": "1.0.0",
            "asset_class": "crypto",
            "sector": "crypto",
            "industry": "layer1",
            "index": "crypto_index",
            "entry_price": 83803.99,
            "stop_loss": 82235.01,
            "take_profit": 86157.46,
        },
    }

    resp = client.post("/api/v1/signals", json=payload)
    assert resp.status_code == 200
    assert resp.json()["score"] > 0.0

    persisted = store.list_signals("BTCUSD", limit=1)[0]
    assert persisted.expected_return is not None
    assert persisted.expected_return > 0.0
    assert persisted.predicted_risk is not None
    assert persisted.predicted_risk > 0.0


def test_api_entry_only_signal_does_not_collapse_to_zero_score(
    provision_scoring_catalogue,
):
    """An entry-only signal must score non-zero or it cannot clear a binding threshold."""
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    _set_observed_context(engine)

    provision_scoring_catalogue(
        store,
        strategy_ids=["api_entry_only_strat"],
    )

    app = create_app(engine)
    client = TestClient(app)

    payload = {
        "ts": dt.datetime.now(tz=dt.UTC).isoformat(),
        "strategy_id": "api_entry_only_strat",
        "symbol": "BTCUSD",
        "insight": {"direction": "Up", "magnitude": 0.0, "confidence": 0.75, "horizon": "1D"},
        "context": {
            "strategy_version": "1.0.0",
            "asset_class": "crypto",
            "sector": "crypto",
            "industry": "layer1",
            "index": "crypto_index",
            "entry_price": 83803.99,
        },
    }

    resp = client.post("/api/v1/signals", json=payload)
    assert resp.status_code == 200
    assert resp.json()["score"] != 0.0, "entry-only signal collapsed to a zero score"

    persisted = store.list_signals("BTCUSD", limit=1)[0]
    assert persisted.expected_return is not None
    assert persisted.expected_return > 0.0
