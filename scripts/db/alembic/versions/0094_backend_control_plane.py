"""Authorize tenant-scoped backend strategy policies and drawdown mandates.

Revision ID: 0094_backend_control_plane
Revises: 0093_factor_risk_exposure
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094_backend_control_plane"
down_revision = "0093_factor_risk_exposure"
branch_labels = None
depends_on = None

_BACKEND_ROLE = "vm_backend"
_TENANT = "nullif(current_setting('app.current_tenant', true), '')"
_OWNED = f"user_id = {_TENANT}"
_TABLE_COMMANDS = {
    "api_audit_logs": ("SELECT", "INSERT"),
    "risk_mandates": ("SELECT", "INSERT"),
    "user_strategy_configs": ("SELECT", "INSERT", "UPDATE"),
}
_SERIAL_COLUMNS = {
    "api_audit_logs": "audit_id",
    "risk_mandates": "mandate_id",
}


def _policy_name(table: str, command: str) -> str:
    return f"{table}_backend_{command.lower()}"


def _create_policy(*, table: str, command: str) -> None:
    name = _policy_name(table, command)
    if command == "SELECT":
        clause = f"FOR SELECT TO {_BACKEND_ROLE} USING ({_OWNED})"
    elif command == "INSERT":
        clause = f"FOR INSERT TO {_BACKEND_ROLE} WITH CHECK ({_OWNED})"
    elif command == "UPDATE":
        clause = f"FOR UPDATE TO {_BACKEND_ROLE} USING ({_OWNED}) WITH CHECK ({_OWNED})"
    else:  # pragma: no cover - closed command inventory above
        message = f"Unsupported backend control-plane command: {command}"
        raise ValueError(message)
    op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def _serial_sequence(table: str, column: str) -> str | None:
    sequence = (
        op.get_bind()
        .execute(
            sa.text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": f"public.{table}", "column_name": column},
        )
        .scalar_one_or_none()
    )
    if sequence is None:
        return None
    name = str(sequence)
    if not all(part.replace("_", "").isalnum() for part in name.split(".")):
        message = f"Unexpected backend control-plane sequence identifier: {name!r}"
        raise RuntimeError(message)
    return name


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(f"GRANT SELECT ON TABLE strategies, strategy_versions TO {_BACKEND_ROLE}")
    for table, commands in _TABLE_COMMANDS.items():
        op.execute(f"GRANT {', '.join(commands)} ON TABLE {table} TO {_BACKEND_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        for command in commands:
            op.execute(f"DROP POLICY IF EXISTS {_policy_name(table, command)} ON {table}")
            _create_policy(table=table, command=command)

    for table, column in _SERIAL_COLUMNS.items():
        sequence = _serial_sequence(table, column)
        if sequence is not None:
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {_BACKEND_ROLE}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table, commands in _TABLE_COMMANDS.items():
        for command in commands:
            op.execute(f"DROP POLICY IF EXISTS {_policy_name(table, command)} ON {table}")
        op.execute(f"REVOKE {', '.join(commands)} ON TABLE {table} FROM {_BACKEND_ROLE}")
    for table, column in _SERIAL_COLUMNS.items():
        sequence = _serial_sequence(table, column)
        if sequence is not None:
            op.execute(f"REVOKE USAGE, SELECT ON SEQUENCE {sequence} FROM {_BACKEND_ROLE}")
    op.execute(f"REVOKE SELECT ON TABLE strategies, strategy_versions FROM {_BACKEND_ROLE}")


__all__ = ["downgrade", "upgrade"]
