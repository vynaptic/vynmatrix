"""SC-asset flag-on hardening: latest-signal-per-strategy store read.

A flat newest-N page can be monopolised by one chatty strategy, crowding slower
strategies off the page. ``list_latest_signal_per_strategy`` returns exactly the
latest row per strategy. These tests lock the ``AppScoreStore`` windowed query
(exercised on sqlite) and the in-memory de-dup default used by unit tests.
"""

from __future__ import annotations

import datetime as dt

from scoring_engine.models import SignalRecord
from scoring_engine.storage import AppScoreStore
from scoring_engine.storage_memory import InMemoryScoreStore

NOW = dt.datetime(2026, 6, 26, 12, 0, tzinfo=dt.UTC)


def _rec(strategy_id: str, signal_id: str, ts: dt.datetime) -> SignalRecord:
    return SignalRecord(
        strategy_version="1.0.0",
        signal_id=signal_id,
        strategy_id=strategy_id,
        strategy_type="indicator",
        symbol="BTCUSD",
        action="long",
        confidence=0.8,
        timestamp=ts,
        asset_class="crypto",
        external_signal_id=f"ext-{signal_id}",
    )


def test_appstore_returns_latest_per_strategy_no_crowding(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    # chatty emits 5 rows; slow emits a single older row. A flat newest-5 page
    # would be all-chatty and hide slow entirely.
    for i in range(5):
        store.add_signal(_rec("chatty", f"c{i}", NOW - dt.timedelta(minutes=9 - i)))
    store.add_signal(_rec("slow", "s0", NOW - dt.timedelta(minutes=30)))

    latest = store.list_latest_signal_per_strategy("BTCUSD", limit=10)

    by_strat = {r.strategy_id: r for r in latest}
    assert set(by_strat) == {"chatty", "slow"}  # slow is NOT crowded out
    assert len(latest) == 2  # exactly one row per strategy
    assert by_strat["chatty"].signal_id == "c4"  # chatty's NEWEST row
    assert latest[0].strategy_id == "chatty"  # newest-first overall


def test_appstore_limit_caps_strategies_newest_first(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    store.add_signal(_rec("old", "o0", NOW - dt.timedelta(minutes=30)))
    store.add_signal(_rec("mid", "m0", NOW - dt.timedelta(minutes=20)))
    store.add_signal(_rec("new", "n0", NOW - dt.timedelta(minutes=10)))

    latest = store.list_latest_signal_per_strategy("BTCUSD", limit=2)

    # The two most-recent strategies, newest-first; the oldest strategy is dropped.
    assert [r.strategy_id for r in latest] == ["new", "mid"]


def test_appstore_unknown_symbol_returns_empty() -> None:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    assert store.list_latest_signal_per_strategy("NOPE", limit=10) == []


def test_inmemory_default_dedups_by_strategy_newest_first() -> None:
    store = InMemoryScoreStore()
    for i in range(5):
        store.add_signal(_rec("chatty", f"c{i}", NOW - dt.timedelta(minutes=9 - i)))
    store.add_signal(_rec("slow", "s0", NOW - dt.timedelta(minutes=30)))

    latest = store.list_latest_signal_per_strategy("BTCUSD", limit=10)

    by_strat = {r.strategy_id: r for r in latest}
    assert set(by_strat) == {"chatty", "slow"}
    assert by_strat["chatty"].signal_id == "c4"  # newest chatty row wins
