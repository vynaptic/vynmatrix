"""Add atomic model rebalances and tenant/account execution plans.

Revision ID: 0088_portfolio_rebalances
Revises: 0087_synchronized_panel_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0088_portfolio_rebalances"
down_revision = "0087_synchronized_panel_runtime"
branch_labels = None
depends_on = None

_MODEL_TABLES = ("model_rebalances", "model_rebalance_legs")
_ACCOUNT_TABLES = ("account_rebalance_plans", "account_rebalance_plan_legs")
_RESOLUTION_TABLES = ("account_rebalance_plan_resolutions",)
_SEALED_AGGREGATES = {
    "model_rebalance_legs": ("model_rebalances", "rebalance_id", "rebalance_id"),
    "account_rebalance_plan_legs": (
        "account_rebalance_plans",
        "account_plan_id",
        "account_plan_id",
    ),
}


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _policy(table: str, *, role: str, command: str) -> None:
    name = f"{table}_{role.removeprefix('vm_')}_{command.lower()}"
    if command == "SELECT":
        clause = f"FOR SELECT TO {role} USING (true)"
    elif command == "INSERT":
        clause = f"FOR INSERT TO {role} WITH CHECK (true)"
    elif command == "UPDATE":
        clause = f"FOR UPDATE TO {role} USING (true) WITH CHECK (true)"
    else:
        message = f"Unsupported rebalance RLS command: {command}"
        raise ValueError(message)
    op.execute(f"CREATE POLICY {name} ON {table} {clause}")


def _enable_service_access() -> None:
    for table in (*_MODEL_TABLES, *_ACCOUNT_TABLES, *_RESOLUTION_TABLES):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    for table in _MODEL_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_scoring")
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO vm_execution")
        for command in ("SELECT", "INSERT"):
            _policy(table, role="vm_scoring", command=command)
        _policy(table, role="vm_execution", command="SELECT")

    for table in _ACCOUNT_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_scoring")
        op.execute(f"GRANT SELECT, UPDATE ON TABLE public.{table} TO vm_execution")
        for command in ("SELECT", "INSERT"):
            _policy(table, role="vm_scoring", command=command)
        for command in ("SELECT", "UPDATE"):
            _policy(table, role="vm_execution", command=command)

    for table in _RESOLUTION_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_execution")
        for command in ("SELECT", "INSERT"):
            _policy(table, role="vm_execution", command=command)


def _create_resolution_protection_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_reject_account_rebalance_resolution_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'account rebalance failure resolutions are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER account_rebalance_resolution_immutable "
        "BEFORE UPDATE OR DELETE ON account_rebalance_plan_resolutions FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_reject_account_rebalance_resolution_mutation()"
    )


def _create_protection_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_reject_model_rebalance_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'model rebalance evidence is immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in _MODEL_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable "
            f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION public.vm_reject_model_rebalance_mutation()"
        )
    for child, (parent, parent_key, child_key) in _SEALED_AGGREGATES.items():
        op.execute(
            f"CREATE TRIGGER {child}_aggregate_sealed "
            f"BEFORE INSERT ON {child} FOR EACH ROW "
            "EXECUTE FUNCTION public.vm_require_append_only_parent_current_xact"
            f"('{parent}', '{parent_key}', '{child_key}')"
        )

    op.execute(
        """
        CREATE FUNCTION public.vm_protect_account_rebalance_plan()
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
                    OR NEW.last_error_detail IS NOT NULL THEN
                    RAISE EXCEPTION
                        'account rebalance plan must begin in pristine exit/pending state'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status IN ('completed', 'degraded', 'failed', 'expired', 'cancelled') THEN
                RAISE EXCEPTION 'terminal account rebalance plan progress is immutable'
                    USING ERRCODE = '55000';
            END IF;
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
    )
    op.execute(
        "CREATE TRIGGER account_rebalance_plan_frozen "
        "BEFORE INSERT OR UPDATE ON account_rebalance_plans FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_protect_account_rebalance_plan()"
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_protect_account_rebalance_leg()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending'
                    OR NEW.last_progress_at IS NOT NULL
                    OR NEW.last_error_code IS NOT NULL THEN
                    RAISE EXCEPTION
                        'account rebalance leg must begin in pristine pending state'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status IN ('confirmed', 'rejected', 'failed', 'skipped') THEN
                RAISE EXCEPTION 'terminal account rebalance leg progress is immutable'
                    USING ERRCODE = '55000';
            END IF;
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
    )
    op.execute(
        "CREATE TRIGGER account_rebalance_leg_frozen "
        "BEFORE INSERT OR UPDATE ON account_rebalance_plan_legs FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_protect_account_rebalance_leg()"
    )
    _create_postgresql_rebalance_completeness()


