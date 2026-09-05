"""Contract tests for authoritative market-session migration 0067."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0067_authoritative_market_sessions.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "authoritative_market_sessions_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _legacy_instruments(engine: sa.Engine) -> sa.Table:
    metadata = sa.MetaData()
    instruments = sa.Table(
        "instruments",
        metadata,
        sa.Column("instr_id", sa.Integer, primary_key=True),
        sa.Column("asset_class", sa.String(20), nullable=False),
        sa.Column("canonical", sa.String(50), nullable=False),
    )
    metadata.create_all(engine)
    return instruments


def test_revision_follows_saxo_catalogue() -> None:
    migration = _load_migration()

    assert migration.revision == "0067_market_sessions"
    assert migration.down_revision == "0066_saxo_broker_catalogue"


def test_upgrade_classifies_existing_instruments_and_round_trips() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    instruments = _legacy_instruments(engine)
    with engine.begin() as connection:
        connection.execute(
            instruments.insert(),
            [
                {"instr_id": 1, "asset_class": "crypto", "canonical": "BTC/USD"},
                {"instr_id": 2, "asset_class": "equity", "canonical": "AAPL"},
            ],
        )

    _run(engine, "upgrade")

    inspector = sa.inspect(engine)
    assert {"market_calendars", "market_sessions"} <= set(inspector.get_table_names())
    assert {"market_session_policy", "market_calendar_id"} <= {
        column["name"] for column in inspector.get_columns("instruments")
    }
    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT instr_id, market_session_policy, market_calendar_id "
                "FROM instruments ORDER BY instr_id"
            )
        ).all() == [
            (1, "continuous", None),
            (2, "scheduled", None),
        ]

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO instruments (instr_id, asset_class, canonical) "
                "VALUES (3, 'crypto', 'ETH/USD')"
            )
        )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO instruments "
                "(instr_id, asset_class, canonical, market_session_policy) "
                "VALUES (3, 'crypto', 'ETH/USD', 'continuous')"
            )
        )

    _run(engine, "downgrade")
    downgraded = sa.inspect(engine)
    assert "market_calendars" not in downgraded.get_table_names()
    assert "market_sessions" not in downgraded.get_table_names()
    assert "market_session_policy" not in {
        column["name"] for column in downgraded.get_columns("instruments")
    }


@pytest.mark.parametrize(
    ("state_sql", "expected_counts"),
    [
        (
            "INSERT INTO market_calendars "
            "(calendar_id, code, source_kind, provider, source_reference) "
            "VALUES (1, 'XNAS:REGULAR', 'exchange', 'nasdaq', 'official')",
            "calendars=1, sessions=0, instrument_assignments=0",
        ),
        (
            "INSERT INTO market_sessions "
            "(session_id, calendar_id, opens_at, closes_at) "
            "VALUES (1, 1, '2026-07-24T13:30:00+00:00', "
            "'2026-07-24T20:00:00+00:00')",
            "calendars=1, sessions=1, instrument_assignments=0",
        ),
        (
            "UPDATE instruments SET market_calendar_id = 99 WHERE instr_id = 1",
            "calendars=0, sessions=0, instrument_assignments=1",
        ),
    ],
    ids=["calendar", "session", "assignment"],
)
def test_downgrade_refuses_authoritative_state(
    state_sql: str,
    expected_counts: str,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    instruments = _legacy_instruments(engine)
    with engine.begin() as connection:
        connection.execute(
            instruments.insert().values(
                instr_id=1,
                asset_class="equity",
                canonical="AAPL",
            )
        )
    _run(engine, "upgrade")
    with engine.begin() as connection:
        if "market_sessions" in state_sql:
            connection.execute(
                sa.text(
                    "INSERT INTO market_calendars "
                    "(calendar_id, code, source_kind, provider, source_reference) "
                    "VALUES (1, 'XNAS:REGULAR', 'exchange', 'nasdaq', 'official')"
                )
            )
        connection.execute(sa.text(state_sql))

    with pytest.raises(RuntimeError, match=expected_counts):
        _run(engine, "downgrade")

    inspector = sa.inspect(engine)
    assert {"market_calendars", "market_sessions"} <= set(inspector.get_table_names())
    assert {"market_session_policy", "market_calendar_id"} <= {
        column["name"] for column in inspector.get_columns("instruments")
    }


class _RecordingPostgresOp:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def get_bind(self) -> Any:
        return self.bind

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def test_downgrade_revokes_backend_instrument_update_on_postgresql() -> None:
    migration = _load_migration()
    recorder = _RecordingPostgresOp()
    migration.op = recorder

    migration._revoke_runtime_privileges()

    assert recorder.executed == ["REVOKE UPDATE ON TABLE public.instruments FROM vm_backend"]


def test_upgrade_enforces_calendar_provenance_and_session_intervals() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    _legacy_instruments(engine)
    _run(engine, "upgrade")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO market_calendars "
                "(calendar_id, code, source_kind, provider, source_reference) "
                "VALUES (1, 'XNAS:REGULAR', 'guess', 'nasdaq', 'https://www.nasdaq.com/')"
            )
        )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO market_calendars "
                "(calendar_id, code, source_kind, provider, source_reference, "
                "coverage_start, coverage_end, observed_at) "
                "VALUES (1, 'XNAS:REGULAR', 'exchange', 'nasdaq', "
                "'https://www.nasdaq.com/', "
                "'2026-07-24T00:00:00+00:00', '2026-07-25T00:00:00+00:00', "
                "'2026-07-24T13:00:00+00:00')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO market_sessions "
                "(session_id, calendar_id, opens_at, closes_at) "
                "VALUES (1, 1, '2026-07-24T20:00:00+00:00', "
                "'2026-07-24T13:30:00+00:00')"
            )
        )
