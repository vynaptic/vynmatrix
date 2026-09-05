"""Add account-wide execution generations and terminal target fences.

Revision ID: 0089_account_execution_fence
Revises: 0088_portfolio_rebalances
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_account_execution_fence"
down_revision = "0088_portfolio_rebalances"
branch_labels = None
depends_on = None

_EXECUTION_FENCE_TABLE = "account_execution_generations"
_PLAN_TABLE = "account_rebalance_plans"
_LEG_TABLE = "account_rebalance_plan_legs"
_PLAN_TERMINAL_FENCE_CHECK = (
    "(completion_account_generation IS NULL AND terminal_targets_sha256 IS NULL) OR "
    "(completion_account_generation IS NOT NULL "
    "AND terminal_targets_sha256 IS NOT NULL "
    "AND completion_account_generation > 0 "
    "AND length(terminal_targets_sha256) = 64)"
)
_LEG_TARGET_FENCE_CHECK = (
    "(frozen_target_quantity IS NULL AND target_account_generation IS NULL "
    "AND target_frozen_at IS NULL) OR "
    "(frozen_target_quantity IS NOT NULL AND target_account_generation IS NOT NULL "
    "AND target_frozen_at IS NOT NULL AND frozen_target_quantity >= 0 "
    "AND target_account_generation > 0)"
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(*, command: str) -> None:
    name = f"{_EXECUTION_FENCE_TABLE}_execution_{command.lower()}"
    if command == "SELECT":
        clause = "FOR SELECT TO vm_execution USING (true)"
    elif command == "INSERT":
        clause = "FOR INSERT TO vm_execution WITH CHECK (true)"
    elif command == "UPDATE":
        clause = "FOR UPDATE TO vm_execution USING (true) WITH CHECK (true)"
    else:
        message = f"Unsupported account execution fence RLS command: {command}"
        raise ValueError(message)
    op.execute(f"CREATE POLICY {name} ON {_EXECUTION_FENCE_TABLE} {clause}")


def _enable_postgresql_execution_fence_access() -> None:
    op.execute(f"ALTER TABLE {_EXECUTION_FENCE_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{_EXECUTION_FENCE_TABLE} TO vm_execution"
    )
    for command in ("SELECT", "INSERT", "UPDATE"):
        _policy(command=command)


def _account_rebalance_plan_protection_sql(*, terminal_fenced: bool) -> str:
    pristine_fence = (
        """
                    OR NEW.completion_account_generation IS NOT NULL
                    OR NEW.terminal_targets_sha256 IS NOT NULL"""
        if terminal_fenced
        else ""
    )
    terminal_fence = (
        """
            IF NEW.status IN ('completed', 'degraded') THEN
                IF NEW.completion_account_generation IS NULL
                    OR NEW.completion_account_generation <= 0
                    OR NEW.terminal_targets_sha256 IS NULL
                    OR length(NEW.terminal_targets_sha256) <> 64 THEN
                    RAISE EXCEPTION
                        'terminal completed plan requires account generation and target digest'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.completion_account_generation IS NOT NULL
                OR NEW.terminal_targets_sha256 IS NOT NULL THEN
                RAISE EXCEPTION
                    'non-completed plan cannot persist terminal target authority'
                    USING ERRCODE = '23514';
            END IF;"""
        if terminal_fenced
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.vm_protect_account_rebalance_plan()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT (
                (NEW.phase = 'exit'
                    AND NEW.status IN ('pending', 'running', 'blocked'))
                OR (NEW.phase IN ('account_refresh', 'entry')
                    AND NEW.status IN ('running', 'blocked'))
                OR (NEW.phase = 'complete'
                    AND NEW.status IN ('completed', 'degraded'))
                OR (NEW.phase = 'failed' AND NEW.status IN ('failed', 'cancelled'))
                OR (NEW.phase = 'expired' AND NEW.status = 'expired')
            ) THEN
                RAISE EXCEPTION 'invalid account rebalance plan phase/status pair: %/%',
                    NEW.phase, NEW.status
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.phase <> 'exit' OR NEW.status <> 'pending'
                    OR NEW.fence_generation <> 0
                    OR NEW.lease_owner IS NOT NULL
                    OR NEW.lease_expires_at IS NOT NULL
                    OR NEW.last_progress_at IS NOT NULL
                    OR NEW.last_error_code IS NOT NULL
                    OR NEW.last_error_detail IS NOT NULL{pristine_fence} THEN
                    RAISE EXCEPTION
                        'account rebalance plan must begin in pristine exit/pending state'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status IN ('completed', 'degraded', 'failed', 'expired', 'cancelled') THEN
                RAISE EXCEPTION 'terminal account rebalance plan progress is immutable'
                    USING ERRCODE = '55000';
            END IF;{terminal_fence}
            IF ROW(
                NEW.account_plan_id, NEW.model_rebalance_id, NEW.user_id,
                NEW.binding_id, NEW.broker_account_id, NEW.strategy_id,
                NEW.data_use_scope, NEW.provider_authority_sha256,
                NEW.decision_cutoff, NEW.execute_not_before,
                NEW.execution_session_sha256, NEW.expires_at,
                NEW.execution_policy_sha256, NEW.broker_route_sha256,
                NEW.content_sha256, NEW.model_leg_count,
                NEW.execution_leg_count, NEW.intentional_cash_slots,
                NEW.progress_deadline_at, NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.account_plan_id, OLD.model_rebalance_id, OLD.user_id,
                OLD.binding_id, OLD.broker_account_id, OLD.strategy_id,
                OLD.data_use_scope, OLD.provider_authority_sha256,
                OLD.decision_cutoff, OLD.execute_not_before,
                OLD.execution_session_sha256, OLD.expires_at,
                OLD.execution_policy_sha256, OLD.broker_route_sha256,
                OLD.content_sha256, OLD.model_leg_count,
                OLD.execution_leg_count, OLD.intentional_cash_slots,
                OLD.progress_deadline_at, OLD.created_at
            ) OR NEW.execution_policy::jsonb IS DISTINCT FROM OLD.execution_policy::jsonb
              OR NEW.broker_route::jsonb IS DISTINCT FROM OLD.broker_route::jsonb THEN
                RAISE EXCEPTION 'frozen account rebalance plan content is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (
                (NEW.phase = OLD.phase AND (
                    (OLD.status = 'pending'
                        AND NEW.status IN ('pending', 'running', 'blocked'))
                    OR (OLD.status = 'running'
                        AND NEW.status IN ('running', 'blocked'))
                    OR (OLD.status = 'blocked'
                        AND NEW.status IN ('blocked', 'running'))
                ))
                OR (OLD.phase = 'exit' AND OLD.status = 'running'
                    AND NEW.phase = 'account_refresh' AND NEW.status = 'running')
                OR (OLD.phase = 'account_refresh' AND OLD.status = 'running'
                    AND NEW.phase = 'entry' AND NEW.status = 'running')
                OR (OLD.phase IN ('exit', 'entry') AND OLD.status = 'running'
                    AND NEW.phase = 'complete'
                    AND NEW.status IN ('completed', 'degraded'))
                OR (OLD.phase IN ('exit', 'account_refresh', 'entry')
                    AND OLD.status IN ('pending', 'running', 'blocked')
                    AND NEW.phase = 'failed'
                    AND NEW.status IN ('failed', 'cancelled'))
                OR (OLD.phase IN ('exit', 'account_refresh', 'entry')
                    AND OLD.status IN ('pending', 'running', 'blocked')
                    AND NEW.phase = 'expired' AND NEW.status = 'expired')
            ) THEN
                RAISE EXCEPTION
                    'invalid account rebalance plan transition: %/% -> %/%',
                    OLD.phase, OLD.status, NEW.phase, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """


