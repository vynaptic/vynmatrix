"""The scoring process holds one bounded pool, not a store pool plus a session pool."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from scoring_engine import main as scoring_main
from scoring_engine.storage import AppScoreStore

_SQLITE = "sqlite+pysqlite:///:memory:"


def _memory_engine() -> Any:
    return create_engine(
        _SQLITE, future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def test_postgres_store_pool_is_bounded_by_the_deployment_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_CONNECTION_BUDGET", "2")
    # Construction never connects, so an unroutable port is safe here.
    store = AppScoreStore("postgresql://vm_scoring_login:secret@127.0.0.1:1/vm_trading")
    pool = store.engine.pool
    assert pool.size() == 2
    assert pool._max_overflow == 0  # SQLAlchemy exposes no public overflow getter
    assert pool._pre_ping is True


def test_injected_engine_is_reused_instead_of_opening_a_second_pool() -> None:
    engine = _memory_engine()
    store = AppScoreStore(_SQLITE, engine=engine)
    assert store.engine is engine


def test_main_shares_one_engine_between_store_and_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _memory_engine()
    seen: dict[str, Any] = {}

    class _Engine:
        store = object()

    def _build_engine(_config: Any, *, db_engine: Any = None) -> _Engine:
        seen["store_engine"] = db_engine
        return _Engine()

    def _session_factory(**kwargs: Any) -> object:
        seen["factory_engine"] = kwargs.get("engine")
        seen["factory_url"] = kwargs.get("db_url")
        return object()

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")  # config accepts the bare scheme
    monkeypatch.delenv("EXEC_ENGINE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["scoring-engine"])
    monkeypatch.setattr(scoring_main, "setup_logging", lambda _level: None)
    monkeypatch.setattr(scoring_main, "init_tracing", lambda _name: None)
    monkeypatch.setattr(scoring_main, "create_engine_for_env", lambda **_: engine)
    monkeypatch.setattr(scoring_main, "build_engine", _build_engine)
    monkeypatch.setattr(scoring_main, "get_session_factory", _session_factory)
    monkeypatch.setattr(scoring_main, "DBProfileProvider", lambda _factory: object())
    monkeypatch.setattr(scoring_main, "DBStrategyConfigProvider", lambda _factory: object())
    monkeypatch.setattr(scoring_main, "_make_lifespan", lambda *_a, **_k: None)
    monkeypatch.setattr(scoring_main, "create_app", lambda *_a, **_k: object())
    monkeypatch.setattr(scoring_main.uvicorn, "run", lambda *_a, **_k: None)

    scoring_main.main()
    try:
        assert seen["store_engine"] is engine
        assert seen["factory_engine"] is engine
        assert seen["factory_url"] is None
        assert scoring_main._runtime.db_engine is engine
        asyncio.run(scoring_main.cleanup_dispatcher())
        assert scoring_main._runtime.db_engine is None
    finally:
        scoring_main._runtime.db_engine = None
        scoring_main._runtime.dispatcher = None
