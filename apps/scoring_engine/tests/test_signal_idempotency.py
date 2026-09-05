"""Canonical-signal writes are idempotent on the originating signal identity (M10).

A NOTIFY redelivery, worker restart, or outbox retry can re-POST the same signal.
``add_signal`` must UPDATE the existing canonical_signals row (keyed on the unique
``external_signal_id``) rather than insert a duplicate — the restart-safety
contract. The Postgres path uses an atomic INSERT ... ON CONFLICT DO UPDATE
(concurrency-safe); this locks the sqlite SELECT-then-update path.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from scoring_engine.models import SignalRecord
from scoring_engine.storage import AppScoreStore

NOW = dt.datetime(2026, 6, 28, 12, 0, tzinfo=dt.UTC)


def _signal(score: float, ext_id: str | None) -> SignalRecord:
    return SignalRecord(
        strategy_version="1.0.0",
        signal_id="ext-uuid",
        strategy_id="test_strategy_alpha_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action="long",
        confidence=0.7,
        score_value=score,
        timestamp=NOW,
        external_signal_id=ext_id,
    )


def _rows(store: AppScoreStore) -> list[app_models.CanonicalSignal]:
    with Session(store._engine) as s:
        return s.query(app_models.CanonicalSignal).all()


def test_redelivered_signal_updates_not_duplicates(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    first_id = store.add_signal(_signal(0.40, "sig-1"))
    redelivered_id = store.add_signal(_signal(0.90, "sig-1"))
    rows = _rows(store)
    assert len(rows) == 1  # updated in place, no duplicate
    assert float(rows[0].raw_score) == 0.90  # latest value won
    assert first_id == redelivered_id == rows[0].signal_id


def test_distinct_signals_stay_separate(app_store_with_btcusd: AppScoreStore) -> None:
    store = app_store_with_btcusd
    first_id = store.add_signal(_signal(0.40, "sig-a"))
    second_id = store.add_signal(_signal(0.60, "sig-b"))
    assert len(_rows(store)) == 2
    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id


def test_identity_less_signal_fails_closed(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    with pytest.raises(ValueError, match="has no canonical external_signal_id"):
        store.add_signal(_signal(0.40, None))

    assert _rows(store) == []
