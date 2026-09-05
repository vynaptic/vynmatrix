import datetime as dt

from lib_application.db.models import Strategy, StrategyVersion
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore


def test_strategy_version_resolution_uses_signal_semver_not_latest_release():
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    with store.get_session() as session:
        session.add(
            Strategy(
                strategy_id="versioned_strategy_v1",
                strategy_name="VersionedStrategy",
                asset_class="crypto",
            )
        )
        session.add_all(
            [
                StrategyVersion(
                    strat_ver_id=9101,
                    strategy_id="versioned_strategy_v1",
                    semver="1.0.0",
                    param_schema={},
                    default_params={},
                    status="deprecated",
                    released_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                ),
                StrategyVersion(
                    strat_ver_id=9102,
                    strategy_id="versioned_strategy_v1",
                    semver="1.0.1",
                    param_schema={},
                    default_params={},
                    status="active",
                    released_at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                ),
            ]
        )
        session.commit()

        assert store._resolve_strategy_version(session, "versioned_strategy_v1", "1.0.0") == 9101
        assert store._resolve_strategy_version(session, "versioned_strategy_v1", "1.0.1") == 9102
        assert store._resolve_strategy_version(session, "versioned_strategy_v1") == 9102
        assert store._resolve_strategy_version(session, "versioned_strategy_v1", "9.9.9") is None


def test_signal_roundtrip_sqlite_persists_all_fields(provision_scoring_catalogue):
    """
    End-to-end round-trip:
    - create in-memory SQL store
    - seed instrument hierarchy (asset_class/sector/industry/index)
    - ingest a full Signal
    - verify asset/sector/market scores and persisted signal fields
    """

    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store, default_weight=1.0, half_life_bars=10)
    provision_scoring_catalogue(
        store,
        strategy_ids=["test_strat"],
    )

    now = dt.datetime.now(tz=dt.UTC)
    signal = Signal(
        strategy_id="test_strat",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=now,
        entry_price=30000.0,
        stop_loss=29000.0,
        take_profit=33000.0,
        size_hint=0.0,
        asset_class="crypto",
        strategy_version="1.0.0",
        source="coinbase_live",
        external_signal_id="ext-roundtrip-test-strat",
        metadata={"note": "roundtrip-test"},
    )

    engine.ingest_signal(signal, sector="crypto", industry="layer1", index="crypto_index")

    asset_score = store.get_latest_score("BTCUSD", scope="asset")
    sector_score = store.get_latest_score("crypto", scope="sector")
    market_score = store.get_latest_score("crypto", scope="market")

    assert asset_score is not None
    assert sector_score is not None
    assert market_score is not None

    stored_signals = store.list_signals("BTCUSD", limit=1)
    assert stored_signals, "Signal was not persisted"
    persisted = stored_signals[0]
    assert persisted.entry_price == 30000.0
    assert persisted.stop_loss == 29000.0
    assert persisted.take_profit == 33000.0
    assert persisted.asset_class == "crypto"
    assert persisted.sector == "crypto"
    assert persisted.industry == "layer1"
    assert persisted.index == "crypto_index"
    assert (
        persisted.strategy_version == "1.0.0"
        or persisted.metadata.get("strategy_version") == "1.0.0"
    )
    assert (
        persisted.source == "coinbase_live" or persisted.metadata.get("source") == "coinbase_live"
    )


def test_options_signal_roundtrip_preserves_contract_metadata(provision_scoring_catalogue):
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    engine = ScoreEngine(store, default_weight=1.0, half_life_bars=10)

    symbol = "NIFTY:2026-04-09:22000:CALL"
    provision_scoring_catalogue(
        store,
        strategy_ids=["options_test_strategy_v1"],
        canonical=symbol,
        asset_class="options",
        settlement_currency="INR",
        sector_code="equity_index",
    )

    now = dt.datetime.now(tz=dt.UTC)
    signal = Signal(
        strategy_id="options_test_strategy_v1",
        strategy_type="indicator",
        symbol=symbol,
        action=SignalAction.SHORT,
        confidence=0.8,
        timestamp=now,
        entry_price=240.0,
        stop_loss=250.0,
        take_profit=192.0,
        asset_class="options",
        source="coinbase_live",
        external_signal_id="ext-options-test-strategy",
        metadata={
            "underlying_symbol": "NIFTY",
            "underlying_asset_class": "index",
            "expiry": "2026-04-09",
            "strike": 22000.0,
            "option_type": "CALL",
            "marked_level": 250.0,
            "session_open_price": 241.0,
            "session_date": "2026-04-02",
            "side_bucket": "CE",
            "entry_index": 1,
            "lot_size": 50,
            "contract_multiplier": 1,
            "hard_stop_price": 276.0,
            "price_timeframe": "3m",
        },
    )

    engine.ingest_signal(signal, sector="equity_index", industry="index_options", index="NIFTY50")

    stored = store.list_signals(symbol, limit=1)
    assert stored
    persisted = stored[0]
    assert persisted.asset_class == "options"
    assert persisted.metadata["underlying_symbol"] == "NIFTY"
    assert persisted.metadata["expiry"] == "2026-04-09"
    assert persisted.metadata["strike"] == 22000.0
    assert persisted.metadata["option_type"] == "CALL"
    assert persisted.metadata["session_open_price"] == 241.0
    assert persisted.metadata["side_bucket"] == "CE"
    assert persisted.metadata["lot_size"] == 50
    assert persisted.metadata["hard_stop_price"] == 276.0