def _create_postgresql_rebalance_completeness() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_assert_model_rebalance_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            leg_count bigint;
            minimum_sequence integer;
            maximum_sequence integer;
        BEGIN
            EXECUTE format(
                'SELECT count(*), min(sequence), max(sequence) '
                'FROM %I.model_rebalance_legs WHERE rebalance_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO leg_count, minimum_sequence, maximum_sequence
            USING NEW.rebalance_id;
            IF leg_count <> NEW.expected_leg_count
                OR (leg_count > 0 AND (
                    minimum_sequence <> 0 OR maximum_sequence <> leg_count - 1
                )) THEN
                RAISE EXCEPTION 'model rebalance % has incomplete aggregate children',
                    NEW.rebalance_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER model_rebalance_complete "
        "AFTER INSERT ON model_rebalances "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_model_rebalance_complete()"
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_assert_account_rebalance_plan_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            leg_count bigint;
            selected_count bigint;
            minimum_model_sequence integer;
            maximum_model_sequence integer;
            minimum_command_sequence integer;
            maximum_command_sequence integer;
        BEGIN
            EXECUTE format(
                'SELECT count(*), '
                'count(*) FILTER (WHERE disposition = ''selected''), '
                'min(model_sequence), max(model_sequence), '
                'min(command_sequence) FILTER (WHERE disposition = ''selected''), '
                'max(command_sequence) FILTER (WHERE disposition = ''selected'') '
                'FROM %I.account_rebalance_plan_legs WHERE account_plan_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO leg_count, selected_count,
                minimum_model_sequence, maximum_model_sequence,
                minimum_command_sequence, maximum_command_sequence
            USING NEW.account_plan_id;
            IF leg_count <> NEW.model_leg_count
                OR selected_count <> NEW.execution_leg_count
                OR (leg_count > 0 AND (
                    minimum_model_sequence <> 0
                    OR maximum_model_sequence <> leg_count - 1
                ))
                OR (selected_count > 0 AND (
                    minimum_command_sequence <> 0
                    OR maximum_command_sequence <> selected_count - 1
                )) THEN
                RAISE EXCEPTION
                    'account rebalance plan % has incomplete aggregate children',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER account_rebalance_plan_complete "
        "AFTER INSERT ON account_rebalance_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_plan_complete()"
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_assert_account_rebalance_terminal_coherent()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            open_count bigint;
            selected_nonconfirmed_count bigint;
            rejected_nonskipped_count bigint;
            reduction_nonconfirmed_count bigint;
            degraded_entry_count bigint;
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
                'count(*) FILTER (WHERE disposition = ''selected'' '
                    'AND phase = ''entry'' '
                    'AND status IN (''rejected'', ''failed'', ''skipped'')) '
                'FROM %I.account_rebalance_plan_legs WHERE account_plan_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO open_count, selected_nonconfirmed_count,
                rejected_nonskipped_count, reduction_nonconfirmed_count,
                degraded_entry_count
            USING NEW.account_plan_id;
            IF open_count <> 0 THEN
                RAISE EXCEPTION
                    'terminal account rebalance plan % has nonterminal legs',
                    NEW.account_plan_id
                    USING ERRCODE = '23514';
            END IF;
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
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER account_rebalance_plan_terminal_coherent "
        "AFTER INSERT OR UPDATE ON account_rebalance_plans "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_account_rebalance_terminal_coherent()"
    )


def upgrade() -> None:
    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.create_unique_constraint(
            "uq_user_strategy_binding_owner",
            ["binding_id", "user_id", "broker_account_id", "strategy_id"],
        )
    with op.batch_alter_table("equity_rank_snapshots") as batch_op:
        batch_op.create_unique_constraint(
            "uq_equity_rank_exact_model_lineage",
            [
                "rank_snapshot_id",
                "strategy_id",
                "strat_ver_id",
                "effective_session",
                "cutoff_at",
                "configuration_digest",
                "data_use_scope",
                "provider_authority_digest",
            ],
        )

    op.create_table(
        "model_rebalances",
        sa.Column("rebalance_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strat_ver_id", sa.BigInteger(), nullable=False),
        sa.Column("rank_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("effective_session", sa.Date(), nullable=False),
        sa.Column("decision_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execute_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_session_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_use_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("configuration_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_leg_count", sa.Integer(), nullable=False),
        sa.Column("intentional_cash_slots", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(rebalance_id) = 64 AND length(rank_snapshot_id) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(configuration_sha256) = 64 "
            "AND length(input_snapshot_sha256) = 64 AND length(content_sha256) = 64",
            name="ck_model_rebalance_digests",
        ),
        sa.CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_model_rebalance_data_use_scope",
        ),
        sa.CheckConstraint(
            "execute_not_before > decision_cutoff",
            name="ck_model_rebalance_execution_timing",
        ),
        sa.CheckConstraint(
            "expected_leg_count >= 0 AND intentional_cash_slots >= 0",
            name="ck_model_rebalance_counts",
        ),
        sa.ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_model_rebalance_strategy_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "rank_snapshot_id",
                "strategy_id",
                "strat_ver_id",
                "effective_session",
                "decision_cutoff",
                "configuration_sha256",
                "data_use_scope",
                "provider_authority_sha256",
            ],
            [
                "equity_rank_snapshots.rank_snapshot_id",
                "equity_rank_snapshots.strategy_id",
                "equity_rank_snapshots.strat_ver_id",
                "equity_rank_snapshots.effective_session",
                "equity_rank_snapshots.cutoff_at",
                "equity_rank_snapshots.configuration_digest",
                "equity_rank_snapshots.data_use_scope",
                "equity_rank_snapshots.provider_authority_digest",
            ],
            name="fk_model_rebalance_exact_rank_lineage",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("rebalance_id"),
        sa.UniqueConstraint("rebalance_id", "strategy_id", name="uq_model_rebalance_strategy"),
        sa.UniqueConstraint("rebalance_id", "rank_snapshot_id", name="uq_model_rebalance_rank"),
        sa.UniqueConstraint(
            "rebalance_id",
            "strategy_id",
            "data_use_scope",
            "provider_authority_sha256",
            "decision_cutoff",
            "execute_not_before",
            "execution_session_sha256",
            "expected_leg_count",
            name="uq_model_rebalance_exact_account_lineage",
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            name="uq_model_rebalance_strategy_version_session",
        ),
    )
    op.create_index(
        "ix_model_rebalance_strategy_session",
        "model_rebalances",
        ["strategy_id", "effective_session"],
    )

    op.create_table(
        "model_rebalance_legs",
        sa.Column("rebalance_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("leg_id", sa.String(length=128), nullable=False),
        sa.Column("leg_sha256", sa.String(length=64), nullable=False),
        sa.Column("signal_snapshot", sa.JSON(), nullable=False),
        sa.Column("signal_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("rank_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("factor_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("current_rank_row_present", sa.Boolean(), nullable=False),
        sa.Column("current_rank_instr_id", sa.Integer(), nullable=True),
        sa.Column("current_rank_factor_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("prior_model_rebalance_id", sa.String(length=64), nullable=True),
        sa.Column("prior_model_leg_id", sa.String(length=128), nullable=True),
        sa.Column("membership_change_reason", sa.String(length=500), nullable=True),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("external_signal_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("rank_position", sa.Numeric(20, 10), nullable=True),
        sa.Column("allocation_hint", sa.Numeric(18, 12), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="ck_model_rebalance_leg_sequence"),
        sa.CheckConstraint(
            "length(trim(leg_id)) > 0 AND length(leg_sha256) = 64 "
            "AND length(signal_snapshot_sha256) = 64 "
            "AND length(rank_snapshot_id) = 64 AND length(factor_snapshot_id) = 64 "
            "AND (current_rank_factor_snapshot_id IS NULL "
            "OR length(current_rank_factor_snapshot_id) = 64) "
            "AND (prior_model_rebalance_id IS NULL "
            "OR length(prior_model_rebalance_id) = 64)",
            name="ck_model_rebalance_leg_identity",
        ),
        sa.CheckConstraint(
            "(current_rank_row_present AND current_rank_instr_id IS NOT NULL "
            "AND current_rank_instr_id = instr_id AND "
            "((current_rank_factor_snapshot_id IS NOT NULL "
            "AND current_rank_factor_snapshot_id = factor_snapshot_id) OR "
            "(phase = 'exit' AND current_rank_factor_snapshot_id IS NULL "
            "AND rank_position IS NULL "
            "AND membership_change_reason IS NOT NULL "
            "AND length(trim(membership_change_reason)) > 0 "
            "AND (membership_change_reason <> 'panel_excluded' OR "
            "(prior_model_rebalance_id IS NOT NULL "
            "AND prior_model_leg_id IS NOT NULL))))) OR "
            "(NOT current_rank_row_present AND current_rank_instr_id IS NULL "
            "AND phase = 'exit' AND current_rank_factor_snapshot_id IS NULL "
            "AND rank_position IS NULL AND prior_model_rebalance_id IS NOT NULL "
            "AND prior_model_leg_id IS NOT NULL "
            "AND membership_change_reason IS NOT NULL "
            "AND membership_change_reason = 'left_effective_universe')",
            name="ck_model_rebalance_leg_current_or_prior_lineage",
        ),
        sa.CheckConstraint(
            "phase IN ('exit', 'reduce', 'hold', 'entry')",
            name="ck_model_rebalance_leg_phase",
        ),
        sa.CheckConstraint(
            "action IN ('long', 'hold', 'flat')",
            name="ck_model_rebalance_leg_action",
        ),
        sa.CheckConstraint(
            "(phase = 'entry' AND action = 'long') OR "
            "(phase IN ('hold', 'reduce') AND action = 'hold') OR "
            "(phase = 'exit' AND action = 'flat')",
            name="ck_model_rebalance_leg_phase_action",
        ),
        sa.CheckConstraint(
            "rank_position IS NULL OR rank_position > 0",
            name="ck_model_rebalance_leg_rank",
        ),
        sa.CheckConstraint(
            "allocation_hint >= 0 AND allocation_hint <= 1 "
            "AND (phase <> 'exit' OR allocation_hint = 0)",
            name="ck_model_rebalance_leg_allocation",
        ),
        sa.ForeignKeyConstraint(
            ["rebalance_id", "rank_snapshot_id"],
            ["model_rebalances.rebalance_id", "model_rebalances.rank_snapshot_id"],
            name="fk_model_rebalance_leg_rank",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rank_snapshot_id", "current_rank_instr_id"],
            [
                "equity_rank_snapshot_rows.rank_snapshot_id",
                "equity_rank_snapshot_rows.instr_id",
            ],
            name="fk_model_rebalance_leg_current_rank_row",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "rank_snapshot_id",
                "current_rank_instr_id",
                "current_rank_factor_snapshot_id",
            ],
            [
                "equity_rank_snapshot_rows.rank_snapshot_id",
                "equity_rank_snapshot_rows.instr_id",
                "equity_rank_snapshot_rows.factor_snapshot_id",
            ],
            name="fk_model_rebalance_leg_rank_row",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["factor_snapshot_id", "instr_id"],
            [
                "equity_factor_snapshots.factor_snapshot_id",
                "equity_factor_snapshots.instr_id",
            ],
            name="fk_model_rebalance_leg_factor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "prior_model_rebalance_id",
                "prior_model_leg_id",
                "instr_id",
                "factor_snapshot_id",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.factor_snapshot_id",
            ],
            name="fk_model_rebalance_leg_prior_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["external_signal_id"],
            ["canonical_signals.external_signal_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("rebalance_id", "sequence"),
        sa.UniqueConstraint("rebalance_id", "leg_id", name="uq_model_rebalance_leg_id"),
        sa.UniqueConstraint(
            "rebalance_id",
            "leg_id",
            "instr_id",
            "factor_snapshot_id",
            name="uq_model_rebalance_leg_prior_identity",
        ),
        sa.UniqueConstraint(
            "rebalance_id", "external_signal_id", name="uq_model_rebalance_leg_signal"
        ),
        sa.UniqueConstraint(
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
            name="uq_model_rebalance_leg_exact_account_identity",
        ),
        sa.UniqueConstraint(
            "rebalance_id",
            "sequence",
            "leg_id",
            "instr_id",
            "external_signal_id",
            "signal_snapshot_sha256",
            "rank_position",
            name="uq_model_rebalance_leg_exact_account_rank",
        ),
    )
    op.create_index("ix_model_rebalance_leg_instrument", "model_rebalance_legs", ["instr_id"])

    op.create_table(
        "account_rebalance_plans",
        sa.Column("account_plan_id", sa.String(length=64), nullable=False),
        sa.Column("model_rebalance_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("binding_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("data_use_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("decision_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execute_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_session_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_policy", sa.JSON(), nullable=False),
        sa.Column("broker_route", sa.JSON(), nullable=False),
        sa.Column("execution_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("broker_route_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_leg_count", sa.Integer(), nullable=False),
        sa.Column("execution_leg_count", sa.Integer(), nullable=False),
        sa.Column("intentional_cash_slots", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False, server_default="exit"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("fence_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "length(account_plan_id) = 64 AND length(model_rebalance_id) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(execution_policy_sha256) = 64 "
            "AND length(broker_route_sha256) = 64 AND length(content_sha256) = 64",
            name="ck_account_rebalance_plan_digests",
        ),
        sa.CheckConstraint(
            "data_use_scope = 'paper_forward'",
            name="ck_account_rebalance_plan_data_use_scope",
        ),
        sa.CheckConstraint(
            "decision_cutoff < execute_not_before AND execute_not_before < expires_at",
            name="ck_account_rebalance_plan_timing",
        ),
        sa.CheckConstraint(
            "execute_not_before < progress_deadline_at AND progress_deadline_at <= expires_at",
            name="ck_account_rebalance_plan_progress_timing",
        ),
        sa.CheckConstraint(
            "model_leg_count >= 0 AND execution_leg_count >= 0 "
            "AND execution_leg_count <= model_leg_count AND intentional_cash_slots >= 0",
            name="ck_account_rebalance_plan_counts",
        ),
        sa.CheckConstraint(
            "phase IN ('exit', 'account_refresh', 'entry', 'complete', 'failed', 'expired')",
            name="ck_account_rebalance_plan_phase",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'blocked', 'completed', 'degraded', "
            "'failed', 'expired', 'cancelled')",
            name="ck_account_rebalance_plan_status",
        ),
        sa.CheckConstraint("fence_generation >= 0", name="ck_account_rebalance_plan_fence"),
        sa.ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "strategy_id",
                "data_use_scope",
                "provider_authority_sha256",
                "decision_cutoff",
                "execute_not_before",
                "execution_session_sha256",
                "model_leg_count",
            ],
            [
                "model_rebalances.rebalance_id",
                "model_rebalances.strategy_id",
                "model_rebalances.data_use_scope",
                "model_rebalances.provider_authority_sha256",
                "model_rebalances.decision_cutoff",
                "model_rebalances.execute_not_before",
                "model_rebalances.execution_session_sha256",
                "model_rebalances.expected_leg_count",
            ],
            name="fk_account_rebalance_plan_exact_model_lineage",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["binding_id", "user_id", "broker_account_id", "strategy_id"],
            [
                "user_strategy_bindings.binding_id",
                "user_strategy_bindings.user_id",
                "user_strategy_bindings.broker_account_id",
                "user_strategy_bindings.strategy_id",
            ],
            name="fk_account_rebalance_plan_binding_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["broker_account_id", "user_id"],
            ["linked_broker_accounts.account_id", "linked_broker_accounts.user_id"],
            name="fk_account_rebalance_plan_account_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("account_plan_id"),
        sa.UniqueConstraint(
            "account_plan_id",
            "user_id",
            "broker_account_id",
            name="uq_account_rebalance_plan_owner",
        ),
        sa.UniqueConstraint(
            "account_plan_id",
            "user_id",
            "broker_account_id",
            "status",
            name="uq_account_rebalance_plan_owner_status",
        ),
        sa.UniqueConstraint(
            "model_rebalance_id",
            "binding_id",
            "user_id",
            "broker_account_id",
            name="uq_account_rebalance_plan_binding",
        ),
    )
    op.create_index(
        "ix_account_rebalance_plan_partition",
        "account_rebalance_plans",
        ["user_id", "broker_account_id", "status", "execute_not_before"],
    )
    op.create_index(
        "ix_account_rebalance_plan_progress",
        "account_rebalance_plans",
        ["status", "progress_deadline_at"],
    )

    op.create_table(
        "account_rebalance_plan_legs",
        sa.Column("account_plan_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("model_sequence", sa.Integer(), nullable=False),
        sa.Column("command_sequence", sa.Integer(), nullable=True),
        sa.Column("model_rebalance_id", sa.String(length=64), nullable=False),
        sa.Column("model_leg_id", sa.String(length=128), nullable=False),
        sa.Column("model_signal_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("plan_leg_id", sa.String(length=64), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("external_signal_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("rank_position", sa.Numeric(20, 10), nullable=True),
        sa.Column("allocation_hint", sa.Numeric(18, 12), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("depends_on_sequences", sa.JSON(), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "model_sequence >= 0 AND (command_sequence IS NULL OR command_sequence >= 0)",
            name="ck_account_rebalance_leg_sequence",
        ),
        sa.CheckConstraint(
            "length(plan_leg_id) = 64 AND length(trim(model_leg_id)) > 0 "
            "AND length(model_signal_snapshot_sha256) = 64 "
            "AND length(trim(external_signal_id)) > 0 AND length(trim(symbol)) > 0",
            name="ck_account_rebalance_leg_identity",
        ),
        sa.CheckConstraint(
            "phase IN ('exit', 'reduce', 'entry')",
            name="ck_account_rebalance_leg_phase",
        ),
        sa.CheckConstraint(
            "action IN ('exit', 'reduce', 'entry', 'reject')",
            name="ck_account_rebalance_leg_action",
        ),
        sa.CheckConstraint(
            "disposition IN ('selected', 'rejected')",
            name="ck_account_rebalance_leg_disposition",
        ),
        sa.CheckConstraint(
            "(disposition = 'selected' AND required "
            "AND command_sequence IS NOT NULL AND action = phase) OR "
            "(disposition = 'rejected' AND NOT required "
            "AND command_sequence IS NULL AND action = 'reject')",
            name="ck_account_rebalance_leg_selection",
        ),
        sa.CheckConstraint(
            "rank_position IS NULL OR rank_position > 0",
            name="ck_account_rebalance_leg_rank",
        ),
        sa.CheckConstraint(
            "allocation_hint >= 0 AND allocation_hint <= 1",
            name="ck_account_rebalance_leg_allocation",
        ),
        sa.CheckConstraint("length(trim(reason_code)) > 0", name="ck_account_rebalance_leg_reason"),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted', 'partial', 'confirmed', 'rejected', "
            "'failed', 'skipped')",
            name="ck_account_rebalance_leg_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_plan_id", "user_id", "broker_account_id"],
            [
                "account_rebalance_plans.account_plan_id",
                "account_rebalance_plans.user_id",
                "account_rebalance_plans.broker_account_id",
            ],
            name="fk_account_rebalance_leg_plan_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "model_sequence",
                "model_leg_id",
                "instr_id",
                "external_signal_id",
                "model_signal_snapshot_sha256",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.sequence",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.external_signal_id",
                "model_rebalance_legs.signal_snapshot_sha256",
            ],
            name="fk_account_rebalance_leg_exact_model_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "model_rebalance_id",
                "model_sequence",
                "model_leg_id",
                "instr_id",
                "external_signal_id",
                "model_signal_snapshot_sha256",
                "rank_position",
            ],
            [
                "model_rebalance_legs.rebalance_id",
                "model_rebalance_legs.sequence",
                "model_rebalance_legs.leg_id",
                "model_rebalance_legs.instr_id",
                "model_rebalance_legs.external_signal_id",
                "model_rebalance_legs.signal_snapshot_sha256",
                "model_rebalance_legs.rank_position",
            ],
            name="fk_account_rebalance_leg_exact_model_rank",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "account_plan_id", "user_id", "broker_account_id", "model_sequence"
        ),
        sa.UniqueConstraint("account_plan_id", "plan_leg_id", name="uq_account_rebalance_leg_id"),
        sa.UniqueConstraint(
            "account_plan_id",
            "command_sequence",
            name="uq_account_rebalance_command_sequence",
        ),
    )
    op.create_index(
        "ix_account_rebalance_leg_status",
        "account_rebalance_plan_legs",
        ["account_plan_id", "phase", "status"],
    )

    op.create_table(
        "account_rebalance_plan_resolutions",
        sa.Column("resolution_id", sa.String(length=64), nullable=False),
        sa.Column("account_plan_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=50), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_status", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("resolution_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("resolved_by", sa.String(length=100), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "length(resolution_id) = 64 AND length(account_plan_id) = 64",
            name="ck_account_rebalance_resolution_digests",
        ),
        sa.CheckConstraint(
            "plan_status = 'failed'",
            name="ck_account_rebalance_resolution_plan_status",
        ),
        sa.CheckConstraint(
            "resolution_type IN ('acknowledged', 'reconciled', 'remediated')",
            name="ck_account_rebalance_resolution_type",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0 AND length(trim(resolved_by)) > 0",
            name="ck_account_rebalance_resolution_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["account_plan_id", "user_id", "broker_account_id", "plan_status"],
            [
                "account_rebalance_plans.account_plan_id",
                "account_rebalance_plans.user_id",
                "account_rebalance_plans.broker_account_id",
                "account_rebalance_plans.status",
            ],
            name="fk_account_rebalance_resolution_exact_failed_plan",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resolution_id"),
    )
    op.create_index(
        "ix_account_rebalance_resolution_plan",
        "account_rebalance_plan_resolutions",
        ["account_plan_id", "resolved_at", "resolution_id"],
    )
    op.create_index(
        "ix_account_rebalance_resolution_owner",
        "account_rebalance_plan_resolutions",
        ["user_id", "broker_account_id", "resolved_at"],
    )

    if _is_postgresql():
        _enable_service_access()
        _create_protection_triggers()
        _create_resolution_protection_trigger()


def downgrade() -> None:
    retained = {
        table: int(op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in (*_MODEL_TABLES, *_ACCOUNT_TABLES, *_RESOLUTION_TABLES)
    }
    if any(retained.values()):
        message = f"Cannot remove immutable portfolio rebalance history: {retained}"
        raise RuntimeError(message)
    if _is_postgresql():
        op.execute(
            "DROP TRIGGER account_rebalance_resolution_immutable "
            "ON account_rebalance_plan_resolutions"
        )
        op.execute("DROP FUNCTION public.vm_reject_account_rebalance_resolution_mutation()")
        op.execute(
            "DROP TRIGGER IF EXISTS account_rebalance_plan_terminal_coherent "
            "ON account_rebalance_plans"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS public.vm_assert_account_rebalance_terminal_coherent()"
        )
        op.execute("DROP TRIGGER account_rebalance_plan_complete ON account_rebalance_plans")
        op.execute("DROP FUNCTION public.vm_assert_account_rebalance_plan_complete()")
        op.execute("DROP TRIGGER model_rebalance_complete ON model_rebalances")
        op.execute("DROP FUNCTION public.vm_assert_model_rebalance_complete()")
        for table in reversed(tuple(_SEALED_AGGREGATES)):
            op.execute(f"DROP TRIGGER {table}_aggregate_sealed ON {table}")
        op.execute("DROP TRIGGER account_rebalance_leg_frozen ON account_rebalance_plan_legs")
        op.execute("DROP FUNCTION public.vm_protect_account_rebalance_leg()")
        op.execute("DROP TRIGGER account_rebalance_plan_frozen ON account_rebalance_plans")
        op.execute("DROP FUNCTION public.vm_protect_account_rebalance_plan()")
        for table in _MODEL_TABLES:
            op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute("DROP FUNCTION public.vm_reject_model_rebalance_mutation()")

    op.drop_index(
        "ix_account_rebalance_resolution_owner",
        table_name="account_rebalance_plan_resolutions",
    )
    op.drop_index(
        "ix_account_rebalance_resolution_plan",
        table_name="account_rebalance_plan_resolutions",
    )
    op.drop_table("account_rebalance_plan_resolutions")
    op.drop_index("ix_account_rebalance_leg_status", table_name="account_rebalance_plan_legs")
    op.drop_table("account_rebalance_plan_legs")
    op.drop_index("ix_account_rebalance_plan_progress", table_name="account_rebalance_plans")
    op.drop_index("ix_account_rebalance_plan_partition", table_name="account_rebalance_plans")
    op.drop_table("account_rebalance_plans")
    op.drop_index("ix_model_rebalance_leg_instrument", table_name="model_rebalance_legs")
    op.drop_table("model_rebalance_legs")
    op.drop_index("ix_model_rebalance_strategy_session", table_name="model_rebalances")
    op.drop_table("model_rebalances")

    if _is_postgresql():
        # ``IF EXISTS`` also permits recycling an empty database that applied
        # an earlier uncommitted draft of this revision before the exact prior-
        # leg lineage constraint was added.
        op.execute(
            "ALTER TABLE equity_rank_snapshots DROP CONSTRAINT IF EXISTS "
            "uq_equity_rank_exact_model_lineage"
        )
    else:
        with op.batch_alter_table("equity_rank_snapshots") as batch_op:
            batch_op.drop_constraint(
                "uq_equity_rank_exact_model_lineage",
                type_="unique",
            )

    with op.batch_alter_table("user_strategy_bindings") as batch_op:
        batch_op.drop_constraint(
            "uq_user_strategy_binding_owner",
            type_="unique",
        )
