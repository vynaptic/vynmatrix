"""Purge retired LEAN values from column defaults + the engine_kind check.

LEAN was retired (D9). Three schema spots still referenced it:
  - prices.source default 'lean' -> 'coinbase_live' (the live market-data source)
  - backtest_results.engine default 'lean' -> 'internal' (the in-house engine)
  - strategy_versions.engine_kind CHECK allowed 'lean' -> dropped from the set

Revision ID: 0024_purge_lean_defaults
Revises: 0023_merge_execution_log_heads
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0024_purge_lean_defaults"
down_revision = "0023_merge_execution_log_heads"
branch_labels = None
depends_on = None

_OLD_ENGINE_KIND = "'lean', 'python_service', 'spark', 'ray'"
_NEW_ENGINE_KIND = "'python_service', 'spark', 'ray'"


def _set_default(table: str, column: str, value: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(column, server_default=value)
        return
    op.alter_column(table, column, server_default=value)


def _replace_engine_kind(values: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("strategy_versions") as batch_op:
            batch_op.drop_constraint("ck_engine_kind", type_="check")
            batch_op.create_check_constraint("ck_engine_kind", f"engine_kind IN ({values})")
        return
    op.drop_constraint("ck_engine_kind", "strategy_versions", type_="check")
    op.create_check_constraint("ck_engine_kind", "strategy_versions", f"engine_kind IN ({values})")


def upgrade() -> None:
    _set_default("prices", "source", "coinbase_live")
    _set_default("backtest_results", "engine", "internal")
    _replace_engine_kind(_NEW_ENGINE_KIND)


def downgrade() -> None:
    _replace_engine_kind(_OLD_ENGINE_KIND)
    _set_default("backtest_results", "engine", "lean")
    _set_default("prices", "source", "lean")
