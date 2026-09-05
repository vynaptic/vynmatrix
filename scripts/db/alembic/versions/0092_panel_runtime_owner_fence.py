"""Fence synchronized panel runtime by owner, scope, environment, and activation.

Revision ID: 0092_panel_owner_fence
Revises: 0091_dated_security_symbols
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092_panel_owner_fence"
down_revision = "0091_dated_security_symbols"
branch_labels = None
depends_on = None

_INPUT_TABLE = "strategy_panel_input_revisions"
_DECISION_TABLE = "strategy_panel_decisions"
_RUNTIME_TABLE = "strategy_runtime_states"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _install_postgresql_guard(*, legacy: bool) -> None:
    binding_fields = (
        ""
        if legacy
        else """
                NEW.entitlement_owner_user_id,
                NEW.runtime_environment,
                NEW.activation_cutoff,
"""
    )
    old_binding_fields = (
        ""
        if legacy
        else """
                OLD.entitlement_owner_user_id,
                OLD.runtime_environment,
                OLD.activation_cutoff,
"""
    )
    terminal_statuses = (
        "'completed', 'rejected_correction'"
        if legacy
        else "'completed', 'rejected_correction', 'skipped', 'expired'"
    )
    prepared_transitions = (
        "'prepared', 'completed'" if legacy else "'prepared', 'completed', 'expired'"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_protect_strategy_panel_decision ON strategy_panel_decisions"
    )
    op.execute("DROP FUNCTION IF EXISTS public.vm_protect_strategy_panel_decision()")
    op.execute(
        f"""
        CREATE FUNCTION public.vm_protect_strategy_panel_decision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF ROW(
                NEW.decision_key,
                NEW.worker_id,
                NEW.strategy_id,
                NEW.strategy_version,
                NEW.cutoff_at,
                NEW.official_session_date,
                NEW.official_session_sha256,
                NEW.execute_not_before,
                NEW.execution_session_sha256,
                NEW.data_use_scope,
                {binding_fields}
                NEW.provider_authority_sha256,
                NEW.membership_sha256,
                NEW.factor_snapshot_sha256,
                NEW.panel_sha256,
                NEW.strategy_input_sha256,
                NEW.member_count,
                NEW.observation_count,
                NEW.exclusion_count,
                NEW.maximum_content_revision,
                NEW.correction_of_decision_key,
                NEW.state_schema_version,
                NEW.prepared_state_generation,
                NEW.prepared_state_sha256,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.decision_key,
                OLD.worker_id,
                OLD.strategy_id,
                OLD.strategy_version,
                OLD.cutoff_at,
                OLD.official_session_date,
                OLD.official_session_sha256,
                OLD.execute_not_before,
                OLD.execution_session_sha256,
                OLD.data_use_scope,
                {old_binding_fields}
                OLD.provider_authority_sha256,
                OLD.membership_sha256,
                OLD.factor_snapshot_sha256,
                OLD.panel_sha256,
                OLD.strategy_input_sha256,
                OLD.member_count,
                OLD.observation_count,
                OLD.exclusion_count,
                OLD.maximum_content_revision,
                OLD.correction_of_decision_key,
                OLD.state_schema_version,
                OLD.prepared_state_generation,
                OLD.prepared_state_sha256,
                OLD.created_at
            )
            OR NEW.provider_authority_policy::jsonb
                IS DISTINCT FROM OLD.provider_authority_policy::jsonb
            OR NEW.panel_payload::jsonb IS DISTINCT FROM OLD.panel_payload::jsonb
            OR NEW.strategy_input_payload::jsonb
                IS DISTINCT FROM OLD.strategy_input_payload::jsonb THEN
                RAISE EXCEPTION 'strategy panel input is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF OLD.status IN ({terminal_statuses}) AND (
                ROW(
                    NEW.status,
                    NEW.rejection_reason,
                    NEW.state_generation,
                    NEW.state_sha256,
                    NEW.rank_snapshot_id,
                    NEW.evaluation_audit_sha256,
                    NEW.completed_at
                ) IS DISTINCT FROM ROW(
                    OLD.status,
                    OLD.rejection_reason,
                    OLD.state_generation,
                    OLD.state_sha256,
                    OLD.rank_snapshot_id,
                    OLD.evaluation_audit_sha256,
                    OLD.completed_at
                )
                OR NEW.state_payload::jsonb IS DISTINCT FROM OLD.state_payload::jsonb
                OR NEW.evaluation_audit_payload::jsonb
                    IS DISTINCT FROM OLD.evaluation_audit_payload::jsonb
                OR NEW.signal_envelopes::jsonb IS DISTINCT FROM OLD.signal_envelopes::jsonb
            ) THEN
                RAISE EXCEPTION 'terminal strategy panel decision is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF OLD.status = 'prepared' AND NEW.status NOT IN ({prepared_transitions}) THEN
                RAISE EXCEPTION 'invalid strategy panel status transition'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status = 'prepared' AND NEW.status = 'prepared' AND (
                ROW(
                    NEW.rejection_reason,
                    NEW.state_generation,
                    NEW.state_sha256,
                    NEW.rank_snapshot_id,
                    NEW.evaluation_audit_sha256,
                    NEW.completed_at
                ) IS DISTINCT FROM ROW(
                    OLD.rejection_reason,
                    OLD.state_generation,
                    OLD.state_sha256,
                    OLD.rank_snapshot_id,
                    OLD.evaluation_audit_sha256,
                    OLD.completed_at
                )
                OR NEW.state_payload::jsonb IS DISTINCT FROM OLD.state_payload::jsonb
                OR NEW.evaluation_audit_payload::jsonb
                    IS DISTINCT FROM OLD.evaluation_audit_payload::jsonb
                OR NEW.signal_envelopes::jsonb IS DISTINCT FROM OLD.signal_envelopes::jsonb
            ) THEN
                RAISE EXCEPTION 'prepared strategy panel output is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_protect_strategy_panel_decision "
        "BEFORE UPDATE ON strategy_panel_decisions FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_protect_strategy_panel_decision()"
    )


