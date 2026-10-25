"""MD-3: DB engines pin the Postgres session timezone to UTC so comparisons
between tz-naive columns and tz-aware UTC datetimes resolve consistently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from lib_application.db.session import _connect_args_for, bind_utc_instant


def test_postgres_url_pins_utc_timezone() -> None:
    assert _connect_args_for("postgresql://u:p@h:5432/db") == {"options": "-c timezone=utc"}
    assert _connect_args_for("postgresql+psycopg2://u:p@h/db") == {"options": "-c timezone=utc"}


def test_sqlite_url_gets_no_connect_args() -> None:
    assert _connect_args_for("sqlite+pysqlite:///:memory:") == {}
    assert _connect_args_for("sqlite:///:memory:") == {}


def _session_on(dialect: str) -> Session:
    bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    return SimpleNamespace(get_bind=lambda: bind)  # type: ignore[return-value]


def test_utc_instant_binds_aware_on_postgres() -> None:
    value = datetime(2026, 6, 30, 16, 0, tzinfo=timezone(timedelta(hours=-4)))

    bound = bind_utc_instant(_session_on("postgresql"), value)

    assert bound == datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
    assert bound.tzinfo is UTC


def test_utc_instant_binds_naive_utc_on_sqlite() -> None:
    value = datetime(2026, 6, 30, 16, 0, tzinfo=timezone(timedelta(hours=-4)))

    assert bind_utc_instant(_session_on("sqlite"), value) == datetime(2026, 6, 30, 20, 0)


def test_utc_instant_rejects_naive_value() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        bind_utc_instant(_session_on("postgresql"), datetime(2026, 6, 30, 20, 0))
