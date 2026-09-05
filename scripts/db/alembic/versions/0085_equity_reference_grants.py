"""Grant the market-data role write access to corporate_actions.

Migration 0084 created the equity reference tables without service-role
grants; the scheduled-daily backfill's corporate-action loading (which runs
as ``vm_market_data_login``) fail-closed on InsufficientPrivilege the first
time it was exercised end-to-end. ``index_membership`` and
``earnings_events`` stay owner-only: they are written by the research
acquisition path, not by a runtime service role.
"""

from __future__ import annotations

from alembic import op

revision = "0085_equity_reference_grants"
down_revision = "0084_equity_reference_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE public.corporate_actions TO vm_market_data")
        op.execute(
            "GRANT USAGE, SELECT ON SEQUENCE public.corporate_actions_action_id_seq "
            "TO vm_market_data"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "REVOKE USAGE, SELECT ON SEQUENCE public.corporate_actions_action_id_seq "
            "FROM vm_market_data"
        )
        op.execute(
            "REVOKE SELECT, INSERT, UPDATE ON TABLE public.corporate_actions FROM vm_market_data"
        )
