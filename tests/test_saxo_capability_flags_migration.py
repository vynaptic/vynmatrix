"""Correct untouched historical Saxo references without overwriting owner edits."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lib_infrastructure.brokers.capabilities import SAXO_CAPABILITIES


def migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts/db/alembic/versions/0104_saxo_capability_flags.py"
    )
    spec = importlib.util.spec_from_file_location("saxo_flags", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_payload_matches_current_source():
    module = migration()
    assert module.down_revision == "0103_remove_commercial_tenancy"
    assert SAXO_CAPABILITIES.catalogue_payload() == module._CURRENT
    assert set(module._CURRENT) - set(module._HISTORICAL) == {
        "exact_fill_retrieval",
        "live_certification_implemented",
    }


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
@pytest.mark.parametrize("variant", ["exact", "custom", "partial", "true", "numeric", "absent"])
def test_exact_payload_only_and_repeat(operation, variant):
    module = migration()
    old, new = (
        (module._HISTORICAL, module._CURRENT)
        if operation == "upgrade"
        else (module._CURRENT, module._HISTORICAL)
    )
    payload = dict(old)
    if variant == "custom":
        payload["custom"] = "preserve"
    elif variant == "partial":
        payload["exact_fill_retrieval"] = False
        payload.pop("live_certification_implemented", None)
    elif variant == "true":
        payload["exact_fill_retrieval"] = True
    elif variant == "numeric":
        payload["exact_fill_retrieval"] = 0
    engine = sa.create_engine("sqlite://")
    table = sa.Table(
        "brokers",
        sa.MetaData(),
        sa.Column("broker_id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        sa.Column("capabilities", sa.JSON),
    )
    table.create(engine)
    try:
        with engine.begin() as connection:
            if variant != "absent":
                connection.execute(
                    table.insert().values(broker_id=42, code="saxo", capabilities=payload)
                )
            connection.execute(table.insert().values(broker_id=43, code="other", capabilities=old))
            module.op = Operations(MigrationContext.configure(connection))
            getattr(module, operation)()
            getattr(module, operation)()
            row = connection.execute(sa.select(table).where(table.c.broker_id == 42)).first()
            if variant == "absent":
                assert row is None
            else:
                assert row.capabilities == (new if variant == "exact" else payload)
                assert row.broker_id == 42
            assert (
                connection.execute(
                    sa.select(table.c.capabilities).where(table.c.broker_id == 43)
                ).scalar_one()
                == old
            )
    finally:
        engine.dispose()
