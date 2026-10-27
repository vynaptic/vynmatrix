"""Workflow migrations retain evidence and grant only bounded research authority."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

PATH = (
    Path(__file__).resolve().parents[1] / "scripts/db/alembic/versions/0114_research_workflows.py"
)


def migration() -> Any:
    spec = importlib.util.spec_from_file_location("research_workflows_migration", PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_linear_and_database_guards_are_declared() -> None:
    module = migration()
    assert module.down_revision == "0113_research_data"
    assert len(module.revision) <= 32
    source = PATH.read_text()
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "vm_research" in source
    assert "current_setting('app.current_tenant', true)" in source
    assert "vm_research_workflow_guard" in source
    assert "pg_try_advisory_xact_lock" in source


def test_sqlite_upgrade_enforces_bulk_immutability_and_retained_downgrade() -> None:
    module = migration()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (user_id varchar(50) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE research_datasets (owner_id varchar(50), dataset_id varchar(36), UNIQUE(owner_id,dataset_id))"
            )
        )
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert str(UUID(connection.scalar(text("SELECT generation FROM research_control"))))
        connection.execute(
            text(
                "INSERT INTO research_drafts (draft_id,owner_id,request_id,request_hash,name,revision,created_at) VALUES ('d','owner','r','hash','Draft',0,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO research_draft_revisions (revision_id,owner_id,draft_id,revision,source,config_json,source_hash,content_hash,created_at) VALUES ('v','owner','d',1,'source','{}','source-hash','content-hash',CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                text("UPDATE research_draft_revisions SET source='changed' WHERE revision_id='v'")
            )
        with pytest.raises(RuntimeError, match="retained research"):
            module.downgrade()
    engine.dispose()


def test_postgres_downgrade_locks_all_affected_tables_before_retained_count() -> None:
    from types import SimpleNamespace

    events: list[str] = []

    class Recorder:
        dialect = SimpleNamespace(name="postgresql")

        def get_bind(self):
            return self

        def scalar(self, statement):
            sql = str(statement)
            events.append(sql)
            return True if "pg_try_advisory_xact_lock" in sql else 0

        def execute(self, statement):
            events.append(str(statement))

        def drop_table(self, table):
            events.append("DROP TABLE " + table)

        def drop_constraint(self, *_args, **_kwargs):
            events.append("DROP CONSTRAINT")

    module = migration()
    module.op = Recorder()
    module.downgrade()
    fence = next(index for index, sql in enumerate(events) if "pg_try_advisory_xact_lock" in sql)
    lock = next(index for index, sql in enumerate(events) if "ACCESS EXCLUSIVE" in sql)
    counts = [index for index, sql in enumerate(events) if "SELECT count(*)" in sql]
    assert fence < lock < min(counts)
    assert all(
        "public." + table in events[lock]
        for table in (
            *module.TABLES,
            "research_control",
            "research_imports",
            "research_datasets",
            "research_bars",
        )
    )
