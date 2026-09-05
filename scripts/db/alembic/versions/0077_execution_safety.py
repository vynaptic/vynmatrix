"""Add current execution authority and audited outbox recovery controls.

Revision ID: 0077_execution_safety
Revises: 0076_durable_paper_orders
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0077_execution_safety"
down_revision = "0076_durable_paper_orders"
branch_labels = None
depends_on = None

_NORMALIZE_INSTRUMENT = re.compile(r"[/_-]")


def _instrument_key(value: object) -> str:
    return _NORMALIZE_INSTRUMENT.sub("", str(value or "").strip()).upper()


def _normalize_active_binding_scopes(bind: Any) -> None:  # noqa: PLR0912
    """Fail on ambiguous ownership and rewrite active scopes canonically."""
    instruments = sa.table(
        "instruments",
        sa.column("instr_id", sa.Integer()),
        sa.column("canonical", sa.String()),
    )
    aliases = sa.table(
        "instrument_aliases",
        sa.column("instr_id", sa.Integer()),
        sa.column("alias", sa.String()),
    )
    bindings = sa.table(
        "user_strategy_bindings",
        sa.column("binding_id", sa.BigInteger()),
        sa.column("broker_account_id", sa.BigInteger()),
        sa.column("strategy_id", sa.String()),
        sa.column("instruments_allowed", sa.JSON()),
        sa.column("is_active", sa.Boolean()),
    )

    canonical_by_id = {
        int(row.instr_id): str(row.canonical)
        for row in bind.execute(
            sa.select(instruments.c.instr_id, instruments.c.canonical)
        )
    }
    ids_by_key: dict[str, set[int]] = defaultdict(set)
    for instr_id, canonical in canonical_by_id.items():
        ids_by_key[_instrument_key(canonical)].add(instr_id)
    for row in bind.execute(sa.select(aliases.c.instr_id, aliases.c.alias)):
        ids_by_key[_instrument_key(row.alias)].add(int(row.instr_id))

    active_rows: list[dict[str, Any]] = []
    for row in bind.execute(
        sa.select(
            bindings.c.binding_id,
            bindings.c.broker_account_id,
            bindings.c.strategy_id,
            bindings.c.instruments_allowed,
        ).where(bindings.c.is_active.is_(True))
    ).mappings():
        binding_id = int(row["binding_id"])
        strategy_id = row["strategy_id"]
        if not str(strategy_id or "").strip():
            msg = f"active binding {binding_id} has wildcard strategy authority"
            raise RuntimeError(msg)
        raw_scope = row["instruments_allowed"]
        if raw_scope is None:
            normalized_scope: list[str] | None = None
            instrument_ids: set[int] | None = None
        else:
            if not isinstance(raw_scope, list) or not raw_scope:
                msg = f"active binding {binding_id} has an invalid instrument scope"
                raise RuntimeError(msg)
            instrument_ids = set()
            normalized_scope = []
            for token in raw_scope:
                if isinstance(token, bool):
                    msg = f"active binding {binding_id} has an invalid instrument token"
                    raise RuntimeError(msg)  # noqa: TRY004
                if isinstance(token, int) or (
                    isinstance(token, str)
                    and token.isascii()
                    and token.isdigit()
                    and not token.startswith("0")
                ):
                    candidates = {int(token)} if int(token) in canonical_by_id else set()
                else:
                    candidates = ids_by_key.get(_instrument_key(token), set())
                if len(candidates) != 1:
                    msg = (
                        f"active binding {binding_id} has an unknown or ambiguous "
                        f"instrument {token!r}"
                    )
                    raise RuntimeError(msg)
                instr_id = next(iter(candidates))
                instrument_ids.add(instr_id)
                canonical = canonical_by_id[instr_id]
                if canonical not in normalized_scope:
                    normalized_scope.append(canonical)
            bind.execute(
                bindings.update()
                .where(bindings.c.binding_id == binding_id)
                .values(instruments_allowed=normalized_scope)
            )
        active_rows.append(
            {
                "binding_id": binding_id,
                "account_id": int(row["broker_account_id"]),
                "strategy_id": str(strategy_id),
                "instrument_ids": instrument_ids,
            }
        )

    by_account: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in active_rows:
        by_account[row["account_id"]].append(row)
    for account_id, rows in by_account.items():
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                if left["strategy_id"] == right["strategy_id"]:
                    continue
                left_scope = left["instrument_ids"]
                right_scope = right["instrument_ids"]
                overlaps = (
                    left_scope is None
                    or right_scope is None
                    or bool(left_scope & right_scope)
                )
                if overlaps:
                    msg = (
                        "conflicting active strategy bindings "
                        f"{left['binding_id']} and {right['binding_id']} overlap "
                        f"on broker account {account_id}"
                    )
                    raise RuntimeError(msg)


def _create_postgresql_binding_trigger() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION vm_binding_instrument_ids(scope JSON)
        RETURNS BIGINT[]
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            token TEXT;
            resolved_id BIGINT;
            resolved_ids BIGINT[] := ARRAY[]::BIGINT[];
        BEGIN
            IF scope IS NULL THEN
                RETURN NULL;
            END IF;
            IF json_typeof(scope) <> 'array' OR json_array_length(scope) = 0 THEN
                RAISE EXCEPTION 'active binding instrument scope must be null or non-empty array';
            END IF;
            FOR token IN SELECT json_array_elements_text(scope)
            LOOP
                SELECT instrument.instr_id
                  INTO resolved_id
                  FROM instruments AS instrument
                 WHERE instrument.canonical = token
                 LIMIT 1;
                IF resolved_id IS NULL THEN
                    RAISE EXCEPTION 'binding instrument % is not canonical', token;
                END IF;
                IF NOT resolved_id = ANY(resolved_ids) THEN
                    resolved_ids := array_append(resolved_ids, resolved_id);
                END IF;
            END LOOP;
            RETURN resolved_ids;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION vm_validate_strategy_binding()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        DECLARE
            new_scope BIGINT[];
            selected_broker TEXT;
        BEGIN
            -- The conflict query below cannot by itself prevent two
            -- READ COMMITTED transactions from both observing an empty scope.
            -- Serialize all authority changes for an account at the database
            -- boundary as well as in the backend API.  UPDATEs that move a row
            -- lock both account scopes in numeric order to avoid deadlocks.
            IF TG_OP = 'UPDATE'
               AND OLD.broker_account_id IS DISTINCT FROM NEW.broker_account_id
            THEN
                IF OLD.broker_account_id < NEW.broker_account_id THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(OLD.broker_account_id::TEXT)
                    );
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(NEW.broker_account_id::TEXT)
                    );
                ELSE
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(NEW.broker_account_id::TEXT)
                    );
                    PERFORM pg_advisory_xact_lock(
                        hashtext('vm_binding_account'),
                        hashtext(OLD.broker_account_id::TEXT)
                    );
                END IF;
            ELSE
                PERFORM pg_advisory_xact_lock(
                    hashtext('vm_binding_account'),
                    hashtext(NEW.broker_account_id::TEXT)
                );
            END IF;

            IF json_typeof(NEW.execution_modes_allowed) <> 'array'
               OR json_array_length(NEW.execution_modes_allowed) = 0
               OR EXISTS (
                    SELECT 1
                      FROM json_array_elements_text(NEW.execution_modes_allowed) AS mode(value)
                     WHERE mode.value NOT IN (
                        'spot', 'margin', 'perpetual', 'futures', 'bull_call',
                        'bear_put', 'bull_put', 'bear_call', 'iron_condor',
                        'straddle', 'strangle', 'options_single', 'notify_only', 'paper'
                     )
               )
            THEN
                RAISE EXCEPTION 'binding has invalid execution_modes_allowed';
            END IF;
            IF NEW.preferred_mode IS NOT NULL
               AND NOT (
                    NEW.execution_modes_allowed::jsonb
                    @> to_jsonb(ARRAY[NEW.preferred_mode]::TEXT[])
               )
            THEN
                RAISE EXCEPTION 'binding preferred_mode is not allowed';
            END IF;
            IF json_typeof(NEW.asset_classes_allowed) <> 'array'
               OR json_array_length(NEW.asset_classes_allowed) = 0
               OR EXISTS (
                    SELECT 1
                      FROM json_array_elements_text(NEW.asset_classes_allowed) AS asset(value)
                     WHERE asset.value NOT IN (
                        'crypto', 'equity', 'etf', 'index', 'futures',
                        'options', 'fx', 'commodities'
                     )
               )
            THEN
                RAISE EXCEPTION 'binding has invalid asset_classes_allowed';
            END IF;
            IF NEW.allowed_brokers IS NOT NULL THEN
                IF json_typeof(NEW.allowed_brokers) <> 'array'
                   OR json_array_length(NEW.allowed_brokers) = 0
                   OR EXISTS (
                        SELECT 1
                          FROM json_array_elements_text(NEW.allowed_brokers) AS allowed(value)
                          LEFT JOIN brokers ON brokers.code = allowed.value
                         WHERE brokers.broker_id IS NULL
                   )
                THEN
                    RAISE EXCEPTION 'binding has invalid allowed_brokers';
                END IF;
                SELECT broker.code
                  INTO selected_broker
                  FROM linked_broker_accounts AS account
                  JOIN brokers AS broker ON broker.broker_id = account.broker_id
                 WHERE account.account_id = NEW.broker_account_id;
                IF selected_broker IS NULL
                   OR NOT (
                        NEW.allowed_brokers::jsonb
                        @> to_jsonb(ARRAY[selected_broker]::TEXT[])
                   )
                THEN
                    RAISE EXCEPTION 'binding account broker is outside allowed_brokers';
                END IF;
            END IF;

            IF NOT NEW.is_active THEN
                RETURN NEW;
            END IF;
            IF NEW.strategy_id IS NULL OR length(trim(NEW.strategy_id)) = 0 THEN
                RAISE EXCEPTION 'active binding requires explicit strategy_id';
            END IF;
            new_scope := vm_binding_instrument_ids(NEW.instruments_allowed);
            IF EXISTS (
                SELECT 1
                  FROM user_strategy_bindings AS existing
                 WHERE existing.binding_id IS DISTINCT FROM NEW.binding_id
                   AND existing.broker_account_id = NEW.broker_account_id
                   AND existing.is_active
                   AND existing.strategy_id IS DISTINCT FROM NEW.strategy_id
                   AND (
                        new_scope IS NULL
                        OR vm_binding_instrument_ids(existing.instruments_allowed) IS NULL
                        OR new_scope && vm_binding_instrument_ids(existing.instruments_allowed)
                   )
            )
            THEN
                RAISE EXCEPTION
                    'another active strategy owns an overlapping account/instrument scope';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_validate_strategy_binding
        BEFORE INSERT OR UPDATE OF
            strategy_id, broker_account_id, execution_modes_allowed, preferred_mode,
            asset_classes_allowed, instruments_allowed, allowed_brokers,
            is_active, autopilot, entries_enabled, exits_enabled
        ON user_strategy_bindings
        FOR EACH ROW
        EXECUTE FUNCTION vm_validate_strategy_binding();
        """
    )


