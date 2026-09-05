"""Database engines apply the same bounded pool policy to explicit service URLs."""

from __future__ import annotations

import pytest

from lib_application.db.session import _postgres_pool_options


def test_postgres_pool_defaults_are_small_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "DB_POOL_RECYCLE_SECONDS",
        "DB_POOL_CONNECTION_BUDGET",
    ):
        monkeypatch.delenv(name, raising=False)

    assert _postgres_pool_options() == {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 10,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
    }


def test_postgres_pool_honours_explicit_runtime_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "600")
    monkeypatch.setenv("DB_POOL_CONNECTION_BUDGET", "5")

    options = _postgres_pool_options()

    assert options["pool_size"] == 4
    assert options["max_overflow"] == 1
    assert options["pool_timeout"] == 7
    assert options["pool_recycle"] == 600


def test_postgres_pool_rejects_capacity_above_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "4")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("DB_POOL_CONNECTION_BUDGET", "5")

    with pytest.raises(ValueError, match="exceeds DB_POOL_CONNECTION_BUDGET"):
        _postgres_pool_options()


def test_postgres_pool_rejects_invalid_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "many")

    with pytest.raises(ValueError, match="Invalid integer value"):
        _postgres_pool_options()
