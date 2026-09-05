"""Allow feedback to lock one eligible strategy release without catalogue writes.

The feedback runtime deliberately has SELECT-only access to ``strategies`` and
``strategy_versions``. PostgreSQL row locks require an UPDATE privilege on the
locked relation, so suggestion creation uses this narrowly scoped
``SECURITY DEFINER`` function instead of broadening the runtime role.

Revision ID: 0073_feedback_catalogue_gate
Revises: 0072_feedback_runtime_integrity
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op

revision = "0073_feedback_catalogue_gate"
down_revision = "0072_feedback_runtime_integrity"
branch_labels = None
depends_on = None

_FUNCTION_SIGNATURE = "public.vm_feedback_exact_strategy_version_active(character varying, bigint)"


def _create_feedback_eligibility_gate() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.vm_feedback_exact_strategy_version_active(
            p_strategy_id character varying,
            p_strat_ver_id bigint
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            PERFORM 1
            FROM public.strategies
            WHERE strategy_id = p_strategy_id
              AND is_active IS TRUE
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RETURN FALSE;
            END IF;

            PERFORM 1
            FROM public.strategy_versions
            WHERE strat_ver_id = p_strat_ver_id
              AND strategy_id = p_strategy_id
              AND status = 'active'
            FOR KEY SHARE;
            RETURN FOUND;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE} TO vm_feedback")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _create_feedback_eligibility_gate()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_SIGNATURE}")