def upgrade() -> None:
    existing_decisions = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM strategy_panel_decisions")).scalar_one()
    )
    if existing_decisions:
        message = "Cannot bind legacy runtime decisions without an exact paper owner"
        raise RuntimeError(message)
    with op.batch_alter_table(_DECISION_TABLE) as batch_op:
        batch_op.drop_constraint("fk_strategy_panel_registered_input", type_="foreignkey")
    op.drop_index("ix_strategy_panel_input_poll", table_name=_INPUT_TABLE)
    with op.batch_alter_table(_INPUT_TABLE) as batch_op:
        batch_op.drop_constraint("uq_strategy_panel_input_identity", type_="unique")
        batch_op.add_column(sa.Column("entitlement_owner_user_id", sa.String(50)))
        batch_op.create_foreign_key(
            "fk_strategy_panel_input_owner",
            "users",
            ["entitlement_owner_user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_strategy_panel_input_owner_nonempty",
            "entitlement_owner_user_id IS NULL OR length(trim(entitlement_owner_user_id)) > 0",
        )
        batch_op.create_unique_constraint(
            "uq_strategy_panel_input_identity",
            [
                "input_sha256",
                "strategy_id",
                "strategy_version",
                "cutoff_at",
                "entitlement_owner_user_id",
            ],
        )
    op.create_index(
        "ix_strategy_panel_input_poll",
        _INPUT_TABLE,
        [
            "strategy_id",
            "strategy_version",
            "data_use_scope",
            "entitlement_owner_user_id",
            "cutoff_at",
        ],
    )

    with op.batch_alter_table(_RUNTIME_TABLE) as batch_op:
        batch_op.add_column(sa.Column("panel_runtime_environment", sa.String(32)))
        batch_op.add_column(sa.Column("panel_data_use_scope", sa.String(32)))
        batch_op.add_column(sa.Column("panel_entitlement_owner_user_id", sa.String(50)))
        batch_op.add_column(sa.Column("panel_activation_cutoff", sa.DateTime(timezone=True)))
        batch_op.create_foreign_key(
            "fk_strategy_runtime_panel_owner",
            "users",
            ["panel_entitlement_owner_user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_strategy_runtime_panel_binding",
            "(panel_runtime_environment IS NULL "
            "AND panel_data_use_scope IS NULL "
            "AND panel_entitlement_owner_user_id IS NULL "
            "AND panel_activation_cutoff IS NULL) OR "
            "(length(trim(panel_runtime_environment)) > 0 "
            "AND panel_data_use_scope = 'paper_forward' "
            "AND length(trim(panel_entitlement_owner_user_id)) > 0 "
            "AND panel_activation_cutoff IS NOT NULL)",
        )

    op.drop_index("uq_strategy_panel_authoritative_cutoff", table_name=_DECISION_TABLE)
    op.drop_index("ix_strategy_panel_pending", table_name=_DECISION_TABLE)
    with op.batch_alter_table(_DECISION_TABLE) as batch_op:
        batch_op.drop_constraint("uq_strategy_panel_worker_cutoff_input", type_="unique")
        batch_op.drop_constraint("ck_strategy_panel_status", type_="check")
        batch_op.drop_constraint("ck_strategy_panel_completion", type_="check")
        batch_op.add_column(sa.Column("entitlement_owner_user_id", sa.String(50), nullable=False))
        batch_op.add_column(sa.Column("runtime_environment", sa.String(32), nullable=False))
        batch_op.add_column(
            sa.Column("activation_cutoff", sa.DateTime(timezone=True), nullable=False)
        )
        batch_op.create_foreign_key(
            "fk_strategy_panel_owner",
            "users",
            ["entitlement_owner_user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_strategy_panel_status",
            "status IN ('prepared', 'completed', 'rejected_correction', 'skipped', 'expired')",
        )
        batch_op.create_check_constraint(
            "ck_strategy_panel_runtime_binding",
            "data_use_scope = 'paper_forward' "
            "AND length(trim(entitlement_owner_user_id)) > 0 "
            "AND length(trim(runtime_environment)) > 0 "
            "AND activation_cutoff IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_strategy_panel_completion",
            "(status = 'prepared' AND completed_at IS NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL AND evaluation_audit_payload IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NOT NULL AND state_generation > 0 "
            "AND state_sha256 IS NOT NULL AND state_payload IS NOT NULL "
            "AND rank_snapshot_id IS NOT NULL AND evaluation_audit_sha256 IS NOT NULL "
            "AND evaluation_audit_payload IS NOT NULL) OR "
            "(status IN ('skipped', 'expired') AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL AND evaluation_audit_payload IS NULL) OR "
            "(status = 'rejected_correction' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL AND evaluation_audit_payload IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_strategy_panel_registered_input",
            _INPUT_TABLE,
            [
                "strategy_input_sha256",
                "strategy_id",
                "strategy_version",
                "cutoff_at",
                "entitlement_owner_user_id",
            ],
            [
                "input_sha256",
                "strategy_id",
                "strategy_version",
                "cutoff_at",
                "entitlement_owner_user_id",
            ],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_strategy_panel_worker_cutoff_input",
            [
                "worker_id",
                "entitlement_owner_user_id",
                "runtime_environment",
                "cutoff_at",
                "strategy_input_sha256",
            ],
        )
    op.create_index(
        "uq_strategy_panel_authoritative_cutoff",
        _DECISION_TABLE,
        ["worker_id", "entitlement_owner_user_id", "runtime_environment", "cutoff_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared', 'completed')"),
        sqlite_where=sa.text("status IN ('prepared', 'completed')"),
    )
    op.create_index(
        "ix_strategy_panel_pending",
        _DECISION_TABLE,
        ["worker_id", "entitlement_owner_user_id", "runtime_environment", "status", "cutoff_at"],
    )
    if _is_postgresql():
        _install_postgresql_guard(legacy=False)


def downgrade() -> None:
    bound = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM strategy_panel_decisions "
                "WHERE entitlement_owner_user_id IS NOT NULL "
                "OR status IN ('skipped', 'expired')"
            )
        )
        .scalar_one()
    )
    bound_inputs = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM strategy_panel_input_revisions "
                "WHERE entitlement_owner_user_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    bound_runtime = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM strategy_runtime_states "
                "WHERE panel_entitlement_owner_user_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if bound or bound_inputs or bound_runtime:
        message = "Cannot remove the panel owner fence while bound runtime history exists"
        raise RuntimeError(message)
    if _is_postgresql():
        op.execute(
            "DROP TRIGGER IF EXISTS trg_protect_strategy_panel_decision ON strategy_panel_decisions"
        )
        op.execute("DROP FUNCTION IF EXISTS public.vm_protect_strategy_panel_decision()")

    op.drop_index("uq_strategy_panel_authoritative_cutoff", table_name=_DECISION_TABLE)
    op.drop_index("ix_strategy_panel_pending", table_name=_DECISION_TABLE)
    with op.batch_alter_table(_DECISION_TABLE) as batch_op:
        batch_op.drop_constraint("fk_strategy_panel_registered_input", type_="foreignkey")
        batch_op.drop_constraint("fk_strategy_panel_owner", type_="foreignkey")
        batch_op.drop_constraint("uq_strategy_panel_worker_cutoff_input", type_="unique")
        batch_op.drop_constraint("ck_strategy_panel_status", type_="check")
        batch_op.drop_constraint("ck_strategy_panel_runtime_binding", type_="check")
        batch_op.drop_constraint("ck_strategy_panel_completion", type_="check")
        batch_op.drop_column("activation_cutoff")
        batch_op.drop_column("runtime_environment")
        batch_op.drop_column("entitlement_owner_user_id")
        batch_op.create_check_constraint(
            "ck_strategy_panel_status",
            "status IN ('prepared', 'completed', 'rejected_correction')",
        )
        batch_op.create_check_constraint(
            "ck_strategy_panel_completion",
            "(status = 'prepared' AND completed_at IS NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL AND evaluation_audit_payload IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NOT NULL AND state_generation > 0 "
            "AND state_sha256 IS NOT NULL AND state_payload IS NOT NULL "
            "AND rank_snapshot_id IS NOT NULL AND evaluation_audit_sha256 IS NOT NULL "
            "AND evaluation_audit_payload IS NOT NULL) OR "
            "(status = 'rejected_correction' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NOT NULL AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL AND evaluation_audit_payload IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_strategy_panel_registered_input",
            _INPUT_TABLE,
            ["strategy_input_sha256", "strategy_id", "strategy_version", "cutoff_at"],
            ["input_sha256", "strategy_id", "strategy_version", "cutoff_at"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_strategy_panel_worker_cutoff_input",
            ["worker_id", "cutoff_at", "strategy_input_sha256"],
        )
    op.create_index(
        "uq_strategy_panel_authoritative_cutoff",
        _DECISION_TABLE,
        ["worker_id", "cutoff_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared', 'completed')"),
        sqlite_where=sa.text("status IN ('prepared', 'completed')"),
    )
    op.create_index(
        "ix_strategy_panel_pending",
        _DECISION_TABLE,
        ["worker_id", "status", "cutoff_at"],
    )

    with op.batch_alter_table(_RUNTIME_TABLE) as batch_op:
        batch_op.drop_constraint("fk_strategy_runtime_panel_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_strategy_runtime_panel_binding", type_="check")
        batch_op.drop_column("panel_activation_cutoff")
        batch_op.drop_column("panel_entitlement_owner_user_id")
        batch_op.drop_column("panel_data_use_scope")
        batch_op.drop_column("panel_runtime_environment")

    op.drop_index("ix_strategy_panel_input_poll", table_name=_INPUT_TABLE)
    with op.batch_alter_table(_INPUT_TABLE) as batch_op:
        batch_op.drop_constraint("fk_strategy_panel_input_owner", type_="foreignkey")
        batch_op.drop_constraint("ck_strategy_panel_input_owner_nonempty", type_="check")
        batch_op.drop_constraint("uq_strategy_panel_input_identity", type_="unique")
        batch_op.drop_column("entitlement_owner_user_id")
        batch_op.create_unique_constraint(
            "uq_strategy_panel_input_identity",
            ["input_sha256", "strategy_id", "strategy_version", "cutoff_at"],
        )
    op.create_index(
        "ix_strategy_panel_input_poll",
        _INPUT_TABLE,
        ["strategy_id", "strategy_version", "cutoff_at"],
    )
    if _is_postgresql():
        _install_postgresql_guard(legacy=True)
