"""Replace polymorphic risk owners with relational tenant identity.

Legacy execution code compressed string user IDs into a 31-bit CRC32
``owner_id``. Distinct tenants can collide in that space, and neither risk
table could enforce linked-account ownership. This revision maps every legacy
row to one real ``users.user_id`` (using the persisted context first, then an
unambiguous legacy-id lookup), refuses unknown or ambiguous mappings, and
adds account ownership to risk breaches when that identity was recorded.

Revision ID: 0058_risk_ownership
Revises: 0057_execution_attribution
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0058_risk_ownership"
down_revision = "0057_execution_attribution"
branch_labels = None
depends_on = None

_EXECUTION_ROLE = "vm_execution"
_RISK_TABLES = ("risk_mandates", "risk_breaches")
_RISK_COMMANDS = {
    "risk_mandates": ("SELECT", "INSERT"),
    "risk_breaches": ("SELECT", "INSERT", "UPDATE", "DELETE"),
}
_OTHER_RUNTIME_ROLES = (
    "vm_app",
    "vm_backend",
    "vm_scoring",
    "vm_feedback",
    "vm_market_data",
    "vm_indicator",
)


def _legacy_crc32(user_id: str) -> int:
    return int(zlib.crc32(user_id.encode("utf-8")) & 0x7FFFFFFF)


def _json_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return dict(decoded)
    msg = "Risk breach context must be a JSON object"
    raise RuntimeError(msg)


def _resolve_legacy_user(
    *,
    owner_id: Any,
    context_user_id: Any,
    user_ids: frozenset[str],
) -> str:
    if owner_id is None:
        msg = "Legacy user-owned risk row has no owner_id"
        raise RuntimeError(msg)
    try:
        numeric_owner_id = int(owner_id)
    except (TypeError, ValueError) as exc:
        msg = f"Legacy risk owner_id is not an integer: {owner_id!r}"
        raise RuntimeError(msg) from exc

    if context_user_id is not None and str(context_user_id).strip():
        user_id = str(context_user_id).strip()
        if user_id not in user_ids:
            msg = f"Risk row references unknown context user_id {user_id!r}"
            raise RuntimeError(msg)
        valid_legacy_ids = {_legacy_crc32(user_id)}
        if user_id.isdecimal():
            valid_legacy_ids.add(int(user_id))
        if numeric_owner_id not in valid_legacy_ids:
            msg = f"Risk row owner_id {numeric_owner_id} conflicts with context user_id {user_id!r}"
            raise RuntimeError(msg)
        return user_id

    candidates = {
        user_id
        for user_id in user_ids
        if _legacy_crc32(user_id) == numeric_owner_id
        or (user_id.isdecimal() and int(user_id) == numeric_owner_id)
    }
    if len(candidates) != 1:
        msg = (
            f"Legacy risk owner_id {numeric_owner_id} maps to "
            f"{len(candidates)} users; ownership migration is unsafe"
        )
        raise RuntimeError(msg)
    return candidates.pop()


def _lock_risk_tables() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        LOCK TABLE
            users,
            linked_broker_accounts,
            risk_mandates,
            risk_breaches
        IN SHARE ROW EXCLUSIVE MODE
        """
    )


