"""Add forward-only optional equity-factor source contracts.

Revision ID: 0090_optional_factor_contracts
Revises: 0089_account_execution_fence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090_optional_factor_contracts"
down_revision = "0089_account_execution_fence"
branch_labels = None
depends_on = None

_REGISTRY_SHA256 = "9eb5899f62939d2e1ac9cca52602afcc35fbbf0c16d59a561758cbebec88fa4d"
_OPTIONAL_KINDS = (
    "analyst_estimate",
    "call_transcript",
    "insider_transaction",
    "macro_release",
    "news_event",
    "positioning",
)
_PREVIOUS_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity')"
)
_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release')"
)
_PREVIOUS_FACTOR_DIGEST_CHECK = (
    "length(factor_snapshot_id) = 64 AND length(configuration_digest) = 64 "
    "AND length(content_sha256) = 64"
)
_FACTOR_DIGEST_CHECK = (
    "length(factor_snapshot_id) = 64 AND length(configuration_digest) = 64 "
    "AND length(source_contract_registry_sha256) = 64 "
    "AND length(content_sha256) = 64"
)


def upgrade() -> None:
    with op.batch_alter_table("equity_source_lineages") as batch_op:
        batch_op.add_column(
            sa.Column("entitlement_owner_user_id", sa.String(length=50), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_equity_source_entitlement_owner",
            "users",
            ["entitlement_owner_user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_equity_source_entitlement_owner",
            "entitlement_owner_user_id IS NULL OR length(trim(entitlement_owner_user_id)) > 0",
        )
        batch_op.create_index(
            "ix_equity_source_entitlement_owner",
            ["entitlement_owner_user_id"],
        )

    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            _OBSERVATION_KIND_CHECK,
        )

    with op.batch_alter_table("equity_factor_snapshots") as batch_op:
        batch_op.add_column(
            sa.Column("source_contract_registry_sha256", sa.String(length=64), nullable=True)
        )
    op.execute(
        sa.text(
            "UPDATE equity_factor_snapshots "
            "SET source_contract_registry_sha256 = :registry_sha256 "
            "WHERE source_contract_registry_sha256 IS NULL"
        ).bindparams(registry_sha256=_REGISTRY_SHA256)
    )
    with op.batch_alter_table("equity_factor_snapshots") as batch_op:
        batch_op.alter_column(
            "source_contract_registry_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.drop_constraint("ck_equity_factor_digests", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_factor_digests",
            _FACTOR_DIGEST_CHECK,
        )


def downgrade() -> None:
    connection = op.get_bind()
    optional_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM equity_observations "
            "WHERE observation_kind IN "
            "('analyst_estimate', 'call_transcript', 'insider_transaction', "
            "'macro_release', 'news_event', 'positioning')"
        )
    )
    owner_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM equity_source_lineages "
            "WHERE entitlement_owner_user_id IS NOT NULL"
        )
    )
    factor_count = connection.scalar(sa.text("SELECT count(*) FROM equity_factor_snapshots"))
    if int(optional_count or 0) or int(owner_count or 0) or int(factor_count or 0):
        message = "Cannot remove optional equity-factor contracts after immutable evidence exists"
        raise RuntimeError(message)

    with op.batch_alter_table("equity_factor_snapshots") as batch_op:
        batch_op.drop_constraint("ck_equity_factor_digests", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_factor_digests",
            _PREVIOUS_FACTOR_DIGEST_CHECK,
        )
        batch_op.drop_column("source_contract_registry_sha256")

    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            _PREVIOUS_OBSERVATION_KIND_CHECK,
        )

    with op.batch_alter_table("equity_source_lineages") as batch_op:
        batch_op.drop_index("ix_equity_source_entitlement_owner")
        batch_op.drop_constraint("ck_equity_source_entitlement_owner", type_="check")
        batch_op.drop_constraint("fk_equity_source_entitlement_owner", type_="foreignkey")
        batch_op.drop_column("entitlement_owner_user_id")


__all__ = [
    "_OPTIONAL_KINDS",
    "_REGISTRY_SHA256",
    "downgrade",
    "upgrade",
]
