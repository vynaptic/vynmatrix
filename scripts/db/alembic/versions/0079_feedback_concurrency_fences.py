"""Fence feedback cycles and strategy-version retirement.

Revision ID: 0079_feedback_concurrency_fences
Revises: 0078_feedback_exact_lineage
"""

from __future__ import annotations

from alembic import op

revision = "0079_feedback_concurrency_fences"
down_revision = "0078_feedback_exact_lineage"
branch_labels = None
depends_on = None

_FUNCTION_SIGNATURE = "public.vm_feedback_exact_strategy_version_active(character varying, bigint)"


def _replace_feedback_eligibility_gate(*, lock_mode: str) -> None:
    if lock_mode not in {"KEY SHARE", "SHARE"}:
        message = f"Unsupported catalogue lock mode: {lock_mode}"
        raise ValueError(message)
    op.execute(
        f"""
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
            FOR {lock_mode};
            IF NOT FOUND THEN
                RETURN FALSE;
            END IF;

            PERFORM 1
            FROM public.strategy_versions
            WHERE strat_ver_id = p_strat_ver_id
              AND strategy_id = p_strategy_id
              AND status = 'active'
            FOR {lock_mode};
            RETURN FOUND;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNCTION_SIGNATURE} TO vm_feedback")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # FOR KEY SHARE does not conflict with an UPDATE of ``status`` or
        # ``is_active``. FOR SHARE keeps retirement from overtaking an
        # eligibility check until the feedback transaction commits.
        _replace_feedback_eligibility_gate(lock_mode="SHARE")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_feedback_eligibility_gate(lock_mode="KEY SHARE")
