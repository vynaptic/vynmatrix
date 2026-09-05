"""Canonical transactional-outbox events for persisted scoring outputs."""

from __future__ import annotations

from lib_common.internal_events import (
    CanonicalSignalEvent,
    ScoreContextSnapshot,
    ScoredSignalEvent,
)
from lib_strategy.signals.signal import Signal

from .models import ScoreRecord
from .snapshot_utils import signal_snapshot
from .storage_base import ScoreStore


def enqueue_scoring_pipeline_events(
    *,
    store: ScoreStore,
    signal: Signal,
    score: ScoreRecord,
    queued_users: list[str] | None = None,
) -> tuple[str, str]:
    """Enqueue canonical and scored events on the caller's active transaction."""

    canonical_signal_db_id: int | None = None
    raw_canonical_signal_id = (signal.metadata or {}).get("canonical_signal_id")
    if raw_canonical_signal_id is not None:
        try:
            canonical_signal_db_id = int(raw_canonical_signal_id)
        except (TypeError, ValueError):
            canonical_signal_db_id = None

    signal_event = CanonicalSignalEvent(
        run_id=signal.run_id,
        correlation_id=signal.signal_id,
        producer="scoring_engine",
        signal=signal_snapshot(signal),
        canonical_signal_db_id=canonical_signal_db_id,
    )
    signal_event_id = store.enqueue_event(
        topic=signal_event.topic,
        event_type=signal_event.event_type,
        payload=signal_event.model_dump(mode="json"),
        schema_version=signal_event.schema_version,
        aggregate_type="canonical_signal",
        aggregate_id=signal.signal_id,
        event_key=f"canonical-signal:{signal.signal_id}",
        ordering_key=signal.symbol,
        headers={"run_id": signal.run_id or ""},
    )

    score_event = ScoredSignalEvent(
        run_id=signal.run_id,
        correlation_id=signal.signal_id,
        producer="scoring_engine",
        signal=signal_snapshot(signal),
        score_context=ScoreContextSnapshot(
            asset_score=float(score.score),
            score_scope=score.scope,
            score_target=score.target,
        ),
        queued_users=list(queued_users or []),
    )
    score_event_id = store.enqueue_event(
        topic=score_event.topic,
        event_type=score_event.event_type,
        payload=score_event.model_dump(mode="json"),
        schema_version=score_event.schema_version,
        aggregate_type="scored_signal",
        aggregate_id=signal.signal_id,
        event_key=f"scored-signal:{signal.signal_id}",
        ordering_key=signal.symbol,
        headers={"run_id": signal.run_id or ""},
    )
    return signal_event_id, score_event_id


__all__ = ["enqueue_scoring_pipeline_events"]
