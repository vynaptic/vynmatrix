"""Decision provenance: every scored signal writes an immutable
``decision_contexts`` snapshot on the SAME unit of work as the signal + scores,
so a trade can be replayed/audited against exactly the inputs that produced it.

The in-memory store keeps the no-op default (provenance is a drop-in), and the
DB-backed store persists a row that rolls back atomically with the decision.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lib_application.db.models import DecisionContext
from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore, InMemoryScoreStore

TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _store_and_engine(provision_scoring_catalogue) -> tuple[AppScoreStore, ScoreEngine]:
    store = AppScoreStore("sqlite+pysqlite:///:memory:")
    provision_scoring_catalogue(
        store,
        strategy_ids=["test_strategy_v1"],
    )
    engine = ScoreEngine(store=store, default_weight=1.0, half_life_bars=10)
    return store, engine


def _signal(ext_id: str) -> Signal:
    return Signal(
        strategy_version="1.0.0",
        strategy_id="test_strategy_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.8,
        timestamp=TS,
        entry_price=100.0,
        external_signal_id=ext_id,
    )


def _contexts(store: AppScoreStore) -> list[DecisionContext]:
    with store.get_session() as session:
        return list(session.query(DecisionContext).all())


def test_ingest_persists_decision_context(provision_scoring_catalogue) -> None:
    store, engine = _store_and_engine(provision_scoring_catalogue)

    with store.unit_of_work():
        score = engine.ingest_signal(_signal("ext-prov-1"))

    rows = _contexts(store)
    assert len(rows) == 1
    ctx = rows[0]
    assert ctx.signal_id  # domain Signal UUID captured
    assert ctx.symbol == "BTCUSD"
    assert ctx.strategy_id == "test_strategy_v1"
    assert ctx.action == "long"
    # score_value mirrors the standardized global score returned to the caller.
    assert ctx.score_value is not None
    assert float(ctx.score_value) == pytest.approx(score.score, rel=1e-6)
    # feature_snapshot carries the per-factor breakdown for replay.
    assert ctx.feature_snapshot is not None
    assert "alpha_raw" in ctx.feature_snapshot
    assert "components" in ctx.feature_snapshot
    assert ctx.abstained is True
    assert ctx.abstain_reason == "meta_inputs_unavailable"


def test_decision_context_rolls_back_with_signal(provision_scoring_catalogue) -> None:
    store, engine = _store_and_engine(provision_scoring_catalogue)

    def _ingest_then_crash() -> None:
        with store.unit_of_work():
            engine.ingest_signal(_signal("ext-prov-rollback"))
            raise RuntimeError("boom: crash before commit")

    with pytest.raises(RuntimeError, match="boom"):
        _ingest_then_crash()

    # Atomic with the decision: no signal, no provenance row.
    assert store.list_signals("BTCUSD") == []
    assert _contexts(store) == []


def test_inmemory_store_persist_is_noop() -> None:
    """The in-memory test double keeps the no-op default — provenance is a
    drop-in and must never raise where there is no schema to write to."""
    store = InMemoryScoreStore()
    assert store.persist_decision_context(signal_id="ext-noop", symbol="BTCUSD") is None
