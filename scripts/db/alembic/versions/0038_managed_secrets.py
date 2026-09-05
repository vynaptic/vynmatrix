"""Add managed_secrets table (DB-encrypted secrets backend).

Backs the ``db`` SECRETS_BACKEND (``DbSecretsProvider``): per-account broker
credentials are stored as ciphertext, encrypted at rest with a symmetric master
key selected from the process-held ``SECRETS_MASTER_KEYS`` rotation ring. This
gives self-hosted / DigitalOcean deployments a provider-agnostic, per-user secret
store without a managed secrets service, and lets a tenant be onboarded with a row
insert (no redeploy). The ``secret_ref`` matches ``broker_credentials.secret_ref``.

Revision ID: 0038_managed_secrets
Revises: 0037_signal_perf_15min_horizon
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_managed_secrets"
down_revision = "0037_signal_perf_15min_horizon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_secrets",
        sa.Column("secret_ref", sa.String(length=500), primary_key=True, nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("managed_secrets")