def _add_relational_columns() -> None:
    op.add_column(
        "risk_mandates",
        sa.Column("user_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "risk_breaches",
        sa.Column("user_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "risk_breaches",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
    )


def _backfill_relational_ownership() -> None:
    bind = op.get_bind()
    user_ids = frozenset(
        str(row.user_id) for row in bind.execute(sa.text("SELECT user_id FROM users")).mappings()
    )
    account_owners = {
        int(row.account_id): str(row.user_id)
        for row in bind.execute(
            sa.text("SELECT account_id, user_id FROM linked_broker_accounts")
        ).mappings()
    }

    mandate_rows = bind.execute(
        sa.text("SELECT mandate_id, owner_type, owner_id FROM risk_mandates ORDER BY mandate_id")
    ).mappings()
    for row in mandate_rows:
        owner_type = str(row.owner_type).strip().lower()
        if owner_type == "global":
            if row.owner_id is not None:
                msg = f"Global risk mandate {row.mandate_id} has an owner_id"
                raise RuntimeError(msg)
            continue
        if owner_type != "user":
            msg = f"Risk mandate {row.mandate_id} has unknown owner_type {owner_type!r}"
            raise RuntimeError(msg)
        user_id = _resolve_legacy_user(
            owner_id=row.owner_id,
            context_user_id=None,
            user_ids=user_ids,
        )
        bind.execute(
            sa.text("UPDATE risk_mandates SET user_id = :user_id WHERE mandate_id = :mandate_id"),
            {"user_id": user_id, "mandate_id": int(row.mandate_id)},
        )

    breach_rows = bind.execute(
        sa.text(
            "SELECT breach_id, owner_type, owner_id, context FROM risk_breaches ORDER BY breach_id"
        )
    ).mappings()
    for row in breach_rows:
        owner_type = str(row.owner_type).strip().lower()
        if owner_type != "user":
            msg = f"Risk breach {row.breach_id} has unsupported owner_type {owner_type!r}"
            raise RuntimeError(msg)
        context = _json_mapping(row.context)
        user_id = _resolve_legacy_user(
            owner_id=row.owner_id,
            context_user_id=context.get("user_id"),
            user_ids=user_ids,
        )

        broker_account_id: int | None = None
        raw_account_id = context.get("broker_account_id")
        if raw_account_id is not None:
            if isinstance(raw_account_id, bool):
                msg = f"Risk breach {row.breach_id} has invalid broker_account_id"
                raise RuntimeError(msg)
            try:
                broker_account_id = int(raw_account_id)
            except (TypeError, ValueError) as exc:
                msg = f"Risk breach {row.breach_id} has invalid broker_account_id"
                raise RuntimeError(msg) from exc
            if broker_account_id <= 0 or account_owners.get(broker_account_id) != user_id:
                msg = (
                    f"Risk breach {row.breach_id} references broker account "
                    f"{broker_account_id} outside user {user_id!r}"
                )
                raise RuntimeError(msg)

        bind.execute(
            sa.text(
                "UPDATE risk_breaches "
                "SET user_id = :user_id, broker_account_id = :broker_account_id "
                "WHERE breach_id = :breach_id"
            ),
            {
                "user_id": user_id,
                "broker_account_id": broker_account_id,
                "breach_id": int(row.breach_id),
            },
        )

    unmapped_mandates = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM risk_mandates WHERE owner_type = 'user' AND user_id IS NULL"
            )
        ).scalar_one()
    )
    unmapped_breaches = int(
        bind.execute(
            sa.text("SELECT count(*) FROM risk_breaches WHERE user_id IS NULL")
        ).scalar_one()
    )
    if unmapped_mandates or unmapped_breaches:
        msg = (
            "Relational risk ownership backfill incomplete: "
            f"risk_mandates={unmapped_mandates}, risk_breaches={unmapped_breaches}"
        )
        raise RuntimeError(msg)


