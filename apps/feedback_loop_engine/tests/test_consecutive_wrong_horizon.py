"""FB-2: the consecutive-wrong tracker is keyed per (strategy, instrument,
horizon), so the six evaluation horizons keep independent counters instead of
corrupting one shared row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lib_application.db.models import (
    Base,
    StrategyConsecutiveWrongTracker,
    StrategyParameterFeedback,
)
from lib_infrastructure.persistence.sqlalchemy.repositories.signal_performance_repo import (
    SQLAlchemySignalPerformanceRepository,
)

TS = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _repo() -> SQLAlchemySignalPerformanceRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return SQLAlchemySignalPerformanceRepository(
        session_factory=sessionmaker(engine, expire_on_commit=False)
    )


def _repo_and_sf() -> tuple[SQLAlchemySignalPerformanceRepository, sessionmaker]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    return SQLAlchemySignalPerformanceRepository(session_factory=sf), sf


def _add_feedback(
    sf: sessionmaker,
    *,
    instr_id: int,
    horizon: str = "1d",
    created_at: datetime | None = None,
) -> int:
    with sf() as session:
        feedback = StrategyParameterFeedback(
            strategy_id="s1",
            instr_id=instr_id,
            horizon=horizon,
            trigger_reason="consecutive_wrong",
            current_params={},
            suggested_params={},
            status="pending",
            created_at=created_at or datetime.now(tz=UTC),
        )
        session.add(feedback)
        session.commit()
        return int(feedback.feedback_id)


def _wrong(repo, signal_id: int, horizon: str):
    return repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=None,
        instr_id=1,
        signal_id=signal_id,
        signal_ts=TS,
        is_correct=False,
        wrong_threshold=2,
        horizon=horizon,
    )


def test_horizons_keep_independent_counters() -> None:
    repo = _repo()
    _wrong(repo, 1, "1d")
    t1d = _wrong(repo, 2, "1d")  # 1d now at 2
    t1h = _wrong(repo, 3, "1h")  # 1h independently at 1

    assert t1d.consecutive_wrong_count == 2
    assert t1d.threshold_reached is True
    # Before FB-2 the shared row would read 3; now 1h is its own counter.
    assert t1h.consecutive_wrong_count == 1
    assert t1h.threshold_reached is False


def test_correct_on_one_horizon_does_not_reset_another() -> None:
    repo = _repo()
    _wrong(repo, 1, "1d")
    _wrong(repo, 2, "1d")  # 1d at 2
    # A correct prediction on 1h must not reset the 1d counter.
    repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=None,
        instr_id=1,
        signal_id=3,
        signal_ts=TS,
        is_correct=True,
        wrong_threshold=2,
        horizon="1h",
    )
    t1d = _wrong(repo, 4, "1d")  # still counting up on 1d
    assert t1d.consecutive_wrong_count == 3


def test_pending_optimization_is_per_instrument() -> None:
    # FB-3: a pending suggestion for one instrument must not block another.
    repo, sf = _repo_and_sf()
    _add_feedback(sf, instr_id=1)
    assert repo.has_pending_optimization("s1", 1) is True
    assert repo.has_pending_optimization("s1", 2) is False


def test_pending_optimization_is_per_horizon() -> None:
    repo, sf = _repo_and_sf()
    _add_feedback(sf, instr_id=1, horizon="1h")

    assert repo.has_pending_optimization("s1", 1, "1h") is True
    assert repo.has_pending_optimization("s1", 1, "1d") is False


def test_stale_pending_optimization_is_expired_and_unblocks() -> None:
    # FB-3: an un-reviewed pending suggestion older than max_age_days is expired
    # so it no longer blocks re-optimization.
    repo, sf = _repo_and_sf()
    _add_feedback(sf, instr_id=1, created_at=datetime.now(tz=UTC) - timedelta(days=30))

    assert repo.has_pending_optimization("s1", 1, max_age_days=14) is False
    with sf() as session:
        row = session.query(StrategyParameterFeedback).filter_by(strategy_id="s1").one()
        assert row.status == "expired"


def test_threshold_one_triggers_on_first_wrong_prediction() -> None:
    repo = _repo()

    tracker = repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=None,
        instr_id=1,
        signal_id=1,
        signal_ts=TS,
        is_correct=False,
        wrong_threshold=1,
        horizon="1h",
    )

    assert tracker.consecutive_wrong_count == 1
    assert tracker.threshold_reached is True
    assert tracker.threshold_reached_at is not None


def test_replayed_signal_does_not_increment_tracker_twice() -> None:
    repo = _repo()

    first = _wrong(repo, 1, "1h")
    replay = _wrong(repo, 1, "1h")

    assert first.consecutive_wrong_count == 1
    assert replay.consecutive_wrong_count == 1
    assert replay.last_signal_id == 1


def test_late_older_signal_cannot_rewind_tracker() -> None:
    repo = _repo()
    newer = repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=10,
        instr_id=1,
        signal_id=2,
        signal_ts=TS + timedelta(minutes=1),
        is_correct=False,
        wrong_threshold=2,
        horizon="1h",
    )
    stale = repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=9,
        instr_id=1,
        signal_id=1,
        signal_ts=TS,
        is_correct=True,
        wrong_threshold=2,
        horizon="1h",
    )

    assert newer.last_signal_id == 2
    assert stale.last_signal_id == 2
    assert stale.strat_ver_id == 10
    assert stale.consecutive_wrong_count == 1
    assert stale.threshold_reached is False


def test_strategy_version_change_resets_tracker_state() -> None:
    repo = _repo()
    repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=10,
        instr_id=1,
        signal_id=1,
        signal_ts=TS,
        is_correct=False,
        wrong_threshold=2,
        horizon="1h",
    )
    reached = repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=10,
        instr_id=1,
        signal_id=2,
        signal_ts=TS,
        is_correct=False,
        wrong_threshold=2,
        horizon="1h",
    )
    assert reached.threshold_reached is True

    next_version = repo.update_consecutive_wrong_tracker(
        strategy_id="s1",
        strat_ver_id=11,
        instr_id=1,
        signal_id=3,
        signal_ts=TS,
        is_correct=False,
        wrong_threshold=2,
        horizon="1h",
    )

    assert next_version.strat_ver_id == 11
    assert next_version.consecutive_wrong_count == 1
    assert next_version.threshold_reached is False
    assert next_version.feedback_id is None


def test_feedback_link_updates_only_the_triggering_horizon() -> None:
    repo, sf = _repo_and_sf()
    for horizon, signal_offset in (("1h", 0), ("1d", 10)):
        repo.update_consecutive_wrong_tracker(
            strategy_id="s1",
            strat_ver_id=None,
            instr_id=1,
            signal_id=signal_offset + 1,
            signal_ts=TS,
            is_correct=False,
            wrong_threshold=2,
            horizon=horizon,
        )
        repo.update_consecutive_wrong_tracker(
            strategy_id="s1",
            strat_ver_id=None,
            instr_id=1,
            signal_id=signal_offset + 2,
            signal_ts=TS,
            is_correct=False,
            wrong_threshold=2,
            horizon=horizon,
        )
    feedback_id = _add_feedback(sf, instr_id=1, horizon="1h")

    assert repo.link_feedback_to_tracker(
        strategy_id="s1",
        instr_id=1,
        horizon="1h",
        feedback_id=feedback_id,
    )

    with sf() as session:
        trackers = {
            row.horizon: row.feedback_id
            for row in session.query(StrategyConsecutiveWrongTracker).all()
        }
    assert trackers == {"1h": feedback_id, "1d": None}
    unlinked = repo.list_unlinked_reached_trackers()
    assert [(row.horizon, row.feedback_id) for row in unlinked] == [("1d", None)]
