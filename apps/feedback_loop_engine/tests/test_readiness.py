"""Feedback /ready reflects DB connectivity (G14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from feedback_loop_engine.api import _engine_db_ready, create_app
from lib_application.db.models import ServiceHeartbeat
from lib_application.db.session import create_engine_for_env, dispose_engine
from lib_application.services.heartbeat_store import HeartbeatStore


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


@pytest.mark.parametrize("heartbeat", ["missing", "stale", "degraded", "future", "fresh"])
def test_daemon_readiness_requires_recent_successful_durable_progress(
    tmp_path, monkeypatch, heartbeat
) -> None:
    monkeypatch.setenv("FEEDBACK_RUN_MODE", "daemon")
    monkeypatch.setenv("EVALUATION_INTERVAL", "300")
    monkeypatch.setenv("API_KEY", "test-feedback-key")
    engine = create_engine_for_env(db_url=f"sqlite:///{tmp_path / 'feedback.sqlite'}")
    ServiceHeartbeat.__table__.create(engine)
    try:
        if heartbeat != "missing":
            timestamp = datetime.now(tz=UTC)
            if heartbeat == "stale":
                timestamp -= timedelta(hours=1)
            elif heartbeat == "future":
                timestamp += timedelta(hours=1)
            HeartbeatStore(engine).record(
                service_name="feedback_loop_engine",
                now=timestamp,
                status="degraded" if heartbeat == "degraded" else "ok",
            )
        with TestClient(create_app(SimpleNamespace(_engine=engine), None)) as client:
            assert client.get("/health").status_code == 200
            response = client.get("/ready")
            assert response.status_code == (200 if heartbeat == "fresh" else 503)
            assert response.json()["checks"]["evaluation_progress"] is (heartbeat == "fresh")
    finally:
        dispose_engine(engine)
