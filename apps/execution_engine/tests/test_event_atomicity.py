"""Command-to-result tracing survives without the retired execution.results event."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.execution_log_store import ExecutionLogStore
from lib_application.db.models import Base, ExecutionLog, OutboxEvent


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _log_kwargs(**details: Any) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "account_id": 1,
        "strategy_id": "swing_high_low_pmo_v1",
        "signal_type": "long",
        "execution_mode": "paper",
        "status": "executed",
        "details": {"orders_submitted": 1, **details},
    }


def test_log_persists_causation_event_id_and_enqueues_nothing() -> None:
    session_local = _session_local()
    store = ExecutionLogStore(session_factory=session_local)

    store.log(**_log_kwargs(causation_event_id="evt-command-1"))

    with session_local() as s:
        row = s.query(ExecutionLog).one()
        assert row.execution_details["causation_event_id"] == "evt-command-1"
        assert s.query(OutboxEvent).count() == 0


def test_log_no_longer_accepts_an_outbox_event_message() -> None:
    store = ExecutionLogStore(session_factory=_session_local())
    with pytest.raises(TypeError):
        store.log(**_log_kwargs(), outbox_store=object(), event_message={})
