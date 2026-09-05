"""Require authoritative market-session coverage for scheduled instruments.

Crypto instruments are explicitly persisted as continuous markets. Every
non-crypto instrument is scheduled and remains fail-closed until an operator
assigns a calendar synchronized from an official broker or exchange source.
Downgrade refuses to discard any persisted calendar, session, or assignment.

Revision ID: 0067_market_sessions
Revises: 0066_saxo_broker_catalogue
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_market_sessions"
down_revision = "0066_saxo_broker_catalogue"
branch_labels = None
depends_on = None

_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _grant_runtime_privileges() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "GRANT SELECT ON TABLE public.market_calendars, public.market_sessions TO vm_execution"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.market_calendars TO vm_backend")
    op.execute("GRANT SELECT, INSERT, DELETE ON TABLE public.market_sessions TO vm_backend")
    op.execute("GRANT SELECT, UPDATE ON TABLE public.instruments TO vm_backend")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE public.market_calendars_calendar_id_seq, "
        "public.market_sessions_session_id_seq TO vm_backend"
    )


def _lock_downgrade_state() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE market_sessions, market_calendars, instruments IN ACCESS EXCLUSIVE MODE")


def _assert_no_authoritative_state_for_downgrade() -> None:
    bind = op.get_bind()
    calendars = int(bind.execute(sa.text("SELECT COUNT(*) FROM market_calendars")).scalar_one())
    sessions = int(bind.execute(sa.text("SELECT COUNT(*) FROM market_sessions")).scalar_one())
    assignments = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM instruments WHERE market_calendar_id IS NOT NULL")
        ).scalar_one()
    )
    if calendars or sessions or assignments:
        msg = (
            "Cannot downgrade authoritative market sessions while persisted "
            f"state exists (calendars={calendars}, sessions={sessions}, "
            f"instrument_assignments={assignments}); export and retire that "
            "operator-owned schedule state explicitly before downgrading."
        )
        raise RuntimeError(msg)


def _revoke_runtime_privileges() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("REVOKE UPDATE ON TABLE public.instruments FROM vm_backend")


def upgrade() -> None:
    op.create_table(
        "market_calendars",
        sa.Column("calendar_id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("coverage_start", sa.DateTime(timezone=True)),
        sa.Column("coverage_end", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_kind IN ('broker', 'exchange')",
            name="ck_market_calendar_source_kind",
        ),
        sa.CheckConstraint(
            "length(trim(code)) > 0 AND length(trim(provider)) > 0 "
            "AND length(trim(source_reference)) > 0",
            name="ck_market_calendar_provenance",
        ),
        sa.CheckConstraint(
            "(coverage_start IS NULL AND coverage_end IS NULL AND observed_at IS NULL) OR "
            "(coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND observed_at IS NOT NULL AND coverage_end > coverage_start)",
            name="ck_market_calendar_coverage",
        ),
        sa.UniqueConstraint("code", name="uq_market_calendars_code"),
    )
    op.create_table(
        "market_sessions",
        sa.Column("session_id", _ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column(
            "calendar_id",
            _ID_TYPE,
            sa.ForeignKey("market_calendars.calendar_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "closes_at > opens_at",
            name="ck_market_session_interval",
        ),
        sa.UniqueConstraint(
            "calendar_id",
            "opens_at",
            "closes_at",
            name="uq_market_session_window",
        ),
    )
    op.create_index(
        "ix_market_sessions_calendar_window",
        "market_sessions",
        ["calendar_id", "opens_at", "closes_at"],
    )

    with op.batch_alter_table("instruments") as batch:
        batch.add_column(
            sa.Column(
                "market_session_policy",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'scheduled'"),
            )
        )
        batch.add_column(sa.Column("market_calendar_id", _ID_TYPE))
        batch.create_foreign_key(
            "fk_instruments_market_calendar_id",
            "market_calendars",
            ["market_calendar_id"],
            ["calendar_id"],
            ondelete="RESTRICT",
        )

    op.execute(
        "UPDATE instruments SET market_session_policy = 'continuous' WHERE asset_class = 'crypto'"
    )
    with op.batch_alter_table("instruments") as batch:
        batch.create_check_constraint(
            "ck_instrument_market_session_policy",
            "(asset_class = 'crypto' AND market_session_policy = 'continuous' "
            "AND market_calendar_id IS NULL) OR "
            "(asset_class <> 'crypto' AND market_session_policy = 'scheduled')",
        )
        batch.create_index(
            "ix_instruments_market_calendar_id",
            ["market_calendar_id"],
        )

    _grant_runtime_privileges()


def downgrade() -> None:
    _lock_downgrade_state()
    _assert_no_authoritative_state_for_downgrade()
    _revoke_runtime_privileges()

    with op.batch_alter_table("instruments") as batch:
        batch.drop_index("ix_instruments_market_calendar_id")
        batch.drop_constraint(
            "ck_instrument_market_session_policy",
            type_="check",
        )
        batch.drop_constraint(
            "fk_instruments_market_calendar_id",
            type_="foreignkey",
        )
        batch.drop_column("market_calendar_id")
        batch.drop_column("market_session_policy")
    op.drop_index("ix_market_sessions_calendar_window", table_name="market_sessions")
    op.drop_table("market_sessions")
    op.drop_table("market_calendars")
