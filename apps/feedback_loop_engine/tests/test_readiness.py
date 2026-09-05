"""Feedback /ready reflects DB connectivity (G14)."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from feedback_loop_engine.api import _engine_db_ready


def test_healthy_engine_is_ready() -> None:
    sa_engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    assert _engine_db_ready(SimpleNamespace(_engine=sa_engine)) is True


def test_missing_engine_is_ready() -> None:
    assert _engine_db_ready(SimpleNamespace(_engine=None)) is True


class _DeadEngine:
    def connect(self):
        raise SQLAlchemyError("connection pool exhausted")


def test_dead_engine_is_not_ready() -> None:
    assert _engine_db_ready(SimpleNamespace(_engine=_DeadEngine())) is False
