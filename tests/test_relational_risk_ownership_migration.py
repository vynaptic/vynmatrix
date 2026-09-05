"""Contract tests for relational risk ownership migration 0058."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    Broker,
    LinkedBrokerAccount,
    RiskBreach,
    RiskMandate,
    User,
)

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0058_relational_risk_ownership.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "relational_risk_ownership_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(engine: sa.Engine, operation: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        getattr(migration, operation)()


def _seed_relational_rows(engine: sa.Engine) -> None:
    with Session(engine) as session:
        session.add(
            User(
                user_id="tenant-alpha",
                email="tenant-alpha@example.invalid",
                base_ccy="USD",
            )
        )
        session.add(Broker(broker_id=1, code="paper", name="Paper", capabilities={}))
        session.flush()
        session.add(
            LinkedBrokerAccount(
                account_id=101,
                user_id="tenant-alpha",
                broker_id=1,
                environment="paper",
                display_name="Primary",
                base_ccy="USD",
                paper_initial_equity=100_000,
                paper_initial_cash=100_000,
            )
        )
        session.add(
            RiskMandate(
                user_id="tenant-alpha",
                rules={"max_position_pct": 0.1},
                effective_at=datetime(2026, 7, 25, tzinfo=UTC),
            )
        )
        session.add(
            RiskBreach(
                user_id="tenant-alpha",
                broker_account_id=101,
                rule_code="max_position_pct",
                severity="block",
                context={"user_id": "tenant-alpha", "broker_account_id": 101},
            )
        )
        session.commit()


def test_revision_follows_canonical_execution_attribution() -> None:
    migration = _load_migration()

    assert migration.revision == "0058_risk_ownership"
    assert migration.down_revision == "0057_execution_attribution"


def test_orm_uses_real_user_and_composite_account_foreign_keys() -> None:
    mandate = Base.metadata.tables["risk_mandates"]
    breach = Base.metadata.tables["risk_breaches"]

    assert {"owner_type", "owner_id"}.isdisjoint(mandate.c.keys())
    assert {"owner_type", "owner_id"}.isdisjoint(breach.c.keys())
    assert mandate.c.user_id.nullable is True
    assert breach.c.user_id.nullable is False
    assert breach.c.broker_account_id.nullable is True

    breach_foreign_keys = {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in breach.foreign_key_constraints
    }
    assert (
        ("broker_account_id", "user_id"),
        (
            "linked_broker_accounts.account_id",
            "linked_broker_accounts.user_id",
        ),
    ) in breach_foreign_keys


def test_relational_schema_round_trips_without_losing_owner_identity() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_relational_rows(engine)

    _run_revision(engine, "downgrade")
    legacy = inspect(engine)
    assert {"owner_type", "owner_id"} <= {
        column["name"] for column in legacy.get_columns("risk_mandates")
    }
    assert {"user_id", "broker_account_id"}.isdisjoint(
        {column["name"] for column in legacy.get_columns("risk_breaches")}
    )

    _run_revision(engine, "upgrade")

    with engine.connect() as connection:
        mandate = connection.execute(sa.text("SELECT user_id FROM risk_mandates")).mappings().one()
        breach = (
            connection.execute(
                sa.text("SELECT user_id, broker_account_id, context FROM risk_breaches")
            )
            .mappings()
            .one()
        )
    assert mandate.user_id == "tenant-alpha"
    assert breach.user_id == "tenant-alpha"
    assert breach.broker_account_id == 101


def test_upgrade_fails_closed_when_legacy_owner_cannot_be_mapped() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _run_revision(engine, "downgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO risk_breaches "
                "(breach_id, owner_type, owner_id, rule_code, severity, context) "
                "VALUES (1, 'user', 2147483647, 'unknown_owner', 'block', '{}')"
            )
        )

    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="maps to 0 users"):
            migration.upgrade()


def test_downgrade_fails_when_crc_owner_id_matches_numeric_user_id() -> None:
    migration = _load_migration()
    user_id = "tenant-alpha"
    numeric_collision = str(migration._legacy_crc32(user_id))

    with pytest.raises(RuntimeError, match="map ambiguously"):
        migration._legacy_owner_ids([user_id, numeric_collision])


class _ScalarResult:
    def scalar_one_or_none(self) -> str:
        return "public.risk_id_seq"


class _RecordingBind:
    dialect = SimpleNamespace(name="postgresql")

    def execute(self, _statement: Any, _params: Any = None) -> _ScalarResult:
        return _ScalarResult()


class _RecordingOp:
    def __init__(self) -> None:
        self.bind = _RecordingBind()
        self.statements: list[str] = []

    def get_bind(self) -> _RecordingBind:
        return self.bind

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def test_service_access_uses_command_specific_execution_policies() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder

    migration._configure_service_access()

    policies = [
        statement for statement in recorder.statements if statement.startswith("CREATE POLICY")
    ]
    assert len(policies) == 6
    assert all(" FOR ALL " not in policy for policy in policies)
    assert (
        "CREATE POLICY risk_breaches_execution_insert ON risk_breaches "
        "FOR INSERT TO vm_execution WITH CHECK "
        "(nullif(current_setting('app.current_tenant', true), '') IS NULL "
        "OR user_id = nullif(current_setting('app.current_tenant', true), ''))"
    ) in policies
    assert not any(" TO vm_backend " in policy for policy in policies)
    assert not any("risk_mandates_execution_update" in policy for policy in policies)
    assert not any("risk_mandates_execution_delete" in policy for policy in policies)
    assert "GRANT SELECT, INSERT ON TABLE risk_mandates TO vm_execution" in recorder.statements
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE risk_breaches TO vm_execution"
        in recorder.statements
    )
    assert "ALTER TABLE risk_mandates ENABLE ROW LEVEL SECURITY" in recorder.statements
    assert "ALTER TABLE risk_breaches ENABLE ROW LEVEL SECURITY" in recorder.statements
