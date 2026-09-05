"""Fence indicator source revisions without granting price mutation.

Revision ID: 0081_indicator_source_lock
Revises: 0080_execution_binding_read
"""

from __future__ import annotations

from alembic import op

revision = "0081_indicator_source_lock"
down_revision = "0080_execution_binding_read"
branch_labels = None
depends_on = None

_SOURCE_LOCK_SIGNATURE = (
    "public.vm_indicator_lock_source_price("
    "bigint, integer, timestamp without time zone, "
    "character varying, character varying, bigint)"
)


def _create_postgresql_indicator_source_lock() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.vm_indicator_lock_source_price(
            p_price_id bigint,
            p_instr_id integer,
            p_ts timestamp without time zone,
            p_timeframe character varying,
            p_source character varying,
            p_content_revision bigint
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            locked_price_id bigint;
        BEGIN
            SELECT price.price_id
              INTO locked_price_id
              FROM public.prices AS price
             WHERE price.price_id = p_price_id
               AND price.instr_id = p_instr_id
               AND price.ts = p_ts
               AND price.timeframe = p_timeframe
               AND price.source = p_source
               AND price.content_revision = p_content_revision
             FOR SHARE;
            RETURN FOUND;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_SOURCE_LOCK_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SOURCE_LOCK_SIGNATURE} TO vm_indicator")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _create_postgresql_indicator_source_lock()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP FUNCTION IF EXISTS {_SOURCE_LOCK_SIGNATURE}")
