"""Add immutable backtest trial provenance ledger.

Revision ID: 0042_backtest_trial_provenance
Revises: 0041_strategy_version_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_backtest_trial_provenance"
down_revision = "0041_strategy_version_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite lineage keys let the trial ledger reject a strategy paired with
    # another strategy's experiment, version, or result at the database boundary.
    op.create_unique_constraint(
        "uq_backtest_experiment_lineage",
        "backtest_experiments",
        ["experiment_id", "strategy_id"],
    )
    op.create_unique_constraint(
        "uq_strategy_version_lineage",
        "strategy_versions",
        ["strat_ver_id", "strategy_id"],
    )
    op.create_unique_constraint(
        "uq_backtest_result_lineage",
        "backtest_results",
        ["result_id", "strategy_id", "strat_ver_id", "experiment_id"],
    )
    op.create_table(
        "backtest_trials",
        sa.Column("trial_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strat_ver_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("trial_family", sa.String(length=64), nullable=False),
        sa.Column("fold_id", sa.String(length=64), nullable=False),
        sa.Column("cost_scenario", sa.String(length=64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="registered", nullable=False),
        sa.Column("error_class", sa.String(length=255), nullable=True),
        sa.Column("error_context", sa.Text(), nullable=True),
        sa.Column("result_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_backtest_trial_sequence"),
        sa.CheckConstraint(
            "status IN ('registered', 'running', 'completed', 'failed', "
            "'interrupted', 'abandoned')",
            name="ck_backtest_trial_status",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64",
            name="ck_backtest_trial_manifest_hash",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id", "strategy_id"],
            ["backtest_experiments.experiment_id", "backtest_experiments.strategy_id"],
            name="fk_backtest_trial_experiment_lineage",
        ),
        sa.ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_backtest_trial_version_lineage",
        ),
        sa.ForeignKeyConstraint(
            ["result_id", "strategy_id", "strat_ver_id", "experiment_id"],
            [
                "backtest_results.result_id",
                "backtest_results.strategy_id",
                "backtest_results.strat_ver_id",
                "backtest_results.experiment_id",
            ],
            name="fk_backtest_trial_result_lineage",
        ),
        sa.PrimaryKeyConstraint("trial_id"),
        sa.UniqueConstraint(
            "experiment_id",
            "sequence",
            name="uq_backtest_trial_experiment_sequence",
        ),
        sa.UniqueConstraint("result_id", name="uq_backtest_trial_result"),
    )
    op.create_index(
        "ix_backtest_trial_experiment_status",
        "backtest_trials",
        ["experiment_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_trial_strategy_version",
        "backtest_trials",
        ["strategy_id", "strat_ver_id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_trial_manifest",
        "backtest_trials",
        ["manifest_hash"],
        unique=False,
    )

    # The service owns legal lifecycle transitions, but identity immutability
    # and evidence retention are also enforced at the PostgreSQL boundary so a
    # bulk SQL update/delete cannot bypass the ORM listeners.
    op.execute(
        """
        CREATE FUNCTION prevent_backtest_trial_rewrite()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'backtest trials are append-only and cannot be deleted';
            END IF;

            IF OLD.trial_id IS DISTINCT FROM NEW.trial_id
               OR OLD.experiment_id IS DISTINCT FROM NEW.experiment_id
               OR OLD.strategy_id IS DISTINCT FROM NEW.strategy_id
               OR OLD.strat_ver_id IS DISTINCT FROM NEW.strat_ver_id
               OR OLD.sequence IS DISTINCT FROM NEW.sequence
               OR OLD.trial_family IS DISTINCT FROM NEW.trial_family
               OR OLD.fold_id IS DISTINCT FROM NEW.fold_id
               OR OLD.cost_scenario IS DISTINCT FROM NEW.cost_scenario
               OR OLD.parameters::jsonb IS DISTINCT FROM NEW.parameters::jsonb
               OR OLD.manifest_hash IS DISTINCT FROM NEW.manifest_hash
            THEN
                RAISE EXCEPTION 'backtest trial identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_backtest_trial_immutable
        BEFORE UPDATE OR DELETE ON backtest_trials
        FOR EACH ROW EXECUTE FUNCTION prevent_backtest_trial_rewrite()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM backtest_trials LIMIT 1) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 0042: immutable backtest trial evidence exists';
            END IF;
        END $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_backtest_trial_immutable ON backtest_trials")
    op.execute("DROP FUNCTION IF EXISTS prevent_backtest_trial_rewrite()")
    op.drop_index("ix_backtest_trial_manifest", table_name="backtest_trials")
    op.drop_index("ix_backtest_trial_strategy_version", table_name="backtest_trials")
    op.drop_index("ix_backtest_trial_experiment_status", table_name="backtest_trials")
    op.drop_table("backtest_trials")
    op.drop_constraint(
        "uq_backtest_result_lineage",
        "backtest_results",
        type_="unique",
    )
    op.drop_constraint(
        "uq_strategy_version_lineage",
        "strategy_versions",
        type_="unique",
    )
    op.drop_constraint(
        "uq_backtest_experiment_lineage",
        "backtest_experiments",
        type_="unique",
    )
