"""Record which build is installed, and let the backend read it.

Revision ID: 0108_deployment_record
Revises: 0107_backend_ui_read

Nothing in the database said which image, commit or schema head a running
installation came from, so "which build is live?" was unanswerable and drift
was invisible. ``vmdev deploy`` opens a row here before it changes anything and
closes it with the outcome, which also makes an interrupted run visibly
incomplete rather than absent.

The table describes the installation, not a user, so it carries no row
security -- like ``canonical_signals`` and ``prices`` in ``0107``. The backend
role receives column-level ``SELECT`` on exactly the columns the owner UI's
version endpoint reads: the snapshot path, the image digest and the failure
stage stay maintenance-only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0108_deployment_record"
down_revision = "0107_backend_ui_read"
branch_labels = None
depends_on = None

_ROLE = "vm_backend"
TABLE = "deployments"
OUTCOMES = ("succeeded", "rolled_back", "failed")
MODES = ("install", "upgrade")

#: Exactly what ``backend.ui_queries.version`` selects, and nothing more.
READ_COLUMNS: tuple[str, ...] = (
    "deployment_id",
    "mode",
    "image_tag",
    "source_commit",
    "source_dirty",
    "alembic_head",
    "started_at",
    "finished_at",
    "outcome",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "deployment_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("image_tag", sa.String(length=255), nullable=False),
        sa.Column("image_digest", sa.String(length=255), nullable=False),
        sa.Column("source_commit", sa.String(length=64), nullable=False),
        sa.Column("source_dirty", sa.Boolean(), nullable=False),
        sa.Column("alembic_head", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_stage", sa.String(length=50), nullable=True),
        sa.Column("snapshot_path", sa.Text(), nullable=True),
        sa.Column("snapshot_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(_in_list("outcome", OUTCOMES), name="ck_deployment_outcome"),
        sa.CheckConstraint(_in_list("mode", MODES), name="ck_deployment_mode"),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_deployment_finished_after_start",
        ),
        sa.CheckConstraint(
            "snapshot_path IS NOT NULL OR snapshot_reason IS NOT NULL",
            name="ck_deployment_snapshot_accounted",
        ),
        sa.PrimaryKeyConstraint("deployment_id"),
    )
    op.create_index(op.f("ix_deployments_started_at"), TABLE, ["started_at"], unique=False)
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"GRANT SELECT ({', '.join(READ_COLUMNS)}) ON TABLE public.{TABLE} TO {_ROLE}")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"REVOKE SELECT ({', '.join(READ_COLUMNS)}) ON TABLE public.{TABLE} FROM {_ROLE}"
        )
    op.drop_index(op.f("ix_deployments_started_at"), table_name=TABLE)
    op.drop_table(TABLE)


__all__ = ["MODES", "OUTCOMES", "READ_COLUMNS", "TABLE", "downgrade", "upgrade"]
