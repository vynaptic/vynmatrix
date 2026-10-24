"""Add the news_coverage observation kind and model inference records.

Revision ID: 0111_adverse_news_gate_storage
Revises: 0110_asset_class_binding_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0111_adverse_news_gate_storage"
down_revision = "0110_asset_class_binding_scope"
branch_labels = None
depends_on = None

_TABLE = "equity_model_inference_records"
_PREVIOUS_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure')"
)
_OBSERVATION_KIND_CHECK = (
    "observation_kind IN ('price', 'corporate_action', 'membership', "
    "'filing', 'xbrl_fact', 'benchmark', 'calendar', 'earnings_event', "
    "'market_cap', 'security_identity', 'analyst_estimate', 'news_event', "
    "'call_transcript', 'insider_transaction', 'positioning', 'macro_release', "
    "'factor_risk_exposure', 'news_coverage')"
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _swap_observation_kind_check(check: str) -> None:
    with op.batch_alter_table("equity_observations") as batch_op:
        batch_op.drop_constraint("ck_equity_observation_kind", type_="check")
        batch_op.create_check_constraint("ck_equity_observation_kind", check)


def upgrade() -> None:
    _swap_observation_kind_check(_OBSERVATION_KIND_CHECK)
    op.create_table(
        _TABLE,
        sa.Column("model_input_sha256", sa.String(length=64), primary_key=True),
        sa.Column("lineage_id", sa.String(length=64), nullable=False),
        sa.Column("model_output_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("request_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("response_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entitlement_scope", sa.String(length=100), nullable=False),
        sa.Column("entitlement_owner_user_id", sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(
            ["lineage_id"],
            ["equity_source_lineages.lineage_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_owner_user_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(model_input_sha256) = 64",
            name="ck_equity_model_inference_input_digest",
        ),
        sa.CheckConstraint(
            "length(model_output_sha256) = 64",
            name="ck_equity_model_inference_output_digest",
        ),
    )
    op.create_index("ix_equity_model_inference_lineage", _TABLE, ["lineage_id"])
    if _is_postgresql():
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{_TABLE} TO vm_market_data")
        op.execute(
            f"CREATE TRIGGER trg_{_TABLE}_immutable "
            f"BEFORE UPDATE OR DELETE ON public.{_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "public.vm_reject_immutable_equity_evidence_mutation()"
        )


def downgrade() -> None:
    connection = op.get_bind()
    coverage_count = connection.scalar(
        sa.text("SELECT count(*) FROM equity_observations WHERE observation_kind = 'news_coverage'")
    )
    inference_count = connection.scalar(sa.text(f"SELECT count(*) FROM {_TABLE}"))
    if int(coverage_count or 0) or int(inference_count or 0):
        message = "Cannot remove adverse-news gate storage after evidence exists"
        raise RuntimeError(message)

    if _is_postgresql():
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_immutable ON public.{_TABLE}")
        op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM vm_market_data")
    op.drop_index("ix_equity_model_inference_lineage", table_name=_TABLE)
    op.drop_table(_TABLE)
    _swap_observation_kind_check(_PREVIOUS_OBSERVATION_KIND_CHECK)


__all__ = ["downgrade", "upgrade"]
