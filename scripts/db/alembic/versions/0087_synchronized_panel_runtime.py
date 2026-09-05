"""Add the durable synchronized-panel strategy runtime.

Revision ID: 0087_synchronized_panel_runtime
Revises: 0086_equity_factor_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087_synchronized_panel_runtime"
down_revision = "0086_equity_factor_evidence"
branch_labels = None
depends_on = None

_TABLE = "strategy_panel_decisions"
_INPUT_TABLE = "strategy_panel_input_revisions"
_SECURITY_IDENTITY_TABLE = "equity_security_identities"
_INDICATOR_ROLE = "vm_indicator"
_SCORING_ROLE = "vm_scoring"
_INDICATOR_OUTBOX_TOPICS = ("signals.submit", "signals.rebalances.submit")


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _enable_postgresql_runtime_access() -> None:
    op.execute(f"GRANT SELECT ON TABLE public.{_SECURITY_IDENTITY_TABLE} TO {_INDICATOR_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{_SECURITY_IDENTITY_TABLE} TO vm_market_data")
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE "
        f"public.{_SECURITY_IDENTITY_TABLE}_identity_id_seq TO vm_market_data"
    )
    op.execute(
        f"CREATE TRIGGER trg_{_SECURITY_IDENTITY_TABLE}_immutable "
        f"BEFORE UPDATE OR DELETE ON public.{_SECURITY_IDENTITY_TABLE} FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_reject_immutable_equity_evidence_mutation()"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{_INPUT_TABLE} TO {_INDICATOR_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE public.{_INPUT_TABLE} TO vm_market_data")
    op.execute(
        f"CREATE TRIGGER trg_{_INPUT_TABLE}_immutable "
        f"BEFORE UPDATE OR DELETE ON public.{_INPUT_TABLE} FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_reject_immutable_equity_evidence_mutation()"
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{_TABLE} TO {_INDICATOR_ROLE}")
    op.execute(
        f"CREATE POLICY {_TABLE}_indicator_select ON {_TABLE} "
        f"FOR SELECT TO {_INDICATOR_ROLE} USING (true)"
    )
    op.execute(
        f"CREATE POLICY {_TABLE}_indicator_insert ON {_TABLE} "
        f"FOR INSERT TO {_INDICATOR_ROLE} WITH CHECK (true)"
    )
    op.execute(
        f"CREATE POLICY {_TABLE}_indicator_update ON {_TABLE} "
        f"FOR UPDATE TO {_INDICATOR_ROLE} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT ON TABLE public.{_TABLE} TO {_SCORING_ROLE}")
    op.execute(
        f"CREATE POLICY {_TABLE}_scoring_select ON {_TABLE} "
        f"FOR SELECT TO {_SCORING_ROLE} USING (status = 'completed')"
    )
    _replace_indicator_outbox_policies(topics=_INDICATOR_OUTBOX_TOPICS)
    op.execute(
        """
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

            IF OLD.status IN ('completed', 'rejected_correction')
               AND ROW(
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
               ) THEN
                RAISE EXCEPTION 'terminal strategy panel decision is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('completed', 'rejected_correction')
               AND (
                    NEW.state_payload::jsonb IS DISTINCT FROM OLD.state_payload::jsonb
                    OR NEW.evaluation_audit_payload::jsonb
                        IS DISTINCT FROM OLD.evaluation_audit_payload::jsonb
                    OR NEW.signal_envelopes::jsonb
                        IS DISTINCT FROM OLD.signal_envelopes::jsonb
               ) THEN
                RAISE EXCEPTION 'terminal strategy panel decision is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF OLD.status = 'prepared' AND NEW.status NOT IN ('prepared', 'completed') THEN
                RAISE EXCEPTION 'invalid strategy panel status transition'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status = 'prepared'
               AND NEW.status = 'prepared'
               AND (
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
                    OR NEW.state_payload::jsonb
                        IS DISTINCT FROM OLD.state_payload::jsonb
                    OR NEW.evaluation_audit_payload::jsonb
                        IS DISTINCT FROM OLD.evaluation_audit_payload::jsonb
                    OR NEW.signal_envelopes::jsonb
                        IS DISTINCT FROM OLD.signal_envelopes::jsonb
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
        f"""
        CREATE TRIGGER trg_protect_strategy_panel_decision
        BEFORE UPDATE ON {_TABLE}
        FOR EACH ROW
        EXECUTE FUNCTION public.vm_protect_strategy_panel_decision()
        """
    )


