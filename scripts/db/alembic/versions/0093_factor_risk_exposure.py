"""Register separate point-in-time portfolio factor-risk observations.

Revision ID: 0093_factor_risk_exposure
Revises: 0092_panel_owner_fence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093_factor_risk_exposure"
down_revision = "0092_panel_owner_fence"
branch_labels = None
depends_on = None

_PREVIOUS_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release')"
)
_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure')"
)


def upgrade() -> None:
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            _OBSERVATION_KIND_CHECK,
        )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM equity_observations "
            "WHERE observation_kind = 'factor_risk_exposure'"
        )
    )
    if int(count or 0):
        message = "Cannot remove factor-risk contract after immutable evidence exists"
        raise RuntimeError(message)
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_equity_observation_kind",
            _PREVIOUS_OBSERVATION_KIND_CHECK,
        )


__all__ = ["downgrade", "upgrade"]
