"""Align indexes / FKs / unique constraints with the ORM models (final drift pass).

Brings the migration schema's index names, FK ON DELETE CASCADE behaviour, and
uniqueness representation (unique index vs named constraint) in line with the
models, plus the two model-only constraints and the model-only instruments/
prices indexes. Also realigns the backtest_results engine CHECK (which
compare_metadata cannot diff) to the post-LEAN values. After this the
model<->migration drift is zero.

Recreated FKs/constraints are given their explicit PostgreSQL-default names so
the migration is reversible (autogenerate emitted ``None`` names, which cannot
be dropped on downgrade).

Revision ID: 0028_align_indexes_fks
Revises: 0027_align_columns
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0028_align_indexes_fks"
down_revision = "0027_align_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Uniqueness: migration UNIQUE CONSTRAINT -> model unique INDEX.
    op.drop_constraint("brokers_code_key", "brokers", type_="unique")
    op.create_index("ix_brokers_code", "brokers", ["code"], unique=True)
    op.drop_constraint("instrument_aliases_alias_key", "instrument_aliases", type_="unique")
    op.drop_index("ix_instrument_alias_alias", table_name="instrument_aliases")
    op.create_index("ix_instrument_aliases_alias", "instrument_aliases", ["alias"], unique=True)
    op.drop_constraint("orgs_name_key", "orgs", type_="unique")
    op.create_index("ix_orgs_name", "orgs", ["name"], unique=True)
    op.drop_constraint("sectors_code_key", "sectors", type_="unique")
    op.create_index("ix_sectors_code", "sectors", ["code"], unique=True)
    op.drop_constraint("users_email_key", "users", type_="unique")
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # Index renames + model-only indexes.
    op.drop_index("ix_canonical_signal_run_id", table_name="canonical_signals")
    op.create_index("ix_canonical_signals_run_id", "canonical_signals", ["run_id"], unique=False)
    op.drop_index("ix_exec_decision_idempotency_key", table_name="execution_decision_logs")
    op.create_index(
        "ix_execution_decision_logs_idempotency_key",
        "execution_decision_logs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_instrument_asset_canonical", "instruments", ["asset_class", "canonical"], unique=False
    )
    op.create_index("ix_instruments_asset_class", "instruments", ["asset_class"], unique=False)
    op.create_index("ix_instruments_canonical", "instruments", ["canonical"], unique=False)
    op.create_index("ix_prices_instr_id", "prices", ["instr_id"], unique=False)
    op.create_index("ix_prices_ts", "prices", ["ts"], unique=False)

    # Model-only constraints.
    op.create_unique_constraint(
        "canonical_signals_external_signal_id_key", "canonical_signals", ["external_signal_id"]
    )
    op.create_unique_constraint("uq_instrument", "instruments", ["asset_class", "canonical"])

    # FK ON DELETE CASCADE alignment + canonical_signals FK rename to default.
    op.drop_constraint("fk_canonical_signals_strat_ver", "canonical_signals", type_="foreignkey")
    op.create_foreign_key(
        "canonical_signals_strat_ver_id_fkey",
        "canonical_signals",
        "strategy_versions",
        ["strat_ver_id"],
        ["strat_ver_id"],
    )
    op.drop_constraint(
        "instrument_broker_symbols_instr_id_fkey",
        "instrument_broker_symbols",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "instrument_broker_symbols_instr_id_fkey",
        "instrument_broker_symbols",
        "instruments",
        ["instr_id"],
        ["instr_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("instrument_sectors_instr_id_fkey", "instrument_sectors", type_="foreignkey")
    op.drop_constraint(
        "instrument_sectors_sector_id_fkey", "instrument_sectors", type_="foreignkey"
    )
    op.create_foreign_key(
        "instrument_sectors_sector_id_fkey",
        "instrument_sectors",
        "sectors",
        ["sector_id"],
        ["sector_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "instrument_sectors_instr_id_fkey",
        "instrument_sectors",
        "instruments",
        ["instr_id"],
        ["instr_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "option_strategy_template_legs_template_id_fkey",
        "option_strategy_template_legs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "option_strategy_template_legs_template_id_fkey",
        "option_strategy_template_legs",
        "option_strategy_templates",
        ["template_id"],
        ["template_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("prices_instr_id_fkey", "prices", type_="foreignkey")
    op.create_foreign_key(
        "prices_instr_id_fkey", "prices", "instruments", ["instr_id"], ["instr_id"], ondelete="CASCADE"
    )

    # compare_metadata does not diff CHECK constraints. The backtest_results
    # engine CHECK still allowed retired LEAN/backtrader/zipline and excluded the
    # current 'internal' default -> realign to the live values.
    op.drop_constraint("ck_backtest_engine", "backtest_results", type_="check")
    op.create_check_constraint(
        "ck_backtest_engine", "backtest_results", "engine IN ('internal', 'custom')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_backtest_engine", "backtest_results", type_="check")
    op.create_check_constraint(
        "ck_backtest_engine",
        "backtest_results",
        "engine IN ('lean', 'backtrader', 'zipline', 'custom')",
    )

    # Revert FK CASCADE alignment.
    op.drop_constraint("prices_instr_id_fkey", "prices", type_="foreignkey")
    op.create_foreign_key(
        "prices_instr_id_fkey", "prices", "instruments", ["instr_id"], ["instr_id"]
    )
    op.drop_constraint(
        "option_strategy_template_legs_template_id_fkey",
        "option_strategy_template_legs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "option_strategy_template_legs_template_id_fkey",
        "option_strategy_template_legs",
        "option_strategy_templates",
        ["template_id"],
        ["template_id"],
    )
    op.drop_constraint("instrument_sectors_instr_id_fkey", "instrument_sectors", type_="foreignkey")
    op.drop_constraint(
        "instrument_sectors_sector_id_fkey", "instrument_sectors", type_="foreignkey"
    )
    op.create_foreign_key(
        "instrument_sectors_sector_id_fkey",
        "instrument_sectors",
        "sectors",
        ["sector_id"],
        ["sector_id"],
    )
    op.create_foreign_key(
        "instrument_sectors_instr_id_fkey",
        "instrument_sectors",
        "instruments",
        ["instr_id"],
        ["instr_id"],
    )
    op.drop_constraint(
        "instrument_broker_symbols_instr_id_fkey",
        "instrument_broker_symbols",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "instrument_broker_symbols_instr_id_fkey",
        "instrument_broker_symbols",
        "instruments",
        ["instr_id"],
        ["instr_id"],
    )
    op.drop_constraint(
        "canonical_signals_strat_ver_id_fkey", "canonical_signals", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_canonical_signals_strat_ver",
        "canonical_signals",
        "strategy_versions",
        ["strat_ver_id"],
        ["strat_ver_id"],
        initially="DEFERRED",
        deferrable=True,
    )

    # Revert model-only constraints.
    op.drop_constraint("uq_instrument", "instruments", type_="unique")
    op.drop_constraint(
        "canonical_signals_external_signal_id_key", "canonical_signals", type_="unique"
    )

    # Revert index renames + model-only indexes.
    op.drop_index("ix_prices_ts", table_name="prices")
    op.drop_index("ix_prices_instr_id", table_name="prices")
    op.drop_index("ix_instruments_canonical", table_name="instruments")
    op.drop_index("ix_instruments_asset_class", table_name="instruments")
    op.drop_index("ix_instrument_asset_canonical", table_name="instruments")
    op.drop_index(
        "ix_execution_decision_logs_idempotency_key", table_name="execution_decision_logs"
    )
    op.create_index(
        "ix_exec_decision_idempotency_key",
        "execution_decision_logs",
        ["idempotency_key"],
        unique=True,
    )
    op.drop_index("ix_canonical_signals_run_id", table_name="canonical_signals")
    op.create_index("ix_canonical_signal_run_id", "canonical_signals", ["run_id"], unique=False)

    # Revert uniqueness form (unique INDEX -> UNIQUE CONSTRAINT).
    op.drop_index("ix_users_email", table_name="users")
    op.create_unique_constraint("users_email_key", "users", ["email"])
    op.drop_index("ix_sectors_code", table_name="sectors")
    op.create_unique_constraint("sectors_code_key", "sectors", ["code"])
    op.drop_index("ix_orgs_name", table_name="orgs")
    op.create_unique_constraint("orgs_name_key", "orgs", ["name"])
    op.drop_index("ix_instrument_aliases_alias", table_name="instrument_aliases")
    op.create_index("ix_instrument_alias_alias", "instrument_aliases", ["alias"], unique=True)
    op.create_unique_constraint("instrument_aliases_alias_key", "instrument_aliases", ["alias"])
    op.drop_index("ix_brokers_code", table_name="brokers")
    op.create_unique_constraint("brokers_code_key", "brokers", ["code"])
