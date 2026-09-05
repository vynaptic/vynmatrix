"""SC-6: asset-score writes are idempotent on the originating signal identity.

A re-delivered signal (same external_signal_id) must UPDATE its asset_scores row
rather than insert a duplicate — duplicates bloat the table and double-count
alpha_raw in the SC-4 boot warm. Distinct signals (different ids, incl. two
strategies on the same bar) stay separate, while identity-less scores fail
closed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from lib_application.db import models as app_models
from scoring_engine.models import ScoreComponent, ScoreRecord
from scoring_engine.storage import AppScoreStore

NOW = dt.datetime(2026, 6, 27, 12, 0, tzinfo=dt.UTC)


def _record(score: float, ext_id: str | None, *, alpha: float = 0.5) -> ScoreRecord:
    return ScoreRecord(
        target="BTCUSD",
        scope="asset",
        score=score,
        components=[
            ScoreComponent(
                strategy_id="alpha",
                weight=1.0,
                raw_value=score,
                weighted_value=score,
                confidence=0.8,
                timestamp=NOW,
            )
        ],
        computed_at=NOW,
        metadata={"alpha_raw": alpha},
        external_signal_id=ext_id,
    )


def _asset_rows(store: AppScoreStore) -> list[app_models.AssetScore]:
    with Session(store._engine) as s:
        return s.query(app_models.AssetScore).all()


def test_reposted_signal_updates_not_duplicates(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    store.upsert_score(_record(0.40, "sig-1"))
    store.upsert_score(_record(0.75, "sig-1"))  # same signal re-delivered, new score

    rows = _asset_rows(store)
    assert len(rows) == 1  # updated in place, no duplicate
    assert float(rows[0].score_value) == 0.75  # latest value won


def test_distinct_signals_stay_separate(app_store_with_btcusd: AppScoreStore) -> None:
    store = app_store_with_btcusd
    # Two strategies score the same bar -> distinct ids -> two legitimate rows.
    store.upsert_score(_record(0.40, "sig-a"))
    store.upsert_score(_record(0.60, "sig-b"))
    assert len(_asset_rows(store)) == 2


def test_identity_less_signal_fails_closed(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    with pytest.raises(ValueError, match="has no external_signal_id"):
        store.upsert_score(_record(0.40, None))

    assert _asset_rows(store) == []


def test_warm_history_not_double_counted_on_repost(
    app_store_with_btcusd: AppScoreStore,
) -> None:
    store = app_store_with_btcusd
    store.upsert_score(_record(0.40, "sig-1", alpha=0.5))
    store.upsert_score(_record(0.75, "sig-1", alpha=0.9))  # re-delivery

    history = store.recent_asset_alpha_history(window=100)
    # One signal -> one alpha sample (the latest), not two.
    assert history == {"BTC/USD": [0.9]}
