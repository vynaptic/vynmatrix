"""Metadata checks for the Section N equity reference tables (§8.2).

Mirrors the ``tests/test_alembic_models_round_trip.py`` pattern: the three
reference tables must be importable from ``lib_application.db.models``,
registered in ``Base.metadata`` (Alembic autogenerate discovery), and carry
the §8.2 uniqueness guarantees the loaders upsert against.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint, create_engine, inspect

from lib_application.db.models import (
    Base,
    CorporateAction,
    EarningsEvent,
    EquitySecurityIdentity,
    IndexMembership,
)

EXPECTED_UNIQUES = {
    "corporate_actions": ("instr_id", "action_type", "ex_date", "source"),
    "index_membership": ("index_code", "instr_id", "effective_from"),
    "equity_security_identities": ("security_id", "effective_from"),
    "earnings_events": ("instr_id", "report_date", "source"),
}


def test_reference_tables_registered_in_metadata() -> None:
    """Every §8.2 table must be discoverable from the shared ``Base.metadata``."""
    for model, table_name in (
        (CorporateAction, "corporate_actions"),
        (IndexMembership, "index_membership"),
        (EquitySecurityIdentity, "equity_security_identities"),
        (EarningsEvent, "earnings_events"),
    ):
        assert model.__tablename__ == table_name
        assert table_name in Base.metadata.tables


def test_reference_tables_carry_upsert_unique_constraints() -> None:
    """The §8.2 natural keys must exist as named unique constraints."""
    for table_name, expected_cols in EXPECTED_UNIQUES.items():
        table = Base.metadata.tables[table_name]
        unique_column_sets = {
            tuple(col.name for col in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert expected_cols in unique_column_sets, (
            f"{table_name} is missing its unique constraint on {expected_cols}; "
            f"found {unique_column_sets}"
        )


def test_reference_tables_create_on_sqlite() -> None:
    """``create_all`` must materialize the new tables (same path as unit fixtures)."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    actual = set(inspect(engine).get_table_names())
    assert set(EXPECTED_UNIQUES) <= actual