def _enforce_relational_schema() -> None:
    with op.batch_alter_table("risk_mandates") as batch_op:
        batch_op.create_foreign_key(
            "fk_risk_mandate_user",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_column("owner_id")
        batch_op.drop_column("owner_type")

    with op.batch_alter_table("risk_breaches") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.String(length=50),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_risk_breach_user",
            "users",
            ["user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_risk_breach_account_owner",
            "linked_broker_accounts",
            ["broker_account_id", "user_id"],
            ["account_id", "user_id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_column("owner_id")
        batch_op.drop_column("owner_type")

    op.create_index(
        "ix_risk_mandates_user_effective",
        "risk_mandates",
        ["user_id", "effective_at"],
    )
    op.create_index(
        "ix_risk_breaches_user_account_occurred",
        "risk_breaches",
        ["user_id", "broker_account_id", "occurred_at"],
    )


def _policy_name(table: str, command: str) -> str:
    return f"{table}_execution_{command.lower()}"


def _create_policy(*, table: str, command: str, predicate: str) -> None:
    name = _policy_name(table, command)
    if command == "SELECT":
        clause = f"FOR SELECT TO {_EXECUTION_ROLE} USING ({predicate})"
    elif command == "INSERT":
        clause = f"FOR INSERT TO {_EXECUTION_ROLE} WITH CHECK ({predicate})"
    elif command == "UPDATE":
        clause = f"FOR UPDATE TO {_EXECUTION_ROLE} USING ({predicate}) WITH CHECK ({predicate})"
    elif command == "DELETE":
        clause = f"FOR DELETE TO {_EXECUTION_ROLE} USING ({predicate})"
    else:  # pragma: no cover - callers use the closed command set below
        msg = f"Unsupported policy command: {command}"
        raise ValueError(msg)
    op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def _configure_service_access() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    excluded_roles = ", ".join((*_OTHER_RUNTIME_ROLES, "PUBLIC"))
    for table in _RISK_TABLES:
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {excluded_roles}")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM {_EXECUTION_ROLE}")
        privileges = ", ".join(_RISK_COMMANDS[table])
        op.execute(f"GRANT {privileges} ON TABLE {table} TO {_EXECUTION_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            op.execute(f"DROP POLICY IF EXISTS {_policy_name(table, command)} ON {table}")

    tenant = "nullif(current_setting('app.current_tenant', true), '')"
    mandate_predicate = f"user_id IS NULL OR {tenant} IS NULL OR user_id = {tenant}"
    breach_predicate = f"{tenant} IS NULL OR user_id = {tenant}"
    for command in _RISK_COMMANDS["risk_mandates"]:
        _create_policy(
            table="risk_mandates",
            command=command,
            predicate=mandate_predicate,
        )
    for command in _RISK_COMMANDS["risk_breaches"]:
        _create_policy(
            table="risk_breaches",
            command=command,
            predicate=breach_predicate,
        )

    bind = op.get_bind()
    for table, column in (
        ("risk_mandates", "mandate_id"),
        ("risk_breaches", "breach_id"),
    ):
        sequence = bind.execute(
            sa.text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": f"public.{table}", "column_name": column},
        ).scalar_one_or_none()
        if sequence:
            sequence_name = str(sequence)
            if not all(part.replace("_", "").isalnum() for part in sequence_name.split(".")):
                msg = f"Unexpected risk sequence identifier: {sequence_name!r}"
                raise RuntimeError(msg)
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence_name} TO {_EXECUTION_ROLE}")


def upgrade() -> None:
    _lock_risk_tables()
    _add_relational_columns()
    _backfill_relational_ownership()
    _enforce_relational_schema()
    _configure_service_access()


def _legacy_owner_ids(user_ids: Iterable[str]) -> dict[str, int]:
    unique_user_ids = frozenset(user_ids)
    result = {user_id: _legacy_crc32(user_id) for user_id in unique_user_ids}
    for user_id, owner_id in result.items():
        candidates = {
            candidate
            for candidate in unique_user_ids
            if _legacy_crc32(candidate) == owner_id
            or (candidate.isdecimal() and int(candidate) == owner_id)
        }
        if candidates != {user_id}:
            msg = (
                f"Cannot downgrade relational risk ownership: user {user_id!r} "
                f"would map ambiguously at legacy owner_id {owner_id}"
            )
            raise RuntimeError(msg)
    return result


def _add_legacy_columns() -> None:
    op.add_column(
        "risk_mandates",
        sa.Column("owner_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "risk_mandates",
        sa.Column("owner_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "risk_breaches",
        sa.Column("owner_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "risk_breaches",
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
    )


def _backfill_legacy_columns() -> None:
    bind = op.get_bind()
    user_ids = [
        str(row.user_id) for row in bind.execute(sa.text("SELECT user_id FROM users")).mappings()
    ]
    legacy_ids = _legacy_owner_ids(user_ids)

    mandate_rows = bind.execute(
        sa.text("SELECT mandate_id, user_id FROM risk_mandates ORDER BY mandate_id")
    ).mappings()
    for row in mandate_rows:
        user_id = str(row.user_id) if row.user_id is not None else None
        bind.execute(
            sa.text(
                "UPDATE risk_mandates "
                "SET owner_type = :owner_type, owner_id = :owner_id "
                "WHERE mandate_id = :mandate_id"
            ),
            {
                "owner_type": "user" if user_id is not None else "global",
                "owner_id": legacy_ids[user_id] if user_id is not None else None,
                "mandate_id": int(row.mandate_id),
            },
        )

    context_update = sa.text(
        "UPDATE risk_breaches SET owner_type = 'user', owner_id = :owner_id, "
        "context = :context WHERE breach_id = :breach_id"
    ).bindparams(sa.bindparam("context", type_=sa.JSON()))
    breach_rows = bind.execute(
        sa.text(
            "SELECT breach_id, user_id, broker_account_id, context "
            "FROM risk_breaches ORDER BY breach_id"
        )
    ).mappings()
    for row in breach_rows:
        user_id = str(row.user_id)
        context = _json_mapping(row.context)
        context["user_id"] = user_id
        if row.broker_account_id is not None:
            context["broker_account_id"] = int(row.broker_account_id)
        bind.execute(
            context_update,
            {
                "owner_id": legacy_ids[user_id],
                "context": context,
                "breach_id": int(row.breach_id),
            },
        )


def _restore_legacy_schema() -> None:
    op.drop_index(
        "ix_risk_breaches_user_account_occurred",
        table_name="risk_breaches",
    )
    op.drop_index(
        "ix_risk_mandates_user_effective",
        table_name="risk_mandates",
    )
    with op.batch_alter_table("risk_breaches") as batch_op:
        batch_op.drop_constraint("fk_risk_breach_account_owner", type_="foreignkey")
        batch_op.drop_constraint("fk_risk_breach_user", type_="foreignkey")
        batch_op.alter_column(
            "owner_type",
            existing_type=sa.String(length=20),
            nullable=False,
        )
        batch_op.drop_column("broker_account_id")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("risk_mandates") as batch_op:
        batch_op.drop_constraint("fk_risk_mandate_user", type_="foreignkey")
        batch_op.alter_column(
            "owner_type",
            existing_type=sa.String(length=20),
            nullable=False,
        )
        batch_op.drop_column("user_id")


def _remove_service_policies() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _RISK_TABLES:
        for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            op.execute(f"DROP POLICY IF EXISTS {_policy_name(table, command)} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    _remove_service_policies()
    _add_legacy_columns()
    _backfill_legacy_columns()
    _restore_legacy_schema()
