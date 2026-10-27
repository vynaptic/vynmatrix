"""Execute research schema migration and evidence-preserving rollback."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

PATH = Path(__file__).resolve().parents[1] / "scripts/db/alembic/versions/0113_research_data.py"


def migration() -> Any:
    spec = importlib.util.spec_from_file_location("research_data_migration", PATH)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(engine: sa.Engine, direction: str) -> None:
    module = migration()
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        getattr(module, direction)()


def test_migration_creates_composite_owner_lineage_and_empty_rollback() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (user_id VARCHAR(50) PRIMARY KEY)"))
    run(engine, "upgrade")
    inspector = sa.inspect(engine)
    assert {"research_imports", "research_datasets", "research_bars"} <= set(
        inspector.get_table_names()
    )
    fks = inspector.get_foreign_keys("research_bars")
    assert any(item["constrained_columns"] == ["owner_id", "dataset_id"] for item in fks)
    run(engine, "downgrade")
    assert "research_imports" not in sa.inspect(engine).get_table_names()


def test_downgrade_refuses_to_destroy_failed_import_evidence() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (user_id VARCHAR(50) PRIMARY KEY)"))
    run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO research_imports (import_id,owner_id,request_hash,original_hash,valid,metadata_json,report_json,original_csv,canonical_bars,stored_bytes) VALUES ('receipt','owner','request','original',false,'{}','{}',X'00','[]',1)"
            )
        )
    with pytest.raises(RuntimeError, match="retained research evidence"):
        run(engine, "downgrade")


def test_downgrade_locks_all_tables_before_observing_empty_state() -> None:
    from types import SimpleNamespace

    class ConcurrentAdmission:
        """A concurrent writer enters unless migration first takes its DDL lock."""

        def __init__(self) -> None:
            self.locked = False
            self.dropped = []

        def get_bind(self) -> Any:
            return self

        @property
        def dialect(self) -> Any:
            return SimpleNamespace(name="postgresql")

        def execute(self, statement: Any) -> None:
            text = str(statement)
            if text.startswith("LOCK TABLE"):
                assert all(
                    name in text
                    for name in ("research_imports", "research_datasets", "research_bars")
                )
                assert "ACCESS EXCLUSIVE" in text
                self.locked = True

        def scalar(self, _statement: Any) -> int:
            assert self.locked, "A concurrent import could arrive after an unlocked empty check"
            return 0

        def drop_table(self, name: str) -> None:
            self.dropped.append(name)

    module = migration()
    concurrent = ConcurrentAdmission()
    module.op = concurrent
    module.downgrade()
    assert concurrent.dropped == ["research_bars", "research_datasets", "research_imports"]
