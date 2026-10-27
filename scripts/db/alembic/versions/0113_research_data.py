"""Add owner-scoped immutable research evidence, never runtime market writes.

Revision ID: 0113_research_data
Revises: 0112_sec_filing_gate_storage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0113_research_data"
down_revision = "0112_sec_filing_gate_storage"
branch_labels = None
depends_on = None

TABLES = ("research_imports", "research_datasets", "research_bars")
READ_COLUMNS = {
    "prices": (
        "price_id",
        "instr_id",
        "source",
        "timeframe",
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "content_revision",
    ),
    "instruments": (
        "instr_id",
        "canonical",
        "asset_class",
        "exchange",
        "settlement_currency",
        "tick_size",
        "lot_size",
        "is_tradable",
        "market_session_policy",
        "market_calendar_id",
    ),
    "market_calendars": (
        "calendar_id",
        "code",
        "source_kind",
        "provider",
        "source_reference",
        "observation_id",
        "coverage_start",
        "coverage_end",
        "observed_at",
    ),
    "market_sessions": ("session_id", "calendar_id", "opens_at", "closes_at"),
}
# Only these columns were newly made readable; downgrade must not remove old grants.
NEW_READ_COLUMNS = {
    "prices": (
        "price_id",
        "instr_id",
        "source",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "content_revision",
    ),
    "instruments": ("is_tradable", "market_session_policy", "market_calendar_id"),
    "market_calendars": READ_COLUMNS["market_calendars"],
    "market_sessions": READ_COLUMNS["market_sessions"],
}


def _schema() -> None:
    op.create_table(
        "research_imports",
        sa.Column("import_id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(50),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("original_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("original_csv", sa.LargeBinary(), nullable=False),
        sa.Column("canonical_bars", sa.JSON(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("owner_id", "import_id", name="uq_research_import_owner"),
        sa.UniqueConstraint("owner_id", "request_hash", name="uq_research_import_request"),
        sa.CheckConstraint("stored_bytes >= 0", name="ck_research_import_bytes"),
        sa.CheckConstraint(
            "(valid AND content_hash IS NOT NULL) OR (NOT valid AND content_hash IS NULL)",
            name="ck_research_import_valid_hash",
        ),
    )
    op.create_index(
        "ix_research_import_owner_created",
        "research_imports",
        ["owner_id", "created_at", "import_id"],
    )
    op.create_table(
        "research_datasets",
        sa.Column("dataset_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(50), nullable=False),
        sa.Column("import_id", sa.String(36), nullable=False),
        sa.Column("supersedes_id", sa.String(36)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("original_hash", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("freeze_xid", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bar_count", sa.Integer(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("owner_id", "dataset_id", name="uq_research_dataset_owner"),
        sa.UniqueConstraint("owner_id", "import_id", name="uq_research_dataset_import"),
        sa.ForeignKeyConstraint(
            ["owner_id", "import_id"],
            ["research_imports.owner_id", "research_imports.import_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "supersedes_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "stored_bytes >= 0 AND bar_count BETWEEN 1 AND 100000",
            name="ck_research_dataset_bounds",
        ),
    )
    op.create_index(
        "ix_research_dataset_owner_created",
        "research_datasets",
        ["owner_id", "created_at", "dataset_id"],
    )
    op.create_table(
        "research_bars",
        sa.Column("owner_id", sa.String(50), primary_key=True),
        sa.Column("dataset_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id", "dataset_id"],
            ["research_datasets.owner_id", "research_datasets.dataset_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_id", "dataset_id", "ts", name="uq_research_bar_timestamp"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 99999", name="ck_research_bar_ordinal"),
    )


def _postgres_guards() -> None:
    op.execute("""
        CREATE FUNCTION public.vm_research_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN RAISE EXCEPTION 'Research evidence is immutable'; END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_data_admission() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE used_bytes bigint; bars_bytes bigint; expected_manifest jsonb;
        BEGIN
            PERFORM pg_advisory_xact_lock(177013, hashtext(NEW.owner_id));
            IF TG_TABLE_NAME = 'research_imports' THEN
                IF octet_length(NEW.original_csv) > 10485760
                   OR json_array_length(NEW.canonical_bars) > 100000 THEN
                    RAISE EXCEPTION 'Research import size limit exceeded';
                END IF;
                NEW.stored_bytes := octet_length(NEW.original_csv)
                    + octet_length(NEW.canonical_bars::text)
                    + octet_length(NEW.metadata_json::text)
                    + octet_length(NEW.report_json::text);
            ELSE
                NEW.freeze_xid := txid_current();
                SELECT octet_length(i.canonical_bars::text), jsonb_build_object(
                    'schema', 'research-dataset-v1',
                    'content_hash', i.content_hash, 'original_hash', i.original_hash,
                    'metadata', i.metadata_json, 'validation', i.report_json,
                    'eligibility', i.report_json -> 'eligibility',
                    'bar_count', json_array_length(i.canonical_bars),
                    'first_ts', i.canonical_bars -> 0 ->> 'ts',
                    'last_ts', i.canonical_bars ->
                        (json_array_length(i.canonical_bars) - 1) ->> 'ts',
                    'price_revisions', coalesce(
                        i.metadata_json -> 'snapshot' -> 'price_revisions', '[]'::json),
                    'original_recorded_at', NEW.manifest_json -> 'original_recorded_at'
                ) INTO bars_bytes, expected_manifest
                    FROM public.research_imports i
                    WHERE i.owner_id = NEW.owner_id AND i.import_id = NEW.import_id
                    AND i.valid AND i.content_hash = NEW.content_hash
                    AND i.original_hash = NEW.original_hash
                    AND i.metadata_json::jsonb = NEW.metadata_json::jsonb
                    AND NEW.supersedes_id IS NOT DISTINCT FROM
                        (i.metadata_json ->> 'supersedes_id')
                    AND jsonb_typeof(NEW.manifest_json::jsonb -> 'original_recorded_at')
                        = 'string'
                    AND (NEW.manifest_json ->> 'original_recorded_at')::timestamptz
                        = i.created_at
                    AND json_array_length(i.canonical_bars) = NEW.bar_count;
                IF bars_bytes IS NULL OR NEW.manifest_json::jsonb
                    IS DISTINCT FROM expected_manifest THEN
                    RAISE EXCEPTION 'Research dataset must match a valid immutable receipt';
                END IF;
                NEW.stored_bytes := bars_bytes + octet_length(NEW.manifest_json::text)
                    + octet_length(NEW.metadata_json::text);
            END IF;
            SELECT coalesce(sum(s.stored_bytes), 0) INTO used_bytes FROM (
                SELECT stored_bytes FROM public.research_imports WHERE owner_id = NEW.owner_id
                UNION ALL
                SELECT stored_bytes FROM public.research_datasets WHERE owner_id = NEW.owner_id
            ) s;
            IF used_bytes + NEW.stored_bytes > 1073741824 THEN
                RAISE EXCEPTION 'Owner research stored-byte quota exceeded';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_bar_admission() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.research_datasets d
                WHERE d.owner_id = NEW.owner_id AND d.dataset_id = NEW.dataset_id
                AND d.freeze_xid = txid_current()
                AND NEW.ordinal >= 0 AND NEW.ordinal < d.bar_count
                AND (NEW.bar_json ->> 'ts')::timestamptz = NEW.ts
            ) THEN
                RAISE EXCEPTION 'Research bars must match receipt during freeze transaction';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_dataset_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE expected_bars json;
        BEGIN
            IF (SELECT count(*) FROM public.research_bars b
                WHERE b.owner_id = NEW.owner_id AND b.dataset_id = NEW.dataset_id)
                <> NEW.bar_count THEN
                RAISE EXCEPTION 'Research dataset freeze is incomplete';
            END IF;
            SELECT i.canonical_bars INTO expected_bars FROM public.research_imports i
                WHERE i.owner_id = NEW.owner_id AND i.import_id = NEW.import_id;
            -- Scan all mismatches: EXISTS startup planning can repeatedly scan
            -- every stored bar when uncommitted table cardinality is unknown.
            IF (
                SELECT count(*) FROM json_array_elements(expected_bars)
                    WITH ORDINALITY AS expected(bar_json, position)
                LEFT JOIN public.research_bars b
                    ON b.owner_id = NEW.owner_id AND b.dataset_id = NEW.dataset_id
                    AND b.ordinal = expected.position - 1
                WHERE expected.bar_json::jsonb IS DISTINCT FROM b.bar_json::jsonb
            ) > 0 THEN
                RAISE EXCEPTION 'Research bars must match the exact immutable receipt';
            END IF;
            RETURN NEW;
        END $$
    """)
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.vm_research_immutable()"
        )
    for table in TABLES[:2]:
        op.execute(
            f"CREATE TRIGGER {table}_admission BEFORE INSERT ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.vm_research_data_admission()"
        )
    op.execute(
        "CREATE TRIGGER research_bars_admission BEFORE INSERT ON public.research_bars "
        "FOR EACH ROW EXECUTE FUNCTION public.vm_research_bar_admission()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER research_dataset_complete AFTER INSERT "
        "ON public.research_datasets DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION public.vm_research_dataset_complete()"
    )
    for function in (
        "vm_research_immutable",
        "vm_research_data_admission",
        "vm_research_bar_admission",
        "vm_research_dataset_complete",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def upgrade() -> None:
    _schema()
    if op.get_bind().dialect.name != "postgresql":
        return
    _postgres_guards()
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        predicate = (
            "owner_id = public.vm_deployment_owner_id() "
            "AND owner_id = current_setting('app.current_tenant', true)"
        )
        op.execute(
            f"CREATE POLICY {table}_backend_select ON public.{table} "
            f"FOR SELECT TO vm_backend USING ({predicate})"
        )
        op.execute(
            f"CREATE POLICY {table}_backend_insert ON public.{table} "
            f"FOR INSERT TO vm_backend WITH CHECK ({predicate})"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_backend")
    for table, columns in READ_COLUMNS.items():
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON TABLE public.{table} TO vm_backend")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute(
            "LOCK TABLE public.research_imports, public.research_datasets, "
            "public.research_bars IN ACCESS EXCLUSIVE MODE"
        )
    retained = sum(
        int(op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0) for table in TABLES
    )
    if retained:
        msg = "Cannot remove retained research evidence; restore a matched snapshot"
        raise RuntimeError(msg)
    if op.get_bind().dialect.name == "postgresql":
        for table, columns in NEW_READ_COLUMNS.items():
            op.execute(
                f"REVOKE SELECT ({', '.join(columns)}) ON TABLE public.{table} FROM vm_backend"
            )
    for table in reversed(TABLES):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "vm_research_dataset_complete",
            "vm_research_bar_admission",
            "vm_research_data_admission",
            "vm_research_immutable",
        ):
            op.execute(f"DROP FUNCTION public.{function}()")


__all__ = ["READ_COLUMNS", "TABLES", "downgrade", "upgrade"]
