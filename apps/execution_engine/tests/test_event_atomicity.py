"""Transactional-outbox atomicity for execution + feedback events (H-5).

The execution result event must be co-committed with the execution_logs row, and
the feedback event with the SignalPerformance row, so a crash can never persist a
business record without its downstream event (or vice-versa).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.execution_log_store import ExecutionLogStore
from lib_application.db.models import Base, ExecutionLog, OutboxEvent
from lib_application.outbox import OutboxStore


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _event_message() -> dict[str, Any]:
    return {
        "topic": "execution.results",
        "event_type": "ExecutionResultEvent",
        "payload": {"signal_id": "sig-1", "success": True},
        "aggregate_type": "execution_result",
        "aggregate_id": "sig-1:u1",
        "event_key": "execution-result:sig-1:u1",
        "ordering_key": "u1",
        "headers": {"run_id": "run-1"},
    }


def _log_kwargs() -> dict[str, Any]:
    return {
        "user_id": "u1",
        "account_id": 1,
        "strategy_id": "swing_high_low_pmo_v1",
        "signal_type": "long",
        "execution_mode": "paper",
        "status": "executed",
        "details": {"orders_submitted": 1},
    }


def test_log_and_result_event_commit_together() -> None:
    session_local = _session_local()
    store = ExecutionLogStore(session_factory=session_local)
    outbox = OutboxStore(session_factory=session_local)

    store.log(**_log_kwargs(), outbox_store=outbox, event_message=_event_message())

    with session_local() as s:
        assert s.query(ExecutionLog).count() == 1
        assert s.query(OutboxEvent).filter(OutboxEvent.topic == "execution.results").count() == 1


def test_log_rolls_back_when_event_enqueue_fails() -> None:
    # If the outbox enqueue raises, the execution_logs row must NOT persist —
    # the two are one unit of work.
    session_local = _session_local()
    store = ExecutionLogStore(session_factory=session_local)

    class _FailingOutbox:
        def enqueue_on_session(self, _session: Any, **_: Any) -> str:
            raise RuntimeError("enqueue boom")

    raised = False
    try:
        store.log(**_log_kwargs(), outbox_store=_FailingOutbox(), event_message=_event_message())
    except RuntimeError:
        raised = True

    assert raised
    with session_local() as s:
        # Neither the log row nor any outbox row was committed.
        assert s.query(ExecutionLog).count() == 0
        assert s.query(OutboxEvent).count() == 0
