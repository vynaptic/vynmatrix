"""Feedback heartbeat: the one-shot loop records DB liveness each cycle.

The feedback loop exits between cycles, so an in-process gauge is never scraped.
A successful cycle writes a service_heartbeats row that a monitor reads to alert
on staleness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

import feedback_loop_engine.main as feedback_main
from feedback_loop_engine.engine import FeedbackLoopEngine
from feedback_loop_engine.main import _refresh_mode_performance
from feedback_loop_engine.mode_performance import ModePerformanceIntegrityError
from feedback_loop_engine.models import EvaluationHorizon
from lib_application.db.models import Base, ServiceHeartbeat
from lib_application.services.heartbeat_store import HeartbeatStore

T0 = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _engine():  # type: ignore[no-untyped-def]
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def test_record_creates_then_updates_row() -> None:
    store = HeartbeatStore(_engine())
    store.record(service_name="svc", detail="first", now=T0)
    assert store.last_success_age_seconds("svc", now=T0) == 0.0

    later = T0 + timedelta(seconds=90)
    store.record(service_name="svc", detail="second", now=later)
    # Upsert moved the timestamp forward (not a second row).
    assert store.last_success_age_seconds("svc", now=later) == 0.0
    assert store.last_success_age_seconds("svc", now=later + timedelta(seconds=30)) == 30.0


def test_age_none_when_never_recorded() -> None:
    store = HeartbeatStore(_engine())
    assert store.last_success_age_seconds("never") is None


def test_feedback_cycle_records_heartbeat_even_when_empty() -> None:
    # A cycle with no pending signals still completed successfully -> heartbeat.
    eng = _engine()
    feedback = FeedbackLoopEngine(engine=eng)
    result = feedback.run_evaluation_cycle(horizon=EvaluationHorizon.D1, limit=10)
    assert result["signals_evaluated"] == 0

    store = HeartbeatStore(eng)
    age = store.last_success_age_seconds("feedback_loop_engine")
    assert age is not None  # the cycle wrote a heartbeat
    # The detail captures the horizon that ran.
    from sqlalchemy.orm import Session

    with Session(eng) as s:
        row = s.get(ServiceHeartbeat, "feedback_loop_engine")
        assert row is not None
        assert "horizon=1d" in (row.detail or "")
        assert row.last_status == "ok"


def test_aggregate_heartbeat_preserves_degraded_run_status() -> None:
    eng = _engine()
    feedback = FeedbackLoopEngine(engine=eng)

    feedback.record_run_heartbeat(
        horizons=[EvaluationHorizon.MIN15, EvaluationHorizon.H1],
        results={"signals_evaluated": 3, "errors": 1},
    )

    from sqlalchemy.orm import Session

    with Session(eng) as session:
        row = session.get(ServiceHeartbeat, "feedback_loop_engine")
        assert row is not None
        assert row.last_status == "degraded"
        assert row.detail == "horizons=15min,1h evaluated=3 errors=1"


def test_later_empty_horizon_cannot_erase_degraded_cycle_diagnostic() -> None:
    eng = _engine()
    feedback = FeedbackLoopEngine(engine=eng)
    feedback._record_cycle_heartbeat(
        horizon=EvaluationHorizon.MIN15,
        results={"signals_evaluated": 1, "errors": 1},
    )

    feedback.run_evaluation_cycle(horizon=EvaluationHorizon.H1, limit=10)

    from sqlalchemy.orm import Session

    with Session(eng) as session:
        row = session.get(ServiceHeartbeat, "feedback_loop_engine")
        assert row is not None
        assert row.last_status == "degraded"
        assert row.detail == "horizon=15min evaluated=1 errors=1"


def test_successful_aggregate_run_clears_prior_cycle_degradation() -> None:
    eng = _engine()
    feedback = FeedbackLoopEngine(engine=eng)
    feedback._record_cycle_heartbeat(
        horizon=EvaluationHorizon.MIN15,
        results={"signals_evaluated": 1, "errors": 1},
    )

    feedback.record_run_heartbeat(
        horizons=[EvaluationHorizon.MIN15, EvaluationHorizon.H1],
        results={"signals_evaluated": 2, "errors": 0},
    )

    from sqlalchemy.orm import Session

    with Session(eng) as session:
        row = session.get(ServiceHeartbeat, "feedback_loop_engine")
        assert row is not None
        assert row.last_status == "ok"
        assert row.detail == "horizons=15min,1h evaluated=2 errors=0"


def test_mode_performance_failure_degrades_the_aggregate_run() -> None:
    class _BrokenWriter:
        @staticmethod
        def update_mode_performance() -> int:
            msg = "invalid realized-trade lineage"
            raise ModePerformanceIntegrityError(msg)

    totals = {"signals_evaluated": 3, "errors": 0}
    assert _refresh_mode_performance(_BrokenWriter(), totals=totals) == 0  # type: ignore[arg-type]
    assert totals == {"signals_evaluated": 3, "errors": 1}


def test_evaluate_command_exits_nonzero_and_records_degraded_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, object]] = []

    class _DegradedFeedback:
        @staticmethod
        def run_evaluation_cycle(
            *,
            horizon: EvaluationHorizon,
            limit: int,
        ) -> dict[str, int]:
            assert horizon is EvaluationHorizon.H1
            assert limit == 100
            return {
                "signals_evaluated": 1,
                "correct_predictions": 0,
                "wrong_predictions": 1,
                "optimizations_triggered": 0,
                "skipped_no_price": 0,
                "errors": 1,
            }

        @staticmethod
        def update_mode_performance() -> int:
            return 0

        @staticmethod
        def record_run_heartbeat(
            *,
            horizons: list[EvaluationHorizon],
            results: dict[str, int],
        ) -> None:
            recorded.append({"horizons": horizons, "results": dict(results)})

    feedback = _DegradedFeedback()
    monkeypatch.setattr(feedback_main, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        feedback_main,
        "create_engine_instance",
        lambda: (feedback, object(), object(), object()),
    )
    monkeypatch.setattr(
        feedback_main,
        "get_feedback_evaluation_horizons",
        lambda: [EvaluationHorizon.H1],
    )
    monkeypatch.setattr(feedback_main, "dispose_engine", lambda _engine: None)
    monkeypatch.setattr(feedback_main.sys, "argv", ["feedback-loop-engine", "evaluate"])

    with pytest.raises(SystemExit) as exc_info:
        feedback_main.main()

    assert exc_info.value.code == 1
    assert recorded == [
        {
            "horizons": [EvaluationHorizon.H1],
            "results": {
                "signals_evaluated": 1,
                "correct_predictions": 0,
                "wrong_predictions": 1,
                "optimizations_triggered": 0,
                "skipped_no_price": 0,
                "errors": 1,
            },
        }
    ]