def _replace_indicator_outbox_policies(*, topics: tuple[str, ...]) -> None:
    quoted_topics = ", ".join(f"'{topic}'" for topic in topics)
    predicate = f"topic IN ({quoted_topics})"
    for command in ("SELECT", "INSERT", "UPDATE"):
        policy = f"outbox_events_indicator_{command.lower()}"
        op.execute(f"DROP POLICY IF EXISTS {policy} ON outbox_events")
        if command == "SELECT":
            clause = f"FOR SELECT TO {_INDICATOR_ROLE} USING ({predicate})"
        elif command == "INSERT":
            clause = f"FOR INSERT TO {_INDICATOR_ROLE} WITH CHECK ({predicate})"
        else:
            clause = f"FOR UPDATE TO {_INDICATOR_ROLE} USING ({predicate}) WITH CHECK ({predicate})"
        op.execute(f"CREATE POLICY {policy} ON outbox_events {clause}")


def upgrade() -> None:
    with op.batch_alter_table("index_membership") as batch_op:
        batch_op.drop_constraint("ck_index_membership_span", type_="check")
        batch_op.create_check_constraint(
            "ck_index_membership_span",
            "effective_to IS NULL OR effective_to >= effective_from",
        )
        batch_op.add_column(sa.Column("observation_id", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_index_membership_observation",
            "equity_observations",
            ["observation_id"],
            ["observation_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_index_membership_observation_id",
            ["observation_id"],
        )
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
            "'market_cap', 'security_identity')",
        )
    with op.batch_alter_table("market_calendars") as batch_op:
        batch_op.add_column(sa.Column("observation_id", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_market_calendar_observation",
            "equity_observations",
            ["observation_id"],
            ["observation_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_market_calendars_observation_id",
            ["observation_id"],
        )
    op.create_table(
        _SECURITY_IDENTITY_TABLE,
        sa.Column("identity_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("security_id", sa.String(length=100), nullable=False),
        sa.Column("issuer_id", sa.String(length=100), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("source_ref", sa.String(length=200), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_equity_security_identity_span",
        ),
        sa.CheckConstraint(
            "length(trim(security_id)) > 0 AND length(trim(issuer_id)) > 0 "
            "AND length(trim(source_ref)) > 0 AND length(observation_id) = 64",
            name="ck_equity_security_identity_values",
        ),
        sa.ForeignKeyConstraint(
            ["instr_id"],
            ["instruments.instr_id"],
            name="fk_equity_security_identity_instrument",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["equity_observations.observation_id"],
            name="fk_equity_security_identity_observation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identity_id"),
        sa.UniqueConstraint(
            "security_id",
            "effective_from",
            name="uq_equity_security_identity_security_from",
        ),
        sa.UniqueConstraint(
            "instr_id",
            "effective_from",
            name="uq_equity_security_identity_instrument_from",
        ),
    )
    op.create_index(
        "ix_equity_security_identities_instr_id",
        _SECURITY_IDENTITY_TABLE,
        ["instr_id"],
    )
    op.create_index(
        "ix_equity_security_identity_effective",
        _SECURITY_IDENTITY_TABLE,
        ["instr_id", "effective_from", "effective_to"],
    )
    op.create_index(
        "ix_equity_security_identity_observation",
        _SECURITY_IDENTITY_TABLE,
        ["observation_id"],
    )
    op.create_table(
        _INPUT_TABLE,
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strategy_version", sa.String(length=20), nullable=False),
        sa.Column("universe_code", sa.String(length=20), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("official_session_date", sa.Date(), nullable=False),
        sa.Column("execute_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_use_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("membership_sha256", sa.String(length=64), nullable=False),
        sa.Column("factor_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("panel_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_validator_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_validator_version", sa.String(length=100), nullable=False),
        sa.Column("strategy_input_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_input_authority_payload", sa.JSON(), nullable=False),
        sa.Column("panel_payload", sa.JSON(), nullable=False),
        sa.Column("strategy_input_payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(input_sha256) = 64 AND length(provider_authority_sha256) = 64 "
            "AND length(membership_sha256) = 64 "
            "AND length(factor_snapshot_sha256) = 64 AND length(panel_sha256) = 64 "
            "AND length(strategy_input_authority_sha256) = 64",
            name="ck_strategy_panel_input_digests",
        ),
        sa.CheckConstraint(
            "execute_not_before > cutoff_at",
            name="ck_strategy_panel_input_execution_timing",
        ),
        sa.CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_strategy_panel_input_data_use_scope",
        ),
        sa.CheckConstraint(
            "length(trim(strategy_version)) > 0 AND length(trim(universe_code)) > 0",
            name="ck_strategy_panel_input_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(strategy_validator_id)) > 0 "
            "AND length(trim(strategy_validator_version)) > 0",
            name="ck_strategy_panel_input_validator_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_panel_input_strategy",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("input_sha256"),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "cutoff_at",
            "input_sha256",
            name="uq_strategy_panel_input_revision",
        ),
        sa.UniqueConstraint(
            "input_sha256",
            "strategy_id",
            "strategy_version",
            "cutoff_at",
            name="uq_strategy_panel_input_identity",
        ),
    )
    op.create_index(
        "ix_strategy_panel_input_poll",
        _INPUT_TABLE,
        ["strategy_id", "strategy_version", "cutoff_at"],
    )
    op.create_table(
        _TABLE,
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strategy_version", sa.String(length=20), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("official_session_date", sa.Date(), nullable=False),
        sa.Column("official_session_sha256", sa.String(length=64), nullable=False),
        sa.Column("execute_not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_session_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_use_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_authority_policy", sa.JSON(), nullable=False),
        sa.Column("provider_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("membership_sha256", sa.String(length=64), nullable=False),
        sa.Column("factor_snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("panel_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_input_sha256", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("exclusion_count", sa.Integer(), nullable=False),
        sa.Column("maximum_content_revision", sa.Integer()),
        sa.Column("panel_payload", sa.JSON(), nullable=False),
        sa.Column("strategy_input_payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'prepared'"),
        ),
        sa.Column("correction_of_decision_key", sa.String(length=64)),
        sa.Column("rejection_reason", sa.String(length=300)),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("prepared_state_generation", sa.BigInteger(), nullable=False),
        sa.Column("prepared_state_sha256", sa.String(length=64), nullable=False),
        sa.Column("state_generation", sa.BigInteger()),
        sa.Column("state_sha256", sa.String(length=64)),
        sa.Column("state_payload", sa.JSON()),
        sa.Column("rank_snapshot_id", sa.String(length=64)),
        sa.Column("evaluation_audit_sha256", sa.String(length=64)),
        sa.Column("evaluation_audit_payload", sa.JSON()),
        sa.Column("signal_envelopes", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "length(decision_key) = 64 "
            "AND length(official_session_sha256) = 64 "
            "AND length(execution_session_sha256) = 64 "
            "AND length(provider_authority_sha256) = 64 "
            "AND length(membership_sha256) = 64 "
            "AND length(factor_snapshot_sha256) = 64 "
            "AND length(panel_sha256) = 64 "
            "AND length(strategy_input_sha256) = 64 "
            "AND length(prepared_state_sha256) = 64 "
            "AND (state_sha256 IS NULL OR length(state_sha256) = 64) "
            "AND (rank_snapshot_id IS NULL OR length(rank_snapshot_id) = 64) "
            "AND (evaluation_audit_sha256 IS NULL "
            "OR length(evaluation_audit_sha256) = 64)",
            name="ck_strategy_panel_digests",
        ),
        sa.CheckConstraint(
            "execute_not_before > cutoff_at",
            name="ck_strategy_panel_execution_timing",
        ),
        sa.CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_strategy_panel_data_use_scope",
        ),
        sa.CheckConstraint(
            "length(trim(strategy_version)) > 0",
            name="ck_strategy_panel_version_nonempty",
        ),
        sa.CheckConstraint(
            "member_count > 0 AND observation_count >= 0 AND exclusion_count >= 0 "
            "AND observation_count + exclusion_count = member_count",
            name="ck_strategy_panel_member_counts",
        ),
        sa.CheckConstraint(
            "maximum_content_revision IS NULL OR maximum_content_revision > 0",
            name="ck_strategy_panel_content_revision_positive",
        ),
        sa.CheckConstraint(
            "state_schema_version > 0 AND prepared_state_generation > 0",
            name="ck_strategy_panel_prepared_state",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'completed', 'rejected_correction')",
            name="ck_strategy_panel_status",
        ),
        sa.CheckConstraint(
            "(status = 'prepared' AND completed_at IS NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL "
            "AND evaluation_audit_payload IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NULL AND rejection_reason IS NULL "
            "AND state_generation IS NOT NULL AND state_generation > 0 "
            "AND state_sha256 IS NOT NULL AND state_payload IS NOT NULL "
            "AND rank_snapshot_id IS NOT NULL "
            "AND evaluation_audit_sha256 IS NOT NULL "
            "AND evaluation_audit_payload IS NOT NULL) OR "
            "(status = 'rejected_correction' AND completed_at IS NOT NULL "
            "AND correction_of_decision_key IS NOT NULL "
            "AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0 "
            "AND state_generation IS NULL AND state_sha256 IS NULL "
            "AND state_payload IS NULL AND rank_snapshot_id IS NULL "
            "AND evaluation_audit_sha256 IS NULL "
            "AND evaluation_audit_payload IS NULL)",
            name="ck_strategy_panel_completion",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["strategy_runtime_states.worker_id"],
            name="fk_strategy_panel_runtime",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.strategy_id"],
            name="fk_strategy_panel_strategy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_input_sha256", "strategy_id", "strategy_version", "cutoff_at"],
            [
                "strategy_panel_input_revisions.input_sha256",
                "strategy_panel_input_revisions.strategy_id",
                "strategy_panel_input_revisions.strategy_version",
                "strategy_panel_input_revisions.cutoff_at",
            ],
            name="fk_strategy_panel_registered_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rank_snapshot_id"],
            ["equity_rank_snapshots.rank_snapshot_id"],
            name="fk_strategy_panel_rank_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["correction_of_decision_key"],
            ["strategy_panel_decisions.decision_key"],
            name="fk_strategy_panel_correction",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_key"),
        sa.UniqueConstraint(
            "worker_id",
            "cutoff_at",
            "strategy_input_sha256",
            name="uq_strategy_panel_worker_cutoff_input",
        ),
    )
    op.create_index("ix_strategy_panel_worker_id", _TABLE, ["worker_id"])
    op.create_index(
        "uq_strategy_panel_authoritative_cutoff",
        _TABLE,
        ["worker_id", "cutoff_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared', 'completed')"),
        sqlite_where=sa.text("status IN ('prepared', 'completed')"),
    )
    op.create_index(
        "ix_strategy_panel_pending",
        _TABLE,
        ["worker_id", "status", "cutoff_at"],
    )
    if _is_postgresql():
        _enable_postgresql_runtime_access()


def downgrade() -> None:
    retained = int(op.get_bind().execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one())
    retained_inputs = int(
        op.get_bind().execute(sa.text(f"SELECT count(*) FROM {_INPUT_TABLE}")).scalar_one()
    )
    retained_security_identities = int(
        op.get_bind()
        .execute(sa.text(f"SELECT count(*) FROM {_SECURITY_IDENTITY_TABLE}"))
        .scalar_one()
    )
    if retained or retained_inputs or retained_security_identities:
        message = (
            "Cannot remove durable synchronized-panel history while "
            f"{retained} decision row(s), {retained_inputs} input row(s), and "
            f"{retained_security_identities} security identity row(s) exist"
        )
        raise RuntimeError(message)
    if _is_postgresql():
        _replace_indicator_outbox_policies(topics=("signals.submit",))
        op.execute(f"DROP TRIGGER IF EXISTS trg_protect_strategy_panel_decision ON {_TABLE}")
        op.execute("DROP FUNCTION IF EXISTS public.vm_protect_strategy_panel_decision()")
        for command in ("select", "insert", "update"):
            op.execute(f"DROP POLICY IF EXISTS {_TABLE}_indicator_{command} ON {_TABLE}")
        op.execute(f"DROP POLICY IF EXISTS {_TABLE}_scoring_select ON {_TABLE}")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{_TABLE} FROM {_INDICATOR_ROLE}")
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{_TABLE} FROM {_SCORING_ROLE}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_INPUT_TABLE}_immutable ON {_INPUT_TABLE}")
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{_SECURITY_IDENTITY_TABLE}_immutable "
            f"ON {_SECURITY_IDENTITY_TABLE}"
        )
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{_INPUT_TABLE} "
            f"FROM {_INDICATOR_ROLE}, vm_market_data"
        )
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE public.{_SECURITY_IDENTITY_TABLE} "
            f"FROM {_INDICATOR_ROLE}, vm_market_data"
        )
        op.execute(
            f"REVOKE ALL PRIVILEGES ON SEQUENCE "
            f"public.{_SECURITY_IDENTITY_TABLE}_identity_id_seq FROM vm_market_data"
        )
    op.drop_index("ix_strategy_panel_pending", table_name=_TABLE)
    op.drop_index("uq_strategy_panel_authoritative_cutoff", table_name=_TABLE)
    op.drop_index("ix_strategy_panel_worker_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_index("ix_strategy_panel_input_poll", table_name=_INPUT_TABLE)
    op.drop_table(_INPUT_TABLE)
    op.drop_index(
        "ix_equity_security_identity_observation",
        table_name=_SECURITY_IDENTITY_TABLE,
    )
    op.drop_index(
        "ix_equity_security_identity_effective",
        table_name=_SECURITY_IDENTITY_TABLE,
    )
    op.drop_index(
        "ix_equity_security_identities_instr_id",
        table_name=_SECURITY_IDENTITY_TABLE,
    )
    op.drop_table(_SECURITY_IDENTITY_TABLE)
    with op.batch_alter_table("market_calendars") as batch_op:
        batch_op.drop_index("ix_market_calendars_observation_id")
        batch_op.drop_constraint(
            "fk_market_calendar_observation",
            type_="foreignkey",
        )
        batch_op.drop_column("observation_id")
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
            "'market_cap')",
        )
    with op.batch_alter_table("index_membership") as batch_op:
        batch_op.drop_index("ix_index_membership_observation_id")
        batch_op.drop_constraint(
            "fk_index_membership_observation",
            type_="foreignkey",
        )
        batch_op.drop_column("observation_id")
        batch_op.drop_constraint("ck_index_membership_span", type_="check")
        batch_op.create_check_constraint(
            "ck_index_membership_span",
            "effective_to IS NULL OR effective_to > effective_from",
        )
