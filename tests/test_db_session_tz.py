"""MD-3: DB engines pin the Postgres session timezone to UTC so comparisons
between tz-naive columns and tz-aware UTC datetimes resolve consistently."""

from __future__ import annotations

from lib_application.db.session import _connect_args_for


def test_postgres_url_pins_utc_timezone() -> None:
    assert _connect_args_for("postgresql://u:p@h:5432/db") == {"options": "-c timezone=utc"}
    assert _connect_args_for("postgresql+psycopg2://u:p@h/db") == {"options": "-c timezone=utc"}


def test_sqlite_url_gets_no_connect_args() -> None:
    assert _connect_args_for("sqlite+pysqlite:///:memory:") == {}
    assert _connect_args_for("sqlite:///:memory:") == {}
