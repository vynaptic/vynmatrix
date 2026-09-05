"""Equity reference data: corporate_actions, index_membership, earnings_events.

The three global reference tables of the US-equity data plane (see
``strategies/indicator/USQualityCompounder/README.md``): corporate actions feed
the in-house price-adjustment pipeline (§7.2), index membership is the point-in-time
universe / survivorship control (§3), and earnings events drive the entry
blackout (§4.5). Reference data, not tenant data — no RLS (the 0039/0040
policies cover user-/account-owned tables only; ``vm_app`` grants arrive via
the 0039 default privileges).

Revision ID: 0084_equity_reference_data
Revises: 0083_paper_fill_projection_checkpoint
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084_equity_reference_data"
down_revision = "0083_paper_fill_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column(
            "action_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.CheckConstraint("action_type IN ('split', 'dividend')", name="ck_corporate_action_type"),
        sa.CheckConstraint(
            "(action_type = 'split' AND ratio IS NOT NULL) "
            "OR (action_type = 'dividend' AND amount IS NOT NULL)",
            name="ck_corporate_action_payload",
        ),
        sa.ForeignKeyConstraint(["instr_id"], ["instruments.instr_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint(
            "instr_id", "action_type", "ex_date", "source", name="uq_corporate_actions_event"
        ),
    )
    op.create_index(
        op.f("ix_corporate_actions_instr_id"), "corporate_actions", ["instr_id"], unique=False
    )
    op.create_index(
        "ix_corporate_actions_instr_ex_date",
        "corporate_actions",
        ["instr_id", "ex_date"],
        unique=False,
    )

    op.create_table(
        "index_membership",
        sa.Column("membership_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("index_code", sa.String(length=20), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_ref", sa.String(length=200), nullable=False),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_index_membership_span",
        ),
        sa.ForeignKeyConstraint(["instr_id"], ["instruments.instr_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint("index_code", "instr_id", "effective_from", name="uq_index_membership"),
    )
    op.create_index(
        op.f("ix_index_membership_index_code"), "index_membership", ["index_code"], unique=False
    )
    op.create_index(
        op.f("ix_index_membership_instr_id"), "index_membership", ["instr_id"], unique=False
    )
    op.create_index(
        "ix_index_membership_code_from",
        "index_membership",
        ["index_code", "effective_from"],
        unique=False,
    )

    op.create_table(
        "earnings_events",
        sa.Column(
            "event_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("announce_time", sa.String(length=10), server_default="unknown", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.CheckConstraint(
            "announce_time IN ('bmo', 'amc', 'dmh', 'unknown')",
            name="ck_earnings_announce_time",
        ),
        sa.CheckConstraint(
            "status IN ('presumed', 'confirmed', 'actual')", name="ck_earnings_status"
        ),
        sa.ForeignKeyConstraint(["instr_id"], ["instruments.instr_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("instr_id", "report_date", "source", name="uq_earnings_event"),
    )
    op.create_index(
        op.f("ix_earnings_events_instr_id"), "earnings_events", ["instr_id"], unique=False
    )
    op.create_index(
        "ix_earnings_events_instr_report",
        "earnings_events",
        ["instr_id", "report_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_earnings_events_instr_report", table_name="earnings_events")
    op.drop_index(op.f("ix_earnings_events_instr_id"), table_name="earnings_events")
    op.drop_table("earnings_events")
    op.drop_index("ix_index_membership_code_from", table_name="index_membership")
    op.drop_index(op.f("ix_index_membership_instr_id"), table_name="index_membership")
    op.drop_index(op.f("ix_index_membership_index_code"), table_name="index_membership")
    op.drop_table("index_membership")
    op.drop_index("ix_corporate_actions_instr_ex_date", table_name="corporate_actions")
    op.drop_index(op.f("ix_corporate_actions_instr_id"), table_name="corporate_actions")
    op.drop_table("corporate_actions")
