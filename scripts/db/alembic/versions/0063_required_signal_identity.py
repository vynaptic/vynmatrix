"""Require durable signal and execution idempotency identities.

Every active producer derives ``external_signal_id`` deterministically at the
HTTP boundary. Allowing identity-less canonical signals, asset scores, or
execution decisions leaves redelivery free to create duplicate trading work.
This migration refuses to invent identities for historical rows; operators must
reconcile or remove unattributed data before upgrading.

Revision ID: 0063_required_signal_identity
Revises: 0062_historical_price_rebuild
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_required_signal_identity"
down_revision = "0062_historical_price_rebuild"
branch_labels = None
depends_on = None

_IDENTITY_CHECKS = (
    (
        "canonical_signals.external_signal_id",
        """
        SELECT count(*)
        FROM canonical_signals
        WHERE external_signal_id IS NULL OR trim(external_signal_id) = ''
        """,
    ),
    (
        "asset_scores.external_signal_id",
        """
        SELECT count(*)
        FROM asset_scores
        WHERE external_signal_id IS NULL OR trim(external_signal_id) = ''
        """,
    ),
    (
        "execution_decision_logs.idempotency_key",
        """
        SELECT count(*)
        FROM execution_decision_logs
        WHERE idempotency_key IS NULL OR trim(idempotency_key) = ''
        """,
    ),
)


def _assert_all_identities_present() -> None:
    bind = op.get_bind()
    invalid: list[str] = []
    for label, query in _IDENTITY_CHECKS:
        count = int(bind.execute(sa.text(query)).scalar_one())
        if count:
            invalid.append(f"{label}={count}")
    if invalid:
        msg = (
            "Required signal-identity migration found unattributed historical "
            f"rows ({', '.join(invalid)}). Reconcile or remove them before upgrade."
        )
        raise RuntimeError(msg)


def upgrade() -> None:
    _assert_all_identities_present()
    with op.batch_alter_table("canonical_signals") as batch:
        batch.alter_column(
            "external_signal_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_canonical_signal_external_identity",
            "length(trim(external_signal_id)) > 0",
        )
    with op.batch_alter_table("asset_scores") as batch:
        batch.alter_column(
            "external_signal_id",
            existing_type=sa.String(length=64),
            type_=sa.String(length=128),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_asset_score_external_identity",
            "length(trim(external_signal_id)) > 0",
        )
    with op.batch_alter_table("execution_decision_logs") as batch:
        batch.alter_column(
            "idempotency_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_execution_decision_idempotency_key",
            "length(trim(idempotency_key)) > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("execution_decision_logs") as batch:
        batch.drop_constraint(
            "ck_execution_decision_idempotency_key",
            type_="check",
        )
        batch.alter_column(
            "idempotency_key",
            existing_type=sa.String(length=64),
            nullable=True,
        )
    with op.batch_alter_table("asset_scores") as batch:
        batch.drop_constraint(
            "ck_asset_score_external_identity",
            type_="check",
        )
        batch.alter_column(
            "external_signal_id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=64),
            nullable=True,
        )
    with op.batch_alter_table("canonical_signals") as batch:
        batch.drop_constraint(
            "ck_canonical_signal_external_identity",
            type_="check",
        )
        batch.alter_column(
            "external_signal_id",
            existing_type=sa.String(length=128),
            nullable=True,
        )
