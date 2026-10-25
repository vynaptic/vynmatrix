"""Add the filing_coverage observation kind for the SEC filing event gate.

Revision ID: 0112_sec_filing_gate_storage
Revises: 0111_adverse_news_gate_storage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0112_sec_filing_gate_storage"
down_revision = "0111_adverse_news_gate_storage"
branch_labels = None
depends_on = None

_PREVIOUS_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure', 'news_coverage')"
)
_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure', 'news_coverage', 'filing_coverage')"
)


def _swap_observation_kind_check(check: str) -> None:
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint("ck_equity_observation_kind", check)


def upgrade() -> None:
    _swap_observation_kind_check(_OBSERVATION_KIND_CHECK)


def downgrade() -> None:
    coverage_count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM equity_observations WHERE observation_kind = 'filing_coverage'"
        )
    )
    if int(coverage_count or 0):
        message = "Cannot remove SEC filing gate storage after evidence exists"
        raise RuntimeError(message)
    _swap_observation_kind_check(_PREVIOUS_OBSERVATION_KIND_CHECK)


__all__ = ["downgrade", "upgrade"]
