"""Offline migration contracts; PostgreSQL privilege enforcement is tested separately."""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration_sql(direction: str) -> str:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts/db/alembic/versions/0101_reference_catalogue.py"
    )
    spec = importlib.util.spec_from_file_location("reference_catalogue_migration", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    )
    getattr(migration, direction)()
    return output.getvalue()


def test_backend_instrument_updates_are_limited_to_calendar_assignment() -> None:
    sql = _migration_sql("upgrade")
    assert "REVOKE UPDATE ON TABLE public.instruments FROM vm_backend;" in sql
    assert "GRANT UPDATE (market_calendar_id) ON TABLE public.instruments TO vm_backend;" in sql
    assert "GRANT UPDATE ON TABLE public.instruments TO vm_backend;" not in sql


def test_downgrade_restores_historical_instrument_privileges_without_row_changes() -> None:
    sql = _migration_sql("downgrade")
    assert "REVOKE UPDATE (market_calendar_id) ON TABLE public.instruments FROM vm_backend;" in sql
    assert "GRANT UPDATE ON TABLE public.instruments TO vm_backend;" in sql
    assert "UPDATE public.instruments SET" not in sql
