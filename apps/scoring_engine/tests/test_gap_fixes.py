"""Tests for gap fixes across the signal flow pipeline.

Phase 1: GAP-12 (CLOSE→flat), GAP-13 (strategy.code)
"""

import datetime as dt

import pytest

from lib_strategy.signals.normalization import normalize_scoring_action
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore

# ---------------------------------------------------------------------------
# GAP-12: CLOSE signals must persist as "flat", not "hold"
# ---------------------------------------------------------------------------


class TestGap12CloseFlatMapping:
    """CLOSE signals should map to 'flat' in scoring persistence."""

    def _make_store_and_engine(self, provision_scoring_catalogue):
        store = AppScoreStore("sqlite+pysqlite:///:memory:")
        engine = ScoreEngine(store, default_weight=1.0, half_life_bars=10)
        provision_scoring_catalogue(
            store,
            strategy_ids=["test_exit", "test_long", "test_short", "test_hold"],
        )
        return store, engine

    def test_close_signal_persists_as_flat(self, provision_scoring_catalogue):
        """A CLOSE signal must be stored with action='flat', not 'hold'."""
        store, engine = self._make_store_and_engine(provision_scoring_catalogue)
        now = dt.datetime.now(tz=dt.UTC)
        signal = Signal(
            strategy_version="1.0.0",
            strategy_id="test_exit",
            strategy_type="indicator",
            symbol="BTCUSD",
            action=SignalAction.CLOSE,
            confidence=0.9,
            timestamp=now,
            asset_class="crypto",
            external_signal_id="ext-test-exit",
        )
        engine.ingest_signal(signal, sector="crypto")

        stored = store.list_signals("BTCUSD", limit=1)
        assert stored, "Signal was not persisted"
        assert stored[0].action == "flat", (
            f"CLOSE signal stored as '{stored[0].action}' instead of 'flat'"
        )

    def test_long_signal_persists_as_long(self, provision_scoring_catalogue):
        """Regression: LONG signals still stored as 'long'."""
        store, engine = self._make_store_and_engine(provision_scoring_catalogue)
        now = dt.datetime.now(tz=dt.UTC)
        signal = Signal(
            strategy_version="1.0.0",
            strategy_id="test_long",
            strategy_type="indicator",
            symbol="BTCUSD",
            action=SignalAction.LONG,
            confidence=0.8,
            timestamp=now,
            asset_class="crypto",
            external_signal_id="ext-test-long",
        )
        engine.ingest_signal(signal, sector="crypto")

        stored = store.list_signals("BTCUSD", limit=1)
        assert stored[0].action == "long"

    def test_short_signal_persists_as_short(self, provision_scoring_catalogue):
        """Regression: SHORT signals still stored as 'short'."""
        store, engine = self._make_store_and_engine(provision_scoring_catalogue)
        now = dt.datetime.now(tz=dt.UTC)
        signal = Signal(
            strategy_version="1.0.0",
            strategy_id="test_short",
            strategy_type="indicator",
            symbol="BTCUSD",
            action=SignalAction.SHORT,
            confidence=0.8,
            timestamp=now,
            asset_class="crypto",
            external_signal_id="ext-test-short",
        )
        engine.ingest_signal(signal, sector="crypto")

        stored = store.list_signals("BTCUSD", limit=1)
        assert stored[0].action == "short"

    def test_hold_signal_persists_as_hold(self, provision_scoring_catalogue):
        """Regression: HOLD signals still stored as 'hold'."""
        store, engine = self._make_store_and_engine(provision_scoring_catalogue)
        now = dt.datetime.now(tz=dt.UTC)
        signal = Signal(
            strategy_version="1.0.0",
            strategy_id="test_hold",
            strategy_type="indicator",
            symbol="BTCUSD",
            action=SignalAction.HOLD,
            confidence=0.5,
            timestamp=now,
            asset_class="crypto",
            external_signal_id="ext-test-hold",
        )
        engine.ingest_signal(signal, sector="crypto")

        stored = store.list_signals("BTCUSD", limit=1)
        assert stored[0].action == "hold"


# ---------------------------------------------------------------------------
# GAP-12 unit: normalize_scoring_action correctness
# ---------------------------------------------------------------------------


class TestNormalizeScoringAction:
    """Verify normalize_scoring_action maps all SignalAction variants correctly."""

    def test_close_to_flat(self):
        assert normalize_scoring_action(SignalAction.CLOSE) == "flat"

    def test_long_to_long(self):
        assert normalize_scoring_action(SignalAction.LONG) == "long"

    def test_short_to_short(self):
        assert normalize_scoring_action(SignalAction.SHORT) == "short"

    def test_hold_to_hold(self):
        assert normalize_scoring_action(SignalAction.HOLD) == "hold"

    def test_string_flat_to_flat(self):
        assert normalize_scoring_action("flat") == "flat"

    def test_string_close_to_flat(self):
        assert normalize_scoring_action("close") == "flat"

    def test_string_exit_to_flat(self):
        assert normalize_scoring_action("exit") == "flat"


def test_market_score_propagates_store_failure_instead_of_fabricating_context() -> None:
    class _BrokenStore:
        def list_all_signals(self, limit=500):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    engine = ScoreEngine(_BrokenStore())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="boom"):
        engine._compute_market_score("crypto")


def test_market_score_requires_explicit_asset_class() -> None:
    class _EmptyStore:
        def list_all_signals(self, limit=500):  # type: ignore[no-untyped-def]
            return []

    engine = ScoreEngine(_EmptyStore())  # type: ignore[arg-type]

    assert engine._compute_market_score("") is None
