"""Converge model-rebalance prior-leg identity constraints.

Revision ID: 0096_model_prior_identity
Revises: 0095_binding_total_exposure

Some databases reached ``0088`` before the model-rebalance prior-leg reference
was strengthened from the legacy two-column identity to the current exact
four-column identity.  Fresh databases already receive the exact constraints
from ``0088``.  This repair revision is therefore deliberately convergent: it
changes only a legacy schema and is a no-op on a freshly migrated schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096_model_prior_identity"
down_revision = "0095_binding_total_exposure"
branch_labels = None
depends_on = None

_TABLE = "model_rebalance_legs"
_LEGACY_FK = "fk_model_rebalance_leg_prior_leg"
_EXACT_FK = "fk_model_rebalance_leg_prior_identity"
_EXACT_UNIQUE = "uq_model_rebalance_leg_prior_identity"
_EXACT_COLUMNS = ("rebalance_id", "leg_id", "instr_id", "factor_snapshot_id")
_PRIOR_COLUMNS = (
    "prior_model_rebalance_id",
    "prior_model_leg_id",
    "instr_id",
    "factor_snapshot_id",
)


def _constraint_names() -> tuple[set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    unique_names = {
        str(item["name"]) for item in inspector.get_unique_constraints(_TABLE) if item.get("name")
    }
    foreign_key_names = {
        str(item["name"]) for item in inspector.get_foreign_keys(_TABLE) if item.get("name")
    }
    return unique_names, foreign_key_names


def _assert_legacy_rows_are_exact() -> None:
    mismatches = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM model_rebalance_legs AS child "
            "LEFT JOIN model_rebalance_legs AS prior "
            "ON prior.rebalance_id = child.prior_model_rebalance_id "
            "AND prior.leg_id = child.prior_model_leg_id "
            "WHERE child.prior_model_rebalance_id IS NOT NULL "
            "AND child.prior_model_leg_id IS NOT NULL "
            "AND (prior.rebalance_id IS NULL "
            "OR prior.instr_id <> child.instr_id "
            "OR prior.factor_snapshot_id <> child.factor_snapshot_id)"
        )
    )
    if int(mismatches or 0):
        message = "Cannot repair model-rebalance prior identity with mismatched history"
        raise RuntimeError(message)


def upgrade() -> None:
    unique_names, foreign_key_names = _constraint_names()
    legacy_present = _LEGACY_FK in foreign_key_names
    exact_unique_present = _EXACT_UNIQUE in unique_names
    exact_fk_present = _EXACT_FK in foreign_key_names

    if exact_unique_present and exact_fk_present and not legacy_present:
        return

    _assert_legacy_rows_are_exact()
    if not exact_unique_present:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_unique_constraint(_EXACT_UNIQUE, list(_EXACT_COLUMNS))

    if not exact_fk_present or legacy_present:
        with op.batch_alter_table(_TABLE) as batch_op:
            if not exact_fk_present:
                batch_op.create_foreign_key(
                    _EXACT_FK,
                    _TABLE,
                    list(_PRIOR_COLUMNS),
                    list(_EXACT_COLUMNS),
                    ondelete="RESTRICT",
                )
            if legacy_present:
                batch_op.drop_constraint(_LEGACY_FK, type_="foreignkey")


def downgrade() -> None:
    """Retain the exact contract expected by the current ``0088`` schema."""


__all__ = ["downgrade", "upgrade"]
