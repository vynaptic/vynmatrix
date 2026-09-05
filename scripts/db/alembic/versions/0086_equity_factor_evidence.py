"""Add immutable point-in-time equity observation, factor, and rank evidence.

Revision ID: 0086_equity_factor_evidence
Revises: 0085_equity_reference_grants
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086_equity_factor_evidence"
down_revision = "0085_equity_reference_grants"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = (
    "equity_source_lineages",
    "equity_observations",
    "equity_observation_values",
    "equity_factor_snapshots",
    "equity_factor_snapshot_details",
    "equity_factor_evidence",
    "equity_rank_snapshots",
    "equity_rank_snapshot_rows",
)
_MARKET_DATA_TABLES = _IMMUTABLE_TABLES[:6]
_RANK_TABLES = _IMMUTABLE_TABLES[6:]
_SEALED_AGGREGATES = {
    "equity_observation_values": (
        "equity_observations",
        "observation_id",
        "observation_id",
    ),
    "equity_factor_snapshot_details": (
        "equity_factor_snapshots",
        "factor_snapshot_id",
        "factor_snapshot_id",
    ),
    "equity_factor_evidence": (
        "equity_factor_snapshots",
        "factor_snapshot_id",
        "factor_snapshot_id",
    ),
    "equity_rank_snapshot_rows": (
        "equity_rank_snapshots",
        "rank_snapshot_id",
        "rank_snapshot_id",
    ),
}


def _enable_postgresql_evidence_access() -> None:
    for role in ("vm_indicator", "vm_scoring"):
        op.execute(
            "GRANT SELECT ON TABLE "
            + ", ".join(f"public.{table}" for table in _IMMUTABLE_TABLES)
            + f" TO {role}"
        )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE "
        + ", ".join(f"public.{table}" for table in _MARKET_DATA_TABLES)
        + " TO vm_market_data"
    )
    op.execute(
        "GRANT INSERT ON TABLE "
        + ", ".join(f"public.{table}" for table in _RANK_TABLES)
        + " TO vm_indicator"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.strategies, public.strategy_versions "
        "TO vm_market_data, vm_indicator"
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_reject_immutable_equity_evidence_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_require_append_only_parent_current_xact()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            parent_identity text;
            parent_is_current boolean;
        BEGIN
            IF TG_NARGS <> 3 THEN
                RAISE EXCEPTION 'invalid append-only aggregate trigger configuration'
                    USING ERRCODE = '55000';
            END IF;
            parent_identity := to_jsonb(NEW) ->> TG_ARGV[2];
            IF parent_identity IS NULL THEN
                RAISE EXCEPTION '% append-only parent identity is missing', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            EXECUTE format(
                'SELECT xmin = pg_current_xact_id()::xid '
                'FROM %I.%I WHERE %I::text = $1',
                TG_TABLE_SCHEMA,
                TG_ARGV[0],
                TG_ARGV[1]
            )
            INTO parent_is_current
            USING parent_identity;
            IF parent_is_current IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION
                    '% must be inserted in the same transaction as its % parent',
                    TG_TABLE_NAME,
                    TG_ARGV[0]
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable "
            f"BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "public.vm_reject_immutable_equity_evidence_mutation()"
        )
    for child, (parent, parent_key, child_key) in _SEALED_AGGREGATES.items():
        op.execute(
            f"CREATE TRIGGER trg_{child}_aggregate_sealed "
            f"BEFORE INSERT ON public.{child} FOR EACH ROW "
            "EXECUTE FUNCTION public.vm_require_append_only_parent_current_xact"
            f"('{parent}', '{parent_key}', '{child_key}')"
        )
    _create_postgresql_aggregate_completeness()


def _create_postgresql_aggregate_completeness() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_assert_equity_factor_snapshot_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            detail_count bigint;
            enabled_count bigint;
            available_count bigint;
            complete_without_evidence boolean;
        BEGIN
            EXECUTE format(
                'SELECT count(*), count(*) FILTER (WHERE enabled), '
                'count(*) FILTER (WHERE enabled AND state = ''complete'') '
                'FROM %I.equity_factor_snapshot_details '
                'WHERE factor_snapshot_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO detail_count, enabled_count, available_count
            USING NEW.factor_snapshot_id;
            EXECUTE format(
                'SELECT EXISTS ('
                'SELECT 1 FROM %I.equity_factor_snapshot_details AS detail '
                'WHERE detail.factor_snapshot_id = $1 '
                'AND detail.enabled AND detail.state = ''complete'' '
                'AND NOT EXISTS ('
                'SELECT 1 FROM %I.equity_factor_evidence AS evidence '
                'WHERE evidence.factor_snapshot_id = detail.factor_snapshot_id '
                'AND evidence.factor_name = detail.factor_name))',
                TG_TABLE_SCHEMA,
                TG_TABLE_SCHEMA
            )
            INTO complete_without_evidence
            USING NEW.factor_snapshot_id;
            IF detail_count = 0
                OR enabled_count = 0
                OR enabled_count <> NEW.expected_factor_count
                OR available_count <> NEW.available_factor_count
                OR complete_without_evidence THEN
                RAISE EXCEPTION
                    'equity factor snapshot % has incomplete aggregate children',
                    NEW.factor_snapshot_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_equity_factor_snapshot_complete "
        "AFTER INSERT ON public.equity_factor_snapshots "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_factor_snapshot_complete()"
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_assert_equity_rank_snapshot_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            row_count bigint;
            included_count bigint;
            excluded_count bigint;
            minimum_ordinal integer;
            maximum_ordinal integer;
        BEGIN
            EXECUTE format(
                'SELECT count(*), count(*) FILTER (WHERE eligible), '
                'count(*) FILTER (WHERE NOT eligible), '
                'min(row_ordinal), max(row_ordinal) '
                'FROM %I.equity_rank_snapshot_rows '
                'WHERE rank_snapshot_id = $1',
                TG_TABLE_SCHEMA
            )
            INTO row_count, included_count, excluded_count,
                minimum_ordinal, maximum_ordinal
            USING NEW.rank_snapshot_id;
            IF row_count <> NEW.expected_instrument_count
                OR included_count <> NEW.included_instrument_count
                OR excluded_count <> NEW.excluded_instrument_count
                OR (row_count > 0 AND (
                    minimum_ordinal <> 0 OR maximum_ordinal <> row_count - 1
                )) THEN
                RAISE EXCEPTION
                    'equity rank snapshot % has incomplete aggregate children',
                    NEW.rank_snapshot_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_equity_rank_snapshot_complete "
        "AFTER INSERT ON public.equity_rank_snapshots "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_assert_equity_rank_snapshot_complete()"
    )


def _create_source_lineage() -> None:
    op.create_table(
        "equity_source_lineages",
        sa.Column("lineage_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("product", sa.String(length=150), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.String(length=100), nullable=False),
        sa.Column("tool_version", sa.String(length=100), nullable=False),
        sa.Column("source_identity", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.String(length=100), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timestamp_semantics", sa.JSON(), nullable=False),
        sa.Column("adjustment_policy", sa.String(length=100), nullable=False),
        sa.Column("entitlement_scope", sa.String(length=200), nullable=False),
        sa.Column("missing_data_policy", sa.String(length=100), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(lineage_id) = 64 AND length(content_sha256) = 64",
            name="ck_equity_source_digests",
        ),
        sa.CheckConstraint(
            "length(trim(provider)) > 0 AND length(trim(product)) > 0 "
            "AND length(trim(endpoint)) > 0 AND length(trim(dataset_version)) > 0 "
            "AND length(trim(tool_version)) > 0 AND length(trim(source_identity)) > 0 "
            "AND length(trim(source_revision)) > 0",
            name="ck_equity_source_identity",
        ),
        sa.CheckConstraint(
            "length(trim(adjustment_policy)) > 0 "
            "AND length(trim(entitlement_scope)) > 0 "
            "AND length(trim(missing_data_policy)) > 0",
            name="ck_equity_source_policies",
        ),
        sa.PrimaryKeyConstraint("lineage_id"),
    )
    op.create_index(
        "ix_equity_source_provider_product",
        "equity_source_lineages",
        ["provider", "product"],
    )
    op.create_index(
        "ix_equity_source_retrieved_at",
        "equity_source_lineages",
        ["retrieved_at"],
    )


def _create_observations() -> None:
    op.create_table(
        "equity_observations",
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("lineage_id", sa.String(length=64), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=True),
        sa.Column("observation_kind", sa.String(length=30), nullable=False),
        sa.Column("source_record_identity", sa.Text(), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_observation_id", sa.String(length=64), nullable=True),
        sa.Column("accession_number", sa.String(length=32), nullable=True),
        sa.Column("filing_form", sa.String(length=20), nullable=True),
        sa.Column("sic_code", sa.String(length=10), nullable=True),
        sa.Column("disposition", sa.String(length=20), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "observation_kind IN ('price', 'corporate_action', 'membership', "
            "'filing', 'xbrl_fact', 'benchmark', 'calendar', "
            "'earnings_event', 'market_cap')",
            name="ck_equity_observation_kind",
        ),
        sa.CheckConstraint(
            "disposition IN ('observed', 'missing', 'excluded', 'unavailable', "
            "'partial', 'failed')",
            name="ck_equity_observation_disposition",
        ),
        sa.CheckConstraint(
            "disposition <> 'observed' OR available_at IS NOT NULL",
            name="ck_equity_observed_availability",
        ),
        sa.CheckConstraint("revision > 0", name="ck_equity_observation_revision"),
        sa.CheckConstraint(
            "length(observation_id) = 64 AND length(content_sha256) = 64",
            name="ck_equity_observation_digests",
        ),
        sa.CheckConstraint(
            "length(trim(source_record_identity)) > 0",
            name="ck_equity_observation_source_identity",
        ),
        sa.CheckConstraint(
            "supersedes_observation_id IS NULL OR supersedes_observation_id <> observation_id",
            name="ck_equity_observation_supersedes_other",
        ),
        sa.CheckConstraint(
            "observation_kind NOT IN ('filing', 'xbrl_fact') "
            "OR (accession_number IS NOT NULL AND length(trim(accession_number)) > 0 "
            "AND filing_form IS NOT NULL AND length(trim(filing_form)) > 0)",
            name="ck_equity_sec_observation_filing_identity",
        ),
        sa.ForeignKeyConstraint(
            ["instr_id"],
            ["instruments.instr_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lineage_id"],
            ["equity_source_lineages.lineage_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_observation_id"],
            ["equity_observations.observation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "lineage_id",
            "source_record_identity",
            "revision",
            name="uq_equity_observation_source_revision",
        ),
    )
    op.create_index(
        "ix_equity_observation_instr_available",
        "equity_observations",
        ["instr_id", "observation_kind", "available_at"],
    )
    op.create_index(
        "ix_equity_observation_kind_event",
        "equity_observations",
        ["observation_kind", "event_at"],
    )
    op.create_index(
        "ix_equity_observation_lineage",
        "equity_observations",
        ["lineage_id"],
    )

    op.create_table(
        "equity_observation_values",
        sa.Column("value_id", sa.String(length=64), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("decimal_value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("integer_value", sa.BigInteger(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("date_value", sa.Date(), nullable=True),
        sa.Column("timestamp_value", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("context_identity", sa.String(length=200), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=20), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "length(value_id) = 64",
            name="ck_equity_observation_value_id",
        ),
        sa.CheckConstraint(
            "length(trim(field_name)) > 0 AND ordinal >= 0",
            name="ck_equity_observation_value_key",
        ),
        sa.CheckConstraint(
            "(value_type = 'decimal' AND decimal_value IS NOT NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'integer' AND decimal_value IS NULL "
            "AND integer_value IS NOT NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'text' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NOT NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'boolean' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NOT NULL AND date_value IS NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'date' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NOT NULL "
            "AND timestamp_value IS NULL) OR "
            "(value_type = 'timestamp' AND decimal_value IS NULL "
            "AND integer_value IS NULL AND text_value IS NULL "
            "AND boolean_value IS NULL AND date_value IS NULL "
            "AND timestamp_value IS NOT NULL)",
            name="ck_equity_observation_typed_value",
        ),
        sa.CheckConstraint(
            "unit IS NULL OR length(trim(unit)) > 0",
            name="ck_equity_observation_unit",
        ),
        sa.CheckConstraint(
            "fiscal_year IS NULL OR fiscal_year BETWEEN 1800 AND 3000",
            name="ck_equity_observation_fiscal_year",
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="ck_equity_observation_value_period",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["equity_observations.observation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("value_id"),
        sa.UniqueConstraint(
            "observation_id",
            "field_name",
            "ordinal",
            name="uq_equity_observation_value",
        ),
    )
    op.create_index(
        "ix_equity_observation_value_observation",
        "equity_observation_values",
        ["observation_id"],
    )


def _create_factor_snapshots() -> None:
    op.create_table(
        "equity_factor_snapshots",
        sa.Column("factor_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strat_ver_id", sa.BigInteger(), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("effective_session", sa.Date(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculation_version", sa.String(length=100), nullable=False),
        sa.Column("configuration_digest", sa.String(length=64), nullable=False),
        sa.Column("peer_taxonomy_version", sa.String(length=100), nullable=False),
        sa.Column("peer_group", sa.String(length=150), nullable=False),
        sa.Column("completeness_status", sa.String(length=20), nullable=False),
        sa.Column("expected_factor_count", sa.Integer(), nullable=False),
        sa.Column("available_factor_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(factor_snapshot_id) = 64 AND length(configuration_digest) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_equity_factor_digests",
        ),
        sa.CheckConstraint(
            "factor_snapshot_id = content_sha256",
            name="ck_equity_factor_content_identity",
        ),
        sa.CheckConstraint(
            "completeness_status IN ('complete', 'incomplete', 'ineligible')",
            name="ck_equity_factor_completeness",
        ),
        sa.CheckConstraint(
            "expected_factor_count >= 0 AND available_factor_count >= 0 "
            "AND available_factor_count <= expected_factor_count "
            "AND (completeness_status <> 'complete' "
            "OR available_factor_count = expected_factor_count)",
            name="ck_equity_factor_counts",
        ),
        sa.CheckConstraint(
            "length(trim(calculation_version)) > 0 "
            "AND length(trim(peer_taxonomy_version)) > 0 "
            "AND length(trim(peer_group)) > 0",
            name="ck_equity_factor_identity",
        ),
        sa.ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_equity_factor_strategy_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instr_id"],
            ["instruments.instr_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("factor_snapshot_id"),
        sa.UniqueConstraint(
            "factor_snapshot_id",
            "instr_id",
            name="uq_equity_factor_snapshot_instrument",
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "instr_id",
            "effective_session",
            "cutoff_at",
            name="uq_equity_factor_snapshot_cutoff",
        ),
    )
    op.create_index(
        "ix_equity_factor_strategy_session",
        "equity_factor_snapshots",
        ["strategy_id", "effective_session"],
    )
    op.create_index(
        "ix_equity_factor_instr_cutoff",
        "equity_factor_snapshots",
        ["instr_id", "cutoff_at"],
    )

    op.create_table(
        "equity_factor_snapshot_details",
        sa.Column("factor_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("factor_name", sa.String(length=100), nullable=False),
        sa.Column("sleeve_name", sa.String(length=100), nullable=False),
        sa.Column("factor_version", sa.String(length=100), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("raw_value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("peer_group", sa.String(length=150), nullable=True),
        sa.Column("peer_count", sa.Integer(), nullable=True),
        sa.Column("peer_center", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("peer_scale", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("peer_scale_method", sa.String(length=40), nullable=True),
        sa.Column(
            "unbounded_normalized_value",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
        sa.Column("normalized_value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("factor_rank", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("weight", sa.Numeric(precision=18, scale=12), nullable=False),
        sa.Column("contribution", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("missing_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "length(trim(factor_name)) > 0 AND length(trim(sleeve_name)) > 0 "
            "AND length(trim(factor_version)) > 0",
            name="ck_equity_factor_detail_identity",
        ),
        sa.CheckConstraint(
            "direction IN ('higher_is_better', 'lower_is_better')",
            name="ck_equity_factor_detail_direction",
        ),
        sa.CheckConstraint(
            "state IN ('complete', 'disabled', 'missing', 'stale', 'ambiguous', "
            "'ineligible', 'insufficient_peers')",
            name="ck_equity_factor_detail_state",
        ),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_equity_factor_detail_weight",
        ),
        sa.CheckConstraint(
            "(enabled AND state <> 'disabled') OR "
            "(NOT enabled AND state = 'disabled' AND weight = 0 "
            "AND raw_value IS NULL AND peer_group IS NULL AND peer_count IS NULL "
            "AND peer_center IS NULL AND peer_scale IS NULL "
            "AND peer_scale_method IS NULL AND unbounded_normalized_value IS NULL "
            "AND normalized_value IS NULL AND factor_rank IS NULL "
            "AND contribution IS NULL)",
            name="ck_equity_disabled_factor_zero",
        ),
        sa.CheckConstraint(
            "state <> 'complete' OR (enabled AND raw_value IS NOT NULL "
            "AND peer_group IS NOT NULL AND peer_count IS NOT NULL AND peer_count > 0 "
            "AND peer_center IS NOT NULL AND peer_scale IS NOT NULL AND peer_scale >= 0 "
            "AND peer_scale_method IS NOT NULL "
            "AND unbounded_normalized_value IS NOT NULL "
            "AND normalized_value IS NOT NULL "
            "AND factor_rank IS NOT NULL AND factor_rank >= 1 "
            "AND contribution IS NOT NULL)",
            name="ck_equity_complete_factor_values",
        ),
        sa.CheckConstraint(
            "peer_scale_method IS NULL OR peer_scale_method IN "
            "('constant', 'median_absolute_deviation', 'standard_deviation_fallback')",
            name="ck_equity_factor_scale_method",
        ),
        sa.CheckConstraint(
            "contribution IS NULL OR abs(contribution - weight * normalized_value) "
            "<= 0.000000000001",
            name="ck_equity_factor_contribution_arithmetic",
        ),
        sa.CheckConstraint(
            "(state IN ('complete', 'disabled') AND missing_reason IS NULL) OR "
            "(state NOT IN ('complete', 'disabled') "
            "AND missing_reason IS NOT NULL AND length(trim(missing_reason)) > 0)",
            name="ck_equity_factor_missing_reason",
        ),
        sa.ForeignKeyConstraint(
            ["factor_snapshot_id"],
            ["equity_factor_snapshots.factor_snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("factor_snapshot_id", "factor_name"),
    )
    op.create_index(
        "ix_equity_factor_detail_sleeve",
        "equity_factor_snapshot_details",
        ["sleeve_name", "state"],
    )

    op.create_table(
        "equity_factor_evidence",
        sa.Column("factor_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("factor_name", sa.String(length=100), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_role", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "evidence_role IN ('primary', 'context', 'peer', 'benchmark', "
            "'calendar', 'membership')",
            name="ck_equity_factor_evidence_role",
        ),
        sa.ForeignKeyConstraint(
            ["factor_snapshot_id", "factor_name"],
            [
                "equity_factor_snapshot_details.factor_snapshot_id",
                "equity_factor_snapshot_details.factor_name",
            ],
            name="fk_equity_factor_evidence_detail",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["equity_observations.observation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("factor_snapshot_id", "factor_name", "observation_id"),
    )
    op.create_index(
        "ix_equity_factor_evidence_observation",
        "equity_factor_evidence",
        ["observation_id"],
    )


def _create_rank_snapshots() -> None:
    op.create_table(
        "equity_rank_snapshots",
        sa.Column("rank_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=50), nullable=False),
        sa.Column("strat_ver_id", sa.BigInteger(), nullable=False),
        sa.Column("effective_session", sa.Date(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configuration_digest", sa.String(length=64), nullable=False),
        sa.Column("panel_revision_digest", sa.String(length=64), nullable=False),
        sa.Column("factor_content_digest", sa.String(length=64), nullable=False),
        sa.Column("data_use_scope", sa.String(length=32), nullable=False),
        sa.Column("provider_authority_digest", sa.String(length=64), nullable=False),
        sa.Column("provider_authority_policy", sa.JSON(), nullable=False),
        sa.Column("peer_taxonomy_version", sa.String(length=100), nullable=False),
        sa.Column("completeness_status", sa.String(length=20), nullable=False),
        sa.Column("expected_instrument_count", sa.Integer(), nullable=False),
        sa.Column("included_instrument_count", sa.Integer(), nullable=False),
        sa.Column("excluded_instrument_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(rank_snapshot_id) = 64 "
            "AND length(configuration_digest) = 64 "
            "AND length(panel_revision_digest) = 64 "
            "AND length(factor_content_digest) = 64 "
            "AND length(provider_authority_digest) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_equity_rank_digests",
        ),
        sa.CheckConstraint(
            "rank_snapshot_id = content_sha256",
            name="ck_equity_rank_content_identity",
        ),
        sa.CheckConstraint(
            "data_use_scope IN ('historical_validation', 'paper_forward', 'live_forward')",
            name="ck_equity_rank_data_use_scope",
        ),
        sa.CheckConstraint(
            "completeness_status IN ('complete', 'incomplete', 'ineligible')",
            name="ck_equity_rank_completeness",
        ),
        sa.CheckConstraint(
            "expected_instrument_count >= 0 AND included_instrument_count >= 0 "
            "AND excluded_instrument_count >= 0 "
            "AND included_instrument_count + excluded_instrument_count "
            "= expected_instrument_count",
            name="ck_equity_rank_counts",
        ),
        sa.CheckConstraint(
            "length(trim(peer_taxonomy_version)) > 0",
            name="ck_equity_rank_peer_taxonomy",
        ),
        sa.ForeignKeyConstraint(
            ["strat_ver_id", "strategy_id"],
            ["strategy_versions.strat_ver_id", "strategy_versions.strategy_id"],
            name="fk_equity_rank_strategy_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("rank_snapshot_id"),
        sa.UniqueConstraint(
            "rank_snapshot_id",
            "strategy_id",
            "strat_ver_id",
            name="uq_equity_rank_strategy_version",
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strat_ver_id",
            "effective_session",
            "cutoff_at",
            "configuration_digest",
            "factor_content_digest",
            "data_use_scope",
            "provider_authority_digest",
            name="uq_equity_rank_factor_lineage",
        ),
    )
    op.create_index(
        "ix_equity_rank_strategy_session",
        "equity_rank_snapshots",
        ["strategy_id", "effective_session"],
    )

    op.create_table(
        "equity_rank_snapshot_rows",
        sa.Column("rank_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("instr_id", sa.Integer(), nullable=False),
        sa.Column("factor_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("strategy_eligible", sa.Boolean(), nullable=False),
        sa.Column("row_ordinal", sa.Integer(), nullable=False),
        sa.Column("rank_position", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("composite_score", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column(
            "target_allocation_hint",
            sa.Numeric(precision=18, scale=12),
            nullable=True,
        ),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("incumbent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("exclusion_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "decision IN ('selected', 'hold', 'eligible', 'excluded', 'exit')",
            name="ck_equity_rank_row_decision",
        ),
        sa.CheckConstraint(
            "row_ordinal >= 0",
            name="ck_equity_rank_row_ordinal",
        ),
        sa.CheckConstraint(
            "(eligible AND factor_snapshot_id IS NOT NULL "
            "AND rank_position IS NOT NULL AND rank_position > 0 "
            "AND composite_score IS NOT NULL) OR "
            "(NOT eligible AND factor_snapshot_id IS NULL AND rank_position IS NULL "
            "AND composite_score IS NULL)",
            name="ck_equity_rank_row_eligibility",
        ),
        sa.CheckConstraint(
            "(strategy_eligible AND eligible "
            "AND decision IN ('selected', 'hold', 'eligible') "
            "AND exclusion_reason IS NULL) OR "
            "(NOT strategy_eligible AND decision IN ('excluded', 'exit') "
            "AND target_allocation_hint IS NULL "
            "AND exclusion_reason IS NOT NULL)",
            name="ck_equity_rank_row_strategy_eligibility",
        ),
        sa.CheckConstraint(
            "target_allocation_hint IS NULL OR "
            "(target_allocation_hint >= 0 AND target_allocation_hint <= 1)",
            name="ck_equity_rank_row_allocation",
        ),
        sa.ForeignKeyConstraint(
            ["factor_snapshot_id", "instr_id"],
            [
                "equity_factor_snapshots.factor_snapshot_id",
                "equity_factor_snapshots.instr_id",
            ],
            name="fk_equity_rank_row_factor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instr_id"],
            ["instruments.instr_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rank_snapshot_id"],
            ["equity_rank_snapshots.rank_snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("rank_snapshot_id", "instr_id"),
        sa.UniqueConstraint(
            "rank_snapshot_id",
            "row_ordinal",
            name="uq_equity_rank_row_ordinal",
        ),
        sa.UniqueConstraint(
            "rank_snapshot_id",
            "instr_id",
            "factor_snapshot_id",
            name="uq_equity_rank_row_factor",
        ),
    )
    op.create_index(
        "ix_equity_rank_row_factor",
        "equity_rank_snapshot_rows",
        ["factor_snapshot_id"],
    )
    op.create_index(
        "ix_equity_rank_row_instrument",
        "equity_rank_snapshot_rows",
        ["instr_id"],
    )
    op.create_index(
        "ix_equity_rank_row_position",
        "equity_rank_snapshot_rows",
        ["rank_snapshot_id", "rank_position", "row_ordinal"],
    )


def upgrade() -> None:
    _create_source_lineage()
    _create_observations()
    _create_factor_snapshots()
    _create_rank_snapshots()
    if op.get_bind().dialect.name == "postgresql":
        _enable_postgresql_evidence_access()


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        table: int(bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in _IMMUTABLE_TABLES
    }
    if any(retained.values()):
        details = ", ".join(f"{table}={count}" for table, count in retained.items())
        msg = f"Cannot remove immutable equity evidence while rows exist: {details}"
        raise RuntimeError(msg)

    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_equity_rank_snapshot_complete "
            "ON public.equity_rank_snapshots"
        )
        op.execute("DROP FUNCTION IF EXISTS public.vm_assert_equity_rank_snapshot_complete()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_equity_factor_snapshot_complete "
            "ON public.equity_factor_snapshots"
        )
        op.execute("DROP FUNCTION IF EXISTS public.vm_assert_equity_factor_snapshot_complete()")
        for table in reversed(tuple(_SEALED_AGGREGATES)):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_aggregate_sealed ON public.{table}")
        for table in reversed(_IMMUTABLE_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON public.{table}")
        op.execute("DROP FUNCTION IF EXISTS public.vm_require_append_only_parent_current_xact()")
        op.execute("DROP FUNCTION IF EXISTS public.vm_reject_immutable_equity_evidence_mutation()")
        for role in ("vm_market_data", "vm_indicator", "vm_scoring"):
            for table in _IMMUTABLE_TABLES:
                op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM {role}")
        op.execute(
            "REVOKE SELECT ON TABLE public.strategies, public.strategy_versions "
            "FROM vm_market_data, vm_indicator"
        )

    op.drop_index("ix_equity_rank_row_position", table_name="equity_rank_snapshot_rows")
    op.drop_index("ix_equity_rank_row_instrument", table_name="equity_rank_snapshot_rows")
    op.drop_index("ix_equity_rank_row_factor", table_name="equity_rank_snapshot_rows")
    op.drop_table("equity_rank_snapshot_rows")
    op.drop_index("ix_equity_rank_strategy_session", table_name="equity_rank_snapshots")
    op.drop_table("equity_rank_snapshots")
    op.drop_index(
        "ix_equity_factor_evidence_observation",
        table_name="equity_factor_evidence",
    )
    op.drop_table("equity_factor_evidence")
    op.drop_index(
        "ix_equity_factor_detail_sleeve",
        table_name="equity_factor_snapshot_details",
    )
    op.drop_table("equity_factor_snapshot_details")
    op.drop_index("ix_equity_factor_instr_cutoff", table_name="equity_factor_snapshots")
    op.drop_index(
        "ix_equity_factor_strategy_session",
        table_name="equity_factor_snapshots",
    )
    op.drop_table("equity_factor_snapshots")
    op.drop_index(
        "ix_equity_observation_value_observation",
        table_name="equity_observation_values",
    )
    op.drop_table("equity_observation_values")
    op.drop_index("ix_equity_observation_lineage", table_name="equity_observations")
    op.drop_index("ix_equity_observation_kind_event", table_name="equity_observations")
    op.drop_index("ix_equity_observation_instr_available", table_name="equity_observations")
    op.drop_table("equity_observations")
    op.drop_index("ix_equity_source_retrieved_at", table_name="equity_source_lineages")
    op.drop_index("ix_equity_source_provider_product", table_name="equity_source_lineages")
    op.drop_table("equity_source_lineages")
