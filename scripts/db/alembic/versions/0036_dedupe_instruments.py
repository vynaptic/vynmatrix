"""De-duplicate normalized-equivalent instruments + guard against recurrence (H2).

A second instrument row could be created for a separator/case variant of an
existing pair (e.g. ``BTCUSD`` id 11 alongside ``BTC/USD`` id 1 + alias
``BTCUSD``), because upsert_instrument matched canonical exactly and ignored the
alias. The duplicate carries no prices, so scores/signals/positions fragment
across two instr_ids and the price lookup starves on the phantom row.

This migration (postgres only; sqlite test fixtures build from the models via
create_all, so it no-ops there):
  1. Merges any group of instruments whose canonical normalizes to the same key:
     keeps the LOWEST instr_id (the original canonical, which holds the price
     history), repoints every FK column referencing instruments.instr_id to the
     keeper, and deletes the duplicate. FK columns are discovered dynamically from
     pg_constraint so no referencing table is missed. (If a repoint would collide
     with a unique constraint on a child table the statement raises and the whole
     migration rolls back — fail-safe; reconcile that table manually.)
  2. Adds a UNIQUE expression index on the normalized canonical so a duplicate can
     never be created again (matches normalize_product_symbol: strip '/','-','_',
     uppercase).

Revision ID: 0036_dedupe_instruments
Revises: 0035_asset_score_ext_signal_id
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

revision = "0036_dedupe_instruments"
down_revision = "0035_asset_score_ext_signal_id"
branch_labels = None
depends_on = None

_MERGE_DUPLICATES = """
DO $$
DECLARE
    grp RECORD;
    keeper BIGINT;
    dup BIGINT;
    fk RECORD;
BEGIN
    FOR grp IN
        SELECT upper(translate(canonical, '/-_', '')) AS norm,
               array_agg(instr_id ORDER BY instr_id) AS ids
        FROM instruments
        GROUP BY 1
        HAVING count(*) > 1
    LOOP
        keeper := grp.ids[1];
        FOREACH dup IN ARRAY grp.ids[2:array_length(grp.ids, 1)] LOOP
            FOR fk IN
                SELECT conrelid::regclass::text AS tbl, a.attname AS col
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                WHERE c.confrelid = 'instruments'::regclass AND c.contype = 'f'
            LOOP
                EXECUTE format(
                    'UPDATE %s SET %I = %L WHERE %I = %L',
                    fk.tbl, fk.col, keeper, fk.col, dup
                );
            END LOOP;
            DELETE FROM instruments WHERE instr_id = dup;
            RAISE NOTICE 'dedupe_instruments: merged instr_id % into %', dup, keeper;
        END LOOP;
    END LOOP;
END $$;
"""

_CREATE_GUARD = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_instruments_norm_canonical "
    "ON instruments (upper(translate(canonical, '/-_', '')))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.execute(_MERGE_DUPLICATES)
    op.execute(_CREATE_GUARD)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.execute("DROP INDEX IF EXISTS uq_instruments_norm_canonical")