def _drop_constraints(table: str, names: Iterable[str]) -> None:
    with op.batch_alter_table(table) as batch:
        for name in names:
            batch.drop_constraint(name, type_="check")


def upgrade() -> None:
    op.add_column(
        "user_strategy_bindings",
        sa.Column("entries_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "user_strategy_bindings",
        sa.Column("exits_enabled", sa.Boolean(), nullable=True),
    )
    op.execute(
        """
        UPDATE user_strategy_bindings
           SET entries_enabled = (is_active AND autopilot),
               exits_enabled = (is_active AND autopilot)
        """
    )
    bind = op.get_bind()
    _normalize_active_binding_scopes(bind)

    with op.batch_alter_table("user_strategy_bindings") as batch:
        batch.alter_column(
            "entries_enabled",
            nullable=False,
            server_default=sa.false(),
        )
        batch.alter_column(
            "exits_enabled",
            nullable=False,
            server_default=sa.false(),
        )
        batch.create_check_constraint(
            "ck_binding_asset_score_threshold_range",
            "asset_score_threshold >= 0 AND asset_score_threshold <= 1",
        )
        batch.create_check_constraint(
            "ck_binding_sector_score_threshold_range",
            "sector_score_threshold IS NULL OR "
            "(sector_score_threshold >= 0 AND sector_score_threshold <= 1)",
        )
        batch.create_check_constraint(
            "ck_binding_market_score_threshold_range",
            "market_score_threshold IS NULL OR "
            "(market_score_threshold >= 0 AND market_score_threshold <= 1)",
        )
        batch.create_check_constraint(
            "ck_binding_max_position_pct_range",
            "max_position_pct > 0 AND max_position_pct <= 1",
        )
        batch.create_check_constraint(
            "ck_binding_max_daily_loss_pct_range",
            "max_daily_loss_pct >= 0 AND max_daily_loss_pct <= 1",
        )
        batch.create_check_constraint(
            "ck_binding_max_open_positions_range",
            "max_open_positions > 0 AND max_open_positions <= 1000",
        )
        batch.create_check_constraint(
            "ck_binding_inactive_has_no_authority",
            "is_active OR (NOT entries_enabled AND NOT exits_enabled)",
        )
        batch.create_check_constraint(
            "ck_binding_active_strategy_required",
            "NOT is_active OR "
            "(strategy_id IS NOT NULL AND length(trim(strategy_id)) > 0)",
        )
        batch.create_check_constraint(
            "ck_binding_entries_require_autopilot",
            "autopilot OR NOT entries_enabled",
        )

    op.add_column("outbox_events", sa.Column("failure_class", sa.String(20)))
    op.add_column(
        "outbox_events",
        sa.Column(
            "redrive_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "redrive_audit",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    with op.batch_alter_table("outbox_events") as batch:
        batch.create_check_constraint(
            "ck_outbox_failure_class",
            "failure_class IS NULL OR failure_class IN ('transient', 'permanent')",
        )
        batch.create_check_constraint(
            "ck_outbox_redrive_generation",
            "redrive_generation >= 0",
        )
        batch.create_index(
            "ix_outbox_topic_failure_class",
            ["topic", "status", "failure_class"],
        )
        # The default is needed only to backfill pre-existing rows while adding
        # the NOT NULL column.  Do not retain a PostgreSQL JSON server default:
        # JSON has no equality operator, which makes Alembic's server-default
        # drift comparison fail before it can report the schema inventory.
        # Runtime writers always persist the explicit empty audit list.
        batch.alter_column("redrive_audit", server_default=None)

    if bind.dialect.name == "postgresql":
        _create_postgresql_binding_trigger()


def downgrade() -> None:
    bind = op.get_bind()
    lossy_authority = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
              FROM user_strategy_bindings
             WHERE entries_enabled != (is_active AND autopilot)
                OR exits_enabled != (is_active AND autopilot)
            """
        )
    ).scalar_one()
    if int(lossy_authority):
        msg = "cannot drop explicit binding authority while close-only state exists"
        raise RuntimeError(msg)
    redrive_history = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
              FROM outbox_events
             WHERE redrive_generation > 0
                OR CAST(redrive_audit AS TEXT) <> '[]'
            """
        )
    ).scalar_one()
    if int(redrive_history):
        msg = "cannot drop outbox redrive audit history"
        raise RuntimeError(msg)

    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_validate_strategy_binding "
            "ON user_strategy_bindings"
        )
        op.execute("DROP FUNCTION IF EXISTS vm_validate_strategy_binding()")
        op.execute("DROP FUNCTION IF EXISTS vm_binding_instrument_ids(JSON)")

    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_index("ix_outbox_topic_failure_class")
        batch.drop_constraint("ck_outbox_redrive_generation", type_="check")
        batch.drop_constraint("ck_outbox_failure_class", type_="check")
    op.drop_column("outbox_events", "redrive_audit")
    op.drop_column("outbox_events", "redrive_generation")
    op.drop_column("outbox_events", "failure_class")

    _drop_constraints(
        "user_strategy_bindings",
        (
            "ck_binding_entries_require_autopilot",
            "ck_binding_active_strategy_required",
            "ck_binding_inactive_has_no_authority",
            "ck_binding_max_open_positions_range",
            "ck_binding_max_daily_loss_pct_range",
            "ck_binding_max_position_pct_range",
            "ck_binding_market_score_threshold_range",
            "ck_binding_sector_score_threshold_range",
            "ck_binding_asset_score_threshold_range",
        ),
    )
    op.drop_column("user_strategy_bindings", "exits_enabled")
    op.drop_column("user_strategy_bindings", "entries_enabled")
