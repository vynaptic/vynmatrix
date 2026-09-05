"""Execution readiness gates on the DB, not just reconciliation health.

An execution instance whose DB pool is dead can neither dedup nor persist
orders/decision logs, so it must report not-ready (503) rather than accept
/execute-command traffic it cannot serve. Mirrors the scoring/feedback
``SELECT 1`` readiness probes (G14).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from execution_engine.api import (
    _execution_db_ready,
    _execution_progress_ready,
    create_app,
)
from lib_application.db.models import Base, InstrumentPrice, PendingOrder
from lib_common.config_validation import ExecutionPaperConfig, ExecutionRuntimeConfig


def test_db_ready_false_without_session_factory() -> None:
    """A deployed execution service cannot operate without its canonical ledger."""
    engine: Any = type("E", (), {})()
    assert _execution_db_ready(engine) is False


def test_db_ready_true_with_live_db() -> None:
    sa_engine = create_engine("sqlite+pysqlite:///:memory:")
    engine: Any = type("E", (), {})()
    engine._session_factory = sessionmaker(bind=sa_engine)
    assert _execution_db_ready(engine) is True


def test_db_ready_false_on_db_error() -> None:
    class _BoomSession:
        def __enter__(self) -> _BoomSession:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, *_a: object, **_k: object) -> None:
            msg = "dead pool"
            raise SQLAlchemyError(msg)

    engine: Any = type("E", (), {})()
    engine._session_factory = lambda: _BoomSession()
    assert _execution_db_ready(engine) is False


def test_readiness_fails_after_paper_position_rehydration_failure() -> None:
    engine: Any = type(
        "E",
        (),
        {
            "is_reconciliation_healthy": lambda self: True,
            "is_paper_rehydration_healthy": lambda self: False,
        },
    )()

    response = TestClient(create_app(engine)).get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {
            "reconciliation": True,
            "database": False,
            "paper_position_rehydration": False,
            "paper_order_lifecycle": True,
            "unknown_submissions_clear": False,
            "paper_order_progress": False,
        },
    }


def test_readiness_fails_when_paper_order_lifecycle_is_unhealthy() -> None:
    engine = _progress_engine()
    engine.is_reconciliation_healthy = lambda: True
    engine.is_paper_rehydration_healthy = lambda: True
    engine.paper_order_lifecycle_status = lambda: {
        "healthy": False,
        "failed_orders": 1,
        "last_error": "order-1 malformed",
    }

    response = TestClient(create_app(engine)).get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["paper_order_lifecycle"] is False


def _progress_engine() -> Any:
    sa_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(sa_engine)
    engine: Any = type("E", (), {})()
    engine._session_factory = sessionmaker(bind=sa_engine)
    engine._runtime_config = ExecutionRuntimeConfig(
        paper=ExecutionPaperConfig(max_order_processing_lag_seconds=60)
    )
    engine.reconciliation_status = lambda: {
        "initial_reconciliation_complete": True,
        "discovered_partitions": 1,
    }
    return engine


def _pending_order(*, status: str, created_at: datetime) -> PendingOrder:
    return PendingOrder(
        order_id=f"order-{status}",
        client_order_id=f"client-{status}",
        user_id="user-1",
        signal_id="signal-1",
        instr_id=1,
        broker_account_id=101,
        symbol="BTC-USDC",
        settlement_currency="USDC",
        side="SELL",
        order_type="stop",
        quantity=Decimal("1"),
        trigger_price=Decimal("90000"),
        execution_mode="paper",
        broker="paper",
        broker_environment="paper",
        status=status,
        market_data_source="coinbase_live",
        market_data_timeframe="1m",
        created_at=created_at,
    )


def test_progress_readiness_blocks_ambiguous_submission() -> None:
    engine = _progress_engine()
    now = datetime.now(tz=UTC)
    with engine._session_factory() as session:
        session.add(_pending_order(status="submission_unknown", created_at=now))
        session.commit()

    assert _execution_progress_ready(engine) == {
        "unknown_submissions_clear": False,
        "paper_order_progress": True,
    }


def test_progress_readiness_blocks_stale_committed_candle_backlog() -> None:
    engine = _progress_engine()
    now = datetime.now(tz=UTC).replace(microsecond=0)
    with engine._session_factory() as session:
        pending = _pending_order(status="working", created_at=now - timedelta(minutes=3))
        pending.last_market_data_ts = now - timedelta(minutes=3)
        pending.last_market_data_revision = 1
        session.add(pending)
        session.add(
            InstrumentPrice(
                price_id=41,
                instr_id=1,
                ts=(now - timedelta(minutes=1)).replace(tzinfo=None),
                timeframe="1m",
                source="coinbase_live",
                content_revision=1,
                close=Decimal("100000"),
            )
        )
        session.commit()

    assert _execution_progress_ready(engine) == {
        "unknown_submissions_clear": True,
        "paper_order_progress": False,
    }