def _account_rebalance_leg_protection_sql(*, target_fenced: bool) -> str:
    pristine_fence = (
        """
                    OR NEW.frozen_target_quantity IS NOT NULL
                    OR NEW.target_account_generation IS NOT NULL
                    OR NEW.target_frozen_at IS NOT NULL"""
        if target_fenced
        else ""
    )
    target_fence = (
        """
            IF OLD.frozen_target_quantity IS NOT NULL AND ROW(
                NEW.frozen_target_quantity,
                NEW.target_account_generation,
                NEW.target_frozen_at
            ) IS DISTINCT FROM ROW(
                OLD.frozen_target_quantity,
                OLD.target_account_generation,
                OLD.target_frozen_at
            ) THEN
                RAISE EXCEPTION 'frozen account rebalance target is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.frozen_target_quantity IS NULL AND NOT (
                (NEW.frozen_target_quantity IS NULL
                    AND NEW.target_account_generation IS NULL
                    AND NEW.target_frozen_at IS NULL)
                OR (NEW.frozen_target_quantity IS NOT NULL
                    AND NEW.target_account_generation IS NOT NULL
                    AND NEW.frozen_target_quantity >= 0
                    AND NEW.target_account_generation > 0
                    AND NEW.target_frozen_at IS NOT NULL)
            ) THEN
                RAISE EXCEPTION 'account rebalance target fence is incomplete'
                    USING ERRCODE = '23514';
            END IF;"""
        if target_fenced
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.vm_protect_account_rebalance_leg()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending'
                    OR NEW.last_progress_at IS NOT NULL
                    OR NEW.last_error_code IS NOT NULL{pristine_fence} THEN
                    RAISE EXCEPTION
                        'account rebalance leg must begin in pristine pending state'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status IN ('confirmed', 'rejected', 'failed', 'skipped') THEN
                RAISE EXCEPTION 'terminal account rebalance leg progress is immutable'
                    USING ERRCODE = '55000';
            END IF;{target_fence}
            IF ROW(
                NEW.account_plan_id, NEW.user_id, NEW.broker_account_id,
                NEW.model_sequence, NEW.command_sequence,
                NEW.model_rebalance_id, NEW.model_leg_id, NEW.plan_leg_id,
                NEW.model_signal_snapshot_sha256,
                NEW.instr_id, NEW.external_signal_id, NEW.symbol,
                NEW.phase, NEW.action, NEW.disposition, NEW.rank_position,
                NEW.allocation_hint, NEW.required,
                NEW.reason_code
            ) IS DISTINCT FROM ROW(
                OLD.account_plan_id, OLD.user_id, OLD.broker_account_id,
                OLD.model_sequence, OLD.command_sequence,
                OLD.model_rebalance_id, OLD.model_leg_id, OLD.plan_leg_id,
                OLD.model_signal_snapshot_sha256,
                OLD.instr_id, OLD.external_signal_id, OLD.symbol,
                OLD.phase, OLD.action, OLD.disposition, OLD.rank_position,
                OLD.allocation_hint, OLD.required,
                OLD.reason_code
            ) OR NEW.depends_on_sequences::jsonb
                    IS DISTINCT FROM OLD.depends_on_sequences::jsonb THEN
                RAISE EXCEPTION 'frozen account rebalance leg content is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (
                (OLD.status = 'pending' AND NEW.status IN (
                    'pending', 'submitted', 'partial',
                    'confirmed', 'rejected', 'failed', 'skipped'
                ))
                OR (OLD.status = 'submitted' AND NEW.status IN (
                    'submitted', 'partial', 'confirmed', 'rejected', 'failed'
                ))
                OR (OLD.status = 'partial' AND NEW.status IN (
                    'partial', 'confirmed', 'rejected', 'failed'
                ))
            ) THEN
                RAISE EXCEPTION 'invalid account rebalance leg transition: % -> %',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """


def _terminal_coherence_sql(*, target_fenced: bool) -> str:
    target_declaration = "\n            missing_target_count bigint;" if target_fenced else ""
    if target_fenced:
        terminal_select = """
                'count(*) FILTER (WHERE disposition = ''selected'' '
                    'AND phase = ''entry'' '
                    'AND status IN (''rejected'', ''failed'', ''skipped'')), '
                'count(*) FILTER (WHERE frozen_target_quantity IS NULL AND ('
                    'disposition = ''selected'' OR reason_code IN ('
                        '''entry_authority_disabled_hold_preserved'', '
                        '''manual_approval_required_hold_preserved''))) '"""
    else:
        terminal_select = """
                'count(*) FILTER (WHERE disposition = ''selected'' '
                    'AND phase = ''entry'' '
                    'AND status IN (''rejected'', ''failed'', ''skipped'')) '"""
    target_into = ", missing_target_count" if target_fenced else ""
    target_check = (
        """
            IF NEW.status IN ('completed', 'degraded') AND missing_target_count <> 0 THEN
                RAISE EXCEPTION
                    'terminal account rebalance plan % has unfrozen target quantities',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;"""
        if target_fenced
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.vm_assert_account_rebalance_terminal_coherent()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            open_count bigint;
            selected_nonconfirmed_count bigint;
            rejected_nonskipped_count bigint;
            reduction_nonconfirmed_count bigint;
            degraded_entry_count bigint;{target_declaration}
        BEGIN
            IF NEW.status NOT IN (
                'completed', 'degraded', 'failed', 'expired', 'cancelled'
            ) THEN
                RETURN NEW;
            END IF;
            EXECUTE format(
                'SELECT '
                'count(*) FILTER (WHERE status NOT IN '
                    '(''confirmed'', ''rejected'', ''failed'', ''skipped'')), '
                'count(*) FILTER (WHERE disposition = ''selected'' '
                    'AND status <> ''confirmed''), '
                'count(*) FILTER (WHERE disposition = ''rejected'' '
                    'AND status <> ''skipped''), '
                'count(*) FILTER (WHERE disposition = ''selected'' '
                    'AND phase IN (''exit'', ''reduce'') '
                    'AND status <> ''confirmed''), '
                {terminal_select}
                'FROM %I.account_rebalance_plan_legs WHERE account_plan_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO open_count, selected_nonconfirmed_count,
                rejected_nonskipped_count, reduction_nonconfirmed_count,
                degraded_entry_count{target_into}
            USING NEW.account_plan_id;
            IF open_count <> 0 THEN
                RAISE EXCEPTION
                    'terminal account rebalance plan % has nonterminal legs',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;{target_check}
            IF NEW.status = 'completed' AND (
                selected_nonconfirmed_count <> 0 OR rejected_nonskipped_count <> 0
            ) THEN
                RAISE EXCEPTION
                    'completed account rebalance plan % has incoherent leg outcomes',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.status = 'degraded' AND (
                reduction_nonconfirmed_count <> 0
                OR rejected_nonskipped_count <> 0
                OR degraded_entry_count = 0
            ) THEN
                RAISE EXCEPTION
                    'degraded account rebalance plan % has incoherent leg outcomes',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """


def _replace_postgresql_rebalance_guards(*, target_fenced: bool) -> None:
    op.execute(_account_rebalance_plan_protection_sql(terminal_fenced=target_fenced))
    op.execute(_account_rebalance_leg_protection_sql(target_fenced=target_fenced))
    op.execute(_terminal_coherence_sql(target_fenced=target_fenced))


def _add_fence_constraints() -> None:
    if _is_postgresql():
        op.execute(
            f"ALTER TABLE {_PLAN_TABLE} ADD CONSTRAINT "
            "ck_account_rebalance_plan_terminal_fence CHECK "
            f"({_PLAN_TERMINAL_FENCE_CHECK}) NOT VALID"
        )
        op.execute(
            f"ALTER TABLE {_PLAN_TABLE} VALIDATE CONSTRAINT "
            "ck_account_rebalance_plan_terminal_fence"
        )
        op.execute(
            f"ALTER TABLE {_LEG_TABLE} ADD CONSTRAINT "
            "ck_account_rebalance_leg_target_fence CHECK "
            f"({_LEG_TARGET_FENCE_CHECK}) NOT VALID"
        )
        op.execute(
            f"ALTER TABLE {_LEG_TABLE} VALIDATE CONSTRAINT ck_account_rebalance_leg_target_fence"
        )
        return
    with op.batch_alter_table(_PLAN_TABLE) as batch_op:
        batch_op.create_check_constraint(
            "ck_account_rebalance_plan_terminal_fence",
            _PLAN_TERMINAL_FENCE_CHECK,
        )
    with op.batch_alter_table(_LEG_TABLE) as batch_op:
        batch_op.create_check_constraint(
            "ck_account_rebalance_leg_target_fence",
            _LEG_TARGET_FENCE_CHECK,
        )


def upgrade() -> None:
    op.create_table(
        _EXECUTION_FENCE_TABLE,
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("active_owner", sa.String(length=100), nullable=True),
        sa.Column("active_writer_kind", sa.String(length=24), nullable=True),
        sa.Column("active_account_plan_id", sa.String(length=64), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_account_execution_generation_positive",
        ),
        sa.CheckConstraint(
            "active_writer_kind IS NULL OR active_writer_kind IN "
            "('ordinary', 'manual', 'rebalance', 'historical_replay', "
            "'paper_lifecycle', 'reconciliation')",
            name="ck_account_execution_generation_writer_kind",
        ),
        sa.CheckConstraint(
            "active_account_plan_id IS NULL OR length(active_account_plan_id) = 64",
            name="ck_account_execution_generation_plan_digest",
        ),
        sa.CheckConstraint(
            "(active_owner IS NULL AND active_writer_kind IS NULL "
            "AND active_account_plan_id IS NULL AND acquired_at IS NULL) OR "
            "(length(trim(active_owner)) > 0 AND active_writer_kind IS NOT NULL "
            "AND acquired_at IS NOT NULL)",
            name="ck_account_execution_generation_active",
        ),
        sa.ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_account_execution_generation_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "broker_account_id"),
    )
    op.create_index(
        "ix_account_execution_generation_active",
        _EXECUTION_FENCE_TABLE,
        ["active_owner", "updated_at"],
    )
    op.add_column(
        _PLAN_TABLE,
        sa.Column("completion_account_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _PLAN_TABLE,
        sa.Column("terminal_targets_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        _LEG_TABLE,
        sa.Column("frozen_target_quantity", sa.Numeric(30, 12), nullable=True),
    )
    op.add_column(
        _LEG_TABLE,
        sa.Column("target_account_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        _LEG_TABLE,
        sa.Column("target_frozen_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_fence_constraints()
    if _is_postgresql():
        _enable_postgresql_execution_fence_access()
        _replace_postgresql_rebalance_guards(target_fenced=True)


def downgrade() -> None:
    retained = {
        _EXECUTION_FENCE_TABLE: int(
            op.get_bind()
            .execute(sa.text(f"SELECT count(*) FROM {_EXECUTION_FENCE_TABLE}"))
            .scalar_one()
        ),
        "terminal_plan_fences": int(
            op.get_bind()
            .execute(
                sa.text(
                    f"SELECT count(*) FROM {_PLAN_TABLE} "
                    "WHERE completion_account_generation IS NOT NULL "
                    "OR terminal_targets_sha256 IS NOT NULL"
                )
            )
            .scalar_one()
        ),
        "frozen_leg_targets": int(
            op.get_bind()
            .execute(
                sa.text(
                    f"SELECT count(*) FROM {_LEG_TABLE} "
                    "WHERE frozen_target_quantity IS NOT NULL "
                    "OR target_account_generation IS NOT NULL "
                    "OR target_frozen_at IS NOT NULL"
                )
            )
            .scalar_one()
        ),
    }
    if any(retained.values()):
        message = f"Cannot remove immutable account execution fence history: {retained}"
        raise RuntimeError(message)

    if _is_postgresql():
        _replace_postgresql_rebalance_guards(target_fenced=False)
        op.execute(
            f"ALTER TABLE {_LEG_TABLE} DROP CONSTRAINT ck_account_rebalance_leg_target_fence"
        )
        op.execute(
            f"ALTER TABLE {_PLAN_TABLE} DROP CONSTRAINT ck_account_rebalance_plan_terminal_fence"
        )
    else:
        with op.batch_alter_table(_LEG_TABLE) as batch_op:
            batch_op.drop_constraint(
                "ck_account_rebalance_leg_target_fence",
                type_="check",
            )
        with op.batch_alter_table(_PLAN_TABLE) as batch_op:
            batch_op.drop_constraint(
                "ck_account_rebalance_plan_terminal_fence",
                type_="check",
            )

    with op.batch_alter_table(_LEG_TABLE) as batch_op:
        batch_op.drop_column("target_frozen_at")
        batch_op.drop_column("target_account_generation")
        batch_op.drop_column("frozen_target_quantity")
    with op.batch_alter_table(_PLAN_TABLE) as batch_op:
        batch_op.drop_column("terminal_targets_sha256")
        batch_op.drop_column("completion_account_generation")
    op.drop_index(
        "ix_account_execution_generation_active",
        table_name=_EXECUTION_FENCE_TABLE,
    )
    op.drop_table(_EXECUTION_FENCE_TABLE)
