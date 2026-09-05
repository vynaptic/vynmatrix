"""C4: the scoring write path is a single transaction.

A signal, its scores, and the outbox execution command must persist
all-or-nothing. Before this change OutboxStore.enqueue committed in its own
session, separate from the signal/score writes — so a crash after the score
commit but before the enqueue left a persisted, scored signal with no
execution command (silent signal loss). unit_of_work() now wraps the whole
ingest + dispatch, so they commit together or roll back together.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lib_strategy.signals.signal import Signal, SignalAction
from scoring_engine.engine import ScoreEngine
from scoring_engine.storage import AppScoreStore

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


def test_unit_of_work_commits_signal_and_command_together(provision_scoring_catalogue) -> None:
    store, engine = _store_and_engine(provision_scoring_catalogue)

    with store.unit_of_work():
        engine.ingest_signal(_signal("ext-commit-1"))
        store.enqueue_event(
            topic="execution.commands",
            event_type="execution.command",
            payload={"hello": "world"},
            event_key="cmd-commit-1",
        )

    assert len(store.list_signals("BTCUSD")) == 1
    claimed = store.claim_outbox_batch(topics=["execution.commands"], consumer="t")
    assert len(claimed) == 1


def test_unit_of_work_rolls_back_signal_and_command_together(
    provision_scoring_catalogue,
) -> None:
    store, engine = _store_and_engine(provision_scoring_catalogue)

    def _ingest_then_crash() -> None:
        with store.unit_of_work():
            engine.ingest_signal(_signal("ext-rollback-1"))
            store.enqueue_event(
                topic="execution.commands",
                event_type="execution.command",
                payload={"hello": "world"},
                event_key="cmd-rollback-1",
            )
            # Simulated crash after the writes but before the transaction commits.
            raise RuntimeError("boom: crash before commit")

    with pytest.raises(RuntimeError, match="boom"):
        _ingest_then_crash()

    # All-or-nothing: neither the signal nor the outbox command survived.
    assert store.list_signals("BTCUSD") == []
    assert store.claim_outbox_batch(topics=["execution.commands"], consumer="t") == []


def test_standalone_enqueue_still_commits_without_unit_of_work(
    provision_scoring_catalogue,
) -> None:
    """Callers outside a unit_of_work (e.g. the relay's own enqueue) still get
    their own committed transaction — behaviour preserved."""
    store, _ = _store_and_engine(provision_scoring_catalogue)

    store.enqueue_event(
        topic="execution.commands",
        event_type="execution.command",
        payload={"hello": "world"},
        event_key="cmd-standalone-1",
    )

    claimed = store.claim_outbox_batch(topics=["execution.commands"], consumer="t")
    assert len(claimed) == 1
