"""PgNotifyListener: libpq DSN normalization and acknowledged-LISTEN state."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from lib_data.pg_notify import PgNotifyListener, libpq_dsn


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql+psycopg2://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db?sslmode=require", "postgresql://u:p@h/db?sslmode=require"),
        ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgres://u:p@h/db", "postgres://u:p@h/db"),
    ],
)
def test_libpq_dsn_strips_only_the_sqlalchemy_driver_suffix(given: str, expected: str) -> None:
    assert libpq_dsn(given) == expected


class _Cursor:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._statements.append(statement)


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements
        self.closed = False

    def set_isolation_level(self, _level: int) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.statements)

    def close(self) -> None:
        self.closed = True


def test_connect_hands_libpq_a_normalized_dsn_and_tracks_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    statements: list[str] = []

    fake = types.ModuleType("psycopg2")
    fake.extensions = types.SimpleNamespace(ISOLATION_LEVEL_AUTOCOMMIT=0)  # type: ignore[attr-defined]

    def _connect(dsn: str) -> _Connection:
        seen["dsn"] = dsn
        return _Connection(statements)

    fake.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake)

    listener = PgNotifyListener(dsn="postgresql+psycopg2://u:p@h/db", channel="outbox_events")
    assert listener.listening is False
    listener._connect()
    assert seen["dsn"] == "postgresql://u:p@h/db"
    assert statements == ["LISTEN outbox_events;"]
    assert listener.listening is True
    assert listener.wait_until_listening(0.0) is True

    listener._close()
    assert listener.listening is False
