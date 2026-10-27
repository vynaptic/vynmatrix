"""Owner-scoped source revisions, durable fenced jobs and immutable model registry.

Revision ID: 0114_research_workflows
Revises: 0113_research_data
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0114_research_workflows"
down_revision = "0113_research_data"
branch_labels = None
depends_on = None

TABLES = (
    "research_drafts",
    "research_draft_revisions",
    "research_jobs",
    "research_job_attempts",
    "research_model_versions",
)
OWNER = (
    "owner_id = public.vm_deployment_owner_id() AND owner_id = "
    "current_setting('app.current_tenant', true)"
)
BACKEND_UPDATE = {
    "research_drafts": ("name", "revision", "latest_revision_id"),
    "research_jobs": ("status", "current_attempt"),
    "research_job_attempts": ("status", "finished_at", "error_json", "claim_token"),
}
WORKER_UPDATE = {
    "research_jobs": ("status",),
    "research_job_attempts": (
        "status",
        "worker_id",
        "claim_token",
        "generation",
        "image_hash",
        "bundle_hash",
        "started_at",
        "heartbeat_at",
        "lease_expires_at",
        "finished_at",
        "error_json",
        "result_json",
    ),
}
HEARTBEAT_COLUMNS = ("worker_id", "image_hash", "bundle_hash", "heartbeat_at")
STATUSES = "'queued','running','completed','failed','cancelled','interrupted'"


def _fk(local: str, remote_table: str, remote: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["owner_id", local],
        [f"{remote_table}.owner_id", f"{remote_table}.{remote}"],
        ondelete="RESTRICT",
    )


def _schema() -> None:
    draft_columns: list[sa.SchemaItem] = [
        sa.Column("draft_id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(50),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("latest_revision_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "draft_id", name="uq_research_draft_owner"),
        sa.UniqueConstraint("owner_id", "request_id", name="uq_research_draft_request"),
    ]
    if op.get_bind().dialect.name != "postgresql":
        draft_columns.append(_fk("latest_revision_id", "research_draft_revisions", "revision_id"))
    op.create_table("research_drafts", *draft_columns)
    op.create_index(
        "ix_research_draft_owner_created", "research_drafts", ["owner_id", "created_at", "draft_id"]
    )
    op.create_table(
        "research_draft_revisions",
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(50), nullable=False),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "revision_id", name="uq_research_revision_owner"),
        sa.UniqueConstraint("owner_id", "draft_id", "revision", name="uq_research_draft_revision"),
        _fk("draft_id", "research_drafts", "draft_id"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_research_draft_latest",
            "research_drafts",
            "research_draft_revisions",
            ["owner_id", "latest_revision_id"],
            ["owner_id", "revision_id"],
            ondelete="RESTRICT",
        )
    job_columns: list[sa.SchemaItem] = [
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(50),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("lineage_json", sa.JSON(), nullable=False),
        sa.Column("inputs_hash", sa.String(64), nullable=False),
        sa.Column("revision_id", sa.String(36)),
        sa.Column("dataset_id", sa.String(36)),
        sa.Column("model_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "job_id", name="uq_research_job_owner"),
        _fk("revision_id", "research_draft_revisions", "revision_id"),
        _fk("dataset_id", "research_datasets", "dataset_id"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_research_job_status"),
        sa.CheckConstraint(
            "kind IN ('validate_strategy','train','predict','backtest')",
            name="ck_research_job_kind",
        ),
    ]
    if op.get_bind().dialect.name != "postgresql":
        job_columns.append(_fk("model_id", "research_model_versions", "model_id"))
    op.create_table("research_jobs", *job_columns)
    op.create_index("ix_research_job_queue", "research_jobs", ["status", "created_at", "job_id"])
    op.create_index(
        "ix_research_job_owner_created", "research_jobs", ["owner_id", "created_at", "job_id"]
    )
    op.create_table(
        "research_job_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(50), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("generation", sa.String(36)),
        sa.Column("image_hash", sa.String(255)),
        sa.Column("bundle_hash", sa.String(64)),
        *(
            sa.Column(name, sa.DateTime(timezone=True))
            for name in ("started_at", "heartbeat_at", "lease_expires_at", "finished_at")
        ),
        sa.Column("error_json", sa.JSON()),
        sa.Column("result_json", sa.JSON()),
        sa.UniqueConstraint("owner_id", "attempt_id", name="uq_research_attempt_owner"),
        sa.UniqueConstraint("owner_id", "request_id", name="uq_research_attempt_request"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_research_attempt_number"),
        _fk("job_id", "research_jobs", "job_id"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_research_attempt_status"),
    )
    op.create_table(
        "research_model_versions",
        sa.Column("model_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(50), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("dataset_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("training_cutoff", sa.String(50), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "model_id", name="uq_research_model_owner"),
        sa.UniqueConstraint("owner_id", "request_id", name="uq_research_model_request"),
        sa.UniqueConstraint("owner_id", "attempt_id", name="uq_research_model_attempt"),
        _fk("job_id", "research_jobs", "job_id"),
        _fk("attempt_id", "research_job_attempts", "attempt_id"),
        _fk("dataset_id", "research_datasets", "dataset_id"),
    )
    op.create_index(
        "ix_research_model_owner_created",
        "research_model_versions",
        ["owner_id", "created_at", "model_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_research_job_model",
            "research_jobs",
            "research_model_versions",
            ["owner_id", "model_id"],
            ["owner_id", "model_id"],
            ondelete="RESTRICT",
        )
    op.create_table(
        "research_control",
        sa.Column("control_id", sa.Integer(), primary_key=True),
        sa.Column("maintenance", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("image_hash", sa.String(255)),
        sa.Column("bundle_hash", sa.String(64)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "control_id = 1 AND length(generation) = 36", name="ck_research_control_singleton"
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO research_control (control_id,maintenance,generation) VALUES "
            "(1,false,:generation)"
        ).bindparams(generation=str(uuid4()))
    )


def _postgres_guards() -> None:
    op.execute("""
        CREATE FUNCTION public.vm_research_admission_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT pg_try_advisory_xact_lock(hashtextextended('vynmatrix.research.control',0))
            THEN RAISE EXCEPTION 'Research maintenance admission is busy'; END IF;
            IF EXISTS(SELECT 1 FROM public.research_control WHERE control_id=1 AND maintenance)
            THEN RAISE EXCEPTION 'Research is fenced for maintenance'; END IF;
            RETURN NEW;
        END $$
    """)
    for table in (
        "research_imports",
        "research_datasets",
        "research_bars",
        "research_drafts",
        "research_draft_revisions",
        "research_model_versions",
    ):
        op.execute(
            f"CREATE TRIGGER research_00_admission BEFORE INSERT ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.vm_research_admission_guard()"
        )
    op.execute("""
        CREATE FUNCTION public.vm_research_workflow_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE control public.research_control%ROWTYPE; active
        public.research_job_attempts%ROWTYPE;
        BEGIN
            IF NOT pg_try_advisory_xact_lock(hashtextextended('vynmatrix.research.control',0)) THEN
                RAISE EXCEPTION 'Research maintenance admission is busy'; END IF;
            SELECT * INTO STRICT control FROM public.research_control WHERE control_id=1;
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Research evidence is immutable'; END IF;
            IF TG_TABLE_NAME='research_jobs' THEN
                IF TG_OP='INSERT' THEN
                IF control.maintenance OR NEW.status <> 'queued' OR NEW.current_attempt <> 1 THEN
                        RAISE EXCEPTION 'Research admission is fenced';
                    END IF;
                ELSE
                IF (to_jsonb(NEW)-ARRAY['status','current_attempt']) IS DISTINCT
                FROM (to_jsonb(OLD)-ARRAY['status','current_attempt']) THEN
                        RAISE EXCEPTION 'Research job inputs are immutable';
                    END IF;
                    IF NEW.current_attempt <> OLD.current_attempt THEN
                IF control.maintenance OR OLD.status NOT IN ('failed','cancelled',
                'interrupted') OR NEW.status <> 'queued' OR NEW.current_attempt <>
                OLD.current_attempt+1 THEN
                            RAISE EXCEPTION 'Research retry transition is invalid';
                        END IF;
                    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
                IF OLD.status IN ('completed','failed','cancelled','interrupted')
                THEN RAISE EXCEPTION 'Terminal job requires explicit retry'; END IF;
                        IF NEW.status='running' THEN
                IF control.maintenance OR OLD.status <> 'queued' OR EXISTS(SELECT 1
                FROM public.research_jobs WHERE status='running' AND
                job_id<>NEW.job_id) THEN RAISE EXCEPTION 'Research claim is fenced'; END IF;
                        ELSIF NEW.status IN ('completed','failed') THEN
                SELECT * INTO active FROM public.research_job_attempts WHERE
                owner_id=NEW.owner_id AND job_id=NEW.job_id AND attempt=NEW.current_attempt;
                IF control.maintenance OR OLD.status<>'running' OR
                active.status<>'running' OR active.generation IS DISTINCT FROM
                control.generation OR active.worker_id IS DISTINCT FROM
                control.worker_id OR active.image_hash IS DISTINCT FROM
                control.image_hash OR active.bundle_hash IS DISTINCT FROM
                control.bundle_hash OR active.lease_expires_at IS NULL OR
                active.lease_expires_at <= clock_timestamp() THEN RAISE EXCEPTION
                'Research result is fenced'; END IF;
                ELSIF NEW.status NOT IN ('cancelled','interrupted') THEN RAISE
                EXCEPTION 'Research job transition is invalid';
                        END IF;
                    END IF;
                END IF;
            ELSE
                IF TG_OP='INSERT' THEN
                IF control.maintenance OR NEW.status<>'queued' OR
                (to_jsonb(NEW)-ARRAY['attempt_id','owner_id','job_id','attempt',
                'request_id','request_hash','status']) <>
                jsonb_build_object('worker_id',null,'claim_token',null,'generation',
                null,'image_hash',null,'bundle_hash',null,'started_at',null,
                'heartbeat_at',null,'lease_expires_at',null,'finished_at',null,
                'error_json',null,'result_json',null) OR NOT EXISTS(SELECT 1 FROM
                public.research_jobs WHERE owner_id=NEW.owner_id AND
                job_id=NEW.job_id AND current_attempt=NEW.attempt AND
                status='queued') THEN RAISE EXCEPTION
                'Research attempt admission is fenced'; END IF;
                ELSE
                IF (to_jsonb(NEW)-ARRAY['status','worker_id','claim_token',
                'generation','image_hash','bundle_hash','started_at','heartbeat_at',
                'lease_expires_at','finished_at','error_json','result_json']) IS
                DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','worker_id',
                'claim_token','generation','image_hash','bundle_hash','started_at',
                'heartbeat_at','lease_expires_at','finished_at','error_json',
                'result_json']) OR OLD.status IN ('completed','failed','cancelled',
                'interrupted') THEN RAISE EXCEPTION 'Research attempt is immutable'; END IF;
                IF NOT EXISTS(SELECT 1 FROM public.research_jobs WHERE
                owner_id=NEW.owner_id AND job_id=NEW.job_id AND
                current_attempt=NEW.attempt AND status=NEW.status) THEN RAISE
                EXCEPTION 'Research attempt is stale'; END IF;
                    IF NEW.status='running' THEN
                IF control.maintenance OR NEW.generation IS DISTINCT FROM
                control.generation OR NEW.worker_id IS DISTINCT FROM
                control.worker_id OR NEW.image_hash IS DISTINCT FROM
                control.image_hash OR NEW.bundle_hash IS DISTINCT FROM
                control.bundle_hash OR NEW.claim_token IS NULL OR
                NEW.lease_expires_at IS NULL OR NEW.lease_expires_at <=
                clock_timestamp() THEN RAISE EXCEPTION 'Research attempt claim is fenced'; END IF;
                IF OLD.status='running' AND (OLD.lease_expires_at <=
                clock_timestamp() OR NEW.claim_token IS DISTINCT FROM
                OLD.claim_token OR NEW.generation IS DISTINCT FROM OLD.generation
                OR NEW.worker_id IS DISTINCT FROM OLD.worker_id OR NEW.image_hash
                IS DISTINCT FROM OLD.image_hash OR NEW.bundle_hash IS DISTINCT FROM
                OLD.bundle_hash OR NEW.started_at IS DISTINCT FROM OLD.started_at)
                THEN RAISE EXCEPTION 'Research heartbeat is fenced'; END IF;
                    ELSIF NEW.status IN ('completed','failed') THEN
                IF control.maintenance OR OLD.status<>'running' OR OLD.generation
                IS DISTINCT FROM control.generation OR OLD.worker_id IS DISTINCT
                FROM control.worker_id OR OLD.image_hash IS DISTINCT FROM
                control.image_hash OR OLD.bundle_hash IS DISTINCT FROM
                control.bundle_hash OR OLD.lease_expires_at IS NULL OR
                OLD.lease_expires_at <= clock_timestamp() THEN RAISE EXCEPTION
                'Research result is fenced'; END IF;
                ELSIF NEW.status NOT IN ('cancelled','interrupted') THEN RAISE
                EXCEPTION 'Research attempt transition is invalid';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_control_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT pg_try_advisory_xact_lock(hashtextextended('vynmatrix.research.control',0)) THEN
                RAISE EXCEPTION 'Research maintenance admission is busy'; END IF;
            IF TG_OP<>'UPDATE' THEN RAISE EXCEPTION
            'Research control identity is immutable'; END IF;
            IF NEW.control_id<>OLD.control_id THEN RAISE EXCEPTION
            'Research control identity is immutable'; END IF;
            IF NEW.maintenance IS DISTINCT FROM OLD.maintenance OR NEW.generation
            IS DISTINCT FROM OLD.generation THEN
                IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON
                n.oid=c.relnamespace WHERE n.nspname='public' AND
                c.relname='research_control' AND pg_has_role(current_user,
                c.relowner,'USAGE')) THEN RAISE EXCEPTION
                'Maintenance database authority is required'; END IF;
                IF OLD.maintenance AND NOT NEW.maintenance THEN
                IF NEW.generation=OLD.generation OR NEW.generation !~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' OR
                EXISTS(SELECT 1 FROM public.research_jobs WHERE status='running')
                THEN RAISE EXCEPTION 'Research maintenance is not drained'; END IF;
                ELSIF NEW.generation<>OLD.generation THEN RAISE EXCEPTION
                'Research generation rotates only after maintenance'; END IF;
            ELSIF OLD.maintenance THEN RAISE EXCEPTION
            'Research supervisor is fenced for maintenance';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_draft_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT pg_try_advisory_xact_lock(hashtextextended('vynmatrix.research.control',0))
            THEN RAISE EXCEPTION 'Research maintenance admission is busy'; END IF;
            IF EXISTS(SELECT 1 FROM public.research_control WHERE control_id=1 AND maintenance)
            THEN RAISE EXCEPTION 'Research is fenced for maintenance'; END IF;
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Research draft evidence is immutable'; END IF;
            IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['name','revision',
            'latest_revision_id']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['name',
            'revision','latest_revision_id']) THEN RAISE EXCEPTION
            'Research draft identity is immutable'; END IF;
            IF NEW.latest_revision_id IS NOT NULL AND (NEW.revision <>
            OLD.revision+1 OR NOT EXISTS(SELECT 1 FROM
            public.research_draft_revisions WHERE owner_id=NEW.owner_id AND
            draft_id=NEW.draft_id AND revision_id=NEW.latest_revision_id AND
            revision=NEW.revision)) THEN RAISE EXCEPTION
            'Research draft revision conflict'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_model_admission() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.research_jobs j
                JOIN public.research_job_attempts a ON a.owner_id=j.owner_id AND
                a.job_id=j.job_id AND a.attempt=j.current_attempt
                JOIN public.research_datasets d ON d.owner_id=j.owner_id AND
                d.dataset_id=j.dataset_id
                WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id AND
                j.kind='train' AND j.status='completed'
                AND a.attempt_id=NEW.attempt_id AND a.status='completed' AND
                d.dataset_id=NEW.dataset_id
                AND (a.result_json->'model')::jsonb=NEW.artifact_json::jsonb
                AND (a.result_json->'metrics')::jsonb=NEW.metrics_json::jsonb
                AND NEW.training_cutoff=NEW.artifact_json->>'training_cutoff'
                AND NEW.artifact_hash=NEW.artifact_json->>'artifact_hash'
                AND NEW.artifact_json->'identity'->>'dataset_hash'=d.content_hash
                AND regexp_replace(NEW.artifact_json->'identity'->>'image_hash',
                '^sha256:','')=regexp_replace(a.image_hash,'^sha256:','')
            ) THEN RAISE EXCEPTION
            'Model registration requires its exact successful train artifact'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION public.vm_research_attempt_complete() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM public.research_jobs j JOIN
            public.research_job_attempts a ON a.owner_id=j.owner_id AND
            a.job_id=j.job_id AND a.attempt=j.current_attempt AND a.status=j.status
            WHERE j.owner_id=NEW.owner_id AND j.job_id=NEW.job_id) THEN RAISE
            EXCEPTION 'Research current attempt is incomplete'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER research_model_admission BEFORE INSERT ON "
        "public.research_model_versions FOR EACH ROW EXECUTE FUNCTION "
        "public.vm_research_model_admission()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER research_attempt_complete AFTER INSERT OR "
        "UPDATE ON public.research_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION public.vm_research_attempt_complete()"
    )
    for table in ("research_draft_revisions", "research_model_versions"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "
            f"public.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"public.vm_research_immutable()"
        )
    for table in ("research_jobs", "research_job_attempts"):
        op.execute(
            f"CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON "
            f"public.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"public.vm_research_workflow_guard()"
        )
    op.execute(
        "CREATE TRIGGER research_drafts_guard BEFORE UPDATE OR DELETE ON "
        "public.research_drafts FOR EACH ROW EXECUTE FUNCTION "
        "public.vm_research_draft_guard()"
    )
    op.execute(
        "CREATE TRIGGER research_control_guard BEFORE UPDATE OR DELETE ON "
        "public.research_control FOR EACH ROW EXECUTE FUNCTION "
        "public.vm_research_control_guard()"
    )
    for function in (
        "vm_research_workflow_guard",
        "vm_research_control_guard",
        "vm_research_draft_guard",
        "vm_research_model_admission",
        "vm_research_attempt_complete",
        "vm_research_admission_guard",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def _sqlite_guards() -> None:
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT "
            f"RAISE(ABORT,'Research evidence is immutable'); END"
        )
    for table in ("research_draft_revisions", "research_model_versions"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT "
            f"RAISE(ABORT,'Research evidence is immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER research_job_inputs_immutable BEFORE UPDATE OF "
        "job_id,owner_id,kind,inputs_json,lineage_json,inputs_hash,revision_id,dataset_id,"
        "model_id,created_at ON research_jobs BEGIN SELECT "
        "RAISE(ABORT,'Research job inputs are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER research_attempt_finished_immutable BEFORE UPDATE ON "
        "research_job_attempts WHEN OLD.status IN "
        "('completed','failed','cancelled','interrupted') BEGIN SELECT "
        "RAISE(ABORT,'Research attempt is immutable'); END"
    )


def _role_and_grants() -> None:
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE "
        "rolname='vm_research') THEN CREATE ROLE vm_research NOLOGIN NOSUPERUSER "
        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; ELSIF EXISTS(SELECT 1 "
        "FROM pg_roles WHERE rolname='vm_research' AND (rolcanlogin OR rolsuper OR "
        "rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) OR "
        "EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM "
        "pg_roles WHERE rolname='vm_research')) THEN RAISE EXCEPTION 'vm_research "
        "has unexpected authority'; END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO vm_research")
    op.execute("GRANT EXECUTE ON FUNCTION public.vm_deployment_owner_id() TO vm_research")
    for table in (*TABLES, "research_datasets", "research_bars"):
        if table in TABLES:
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
            op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO vm_backend")
            for command in ("SELECT", "INSERT"):
                clause = f"USING ({OWNER})" if command == "SELECT" else f"WITH CHECK ({OWNER})"
                op.execute(
                    f"CREATE POLICY {table}_backend_{command.lower()} ON public.{table} FOR "
                    f"{command} TO vm_backend {clause}"
                )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO vm_research")
        op.execute(
            f"CREATE POLICY {table}_research_select ON public.{table} FOR SELECT TO "
            f"vm_research USING ({OWNER})"
        )
    for role, inventory in (("vm_backend", BACKEND_UPDATE), ("vm_research", WORKER_UPDATE)):
        for table, columns in inventory.items():
            op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON TABLE public.{table} TO {role}")
            op.execute(
                f"CREATE POLICY {table}_{role}_update ON public.{table} FOR UPDATE TO "
                f"{role} USING ({OWNER}) WITH CHECK ({OWNER})"
            )
    op.execute("GRANT SELECT ON TABLE public.research_control TO vm_backend, vm_research")
    op.execute(
        f"GRANT UPDATE ({', '.join(HEARTBEAT_COLUMNS)}) ON TABLE "
        f"public.research_control TO vm_research"
    )


def upgrade() -> None:
    _schema()
    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()
        _role_and_grants()
    elif op.get_bind().dialect.name == "sqlite":
        _sqlite_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Fence before taking table locks. Runtime admission uses try-locks, so
        # a writer cannot hold a table lock while waiting for this control key.
        acquired = bind.scalar(
            sa.text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('vynmatrix.research.control',0))"
            )
        )
        if acquired is not True:
            msg = "Research control is busy; retry downgrade on its maintenance connection"
            raise RuntimeError(msg)
        affected = (
            "research_control",
            *TABLES,
            "research_imports",
            "research_datasets",
            "research_bars",
        )
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute(
            "LOCK TABLE "
            + ", ".join("public." + table for table in affected)
            + " IN ACCESS EXCLUSIVE MODE"
        )
    retained = sum(
        int(op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0) for table in TABLES
    )
    if retained:
        msg = "Cannot remove retained research evidence; restore a matched snapshot"
        raise RuntimeError(msg)
    if op.get_bind().dialect.name == "postgresql":
        for table in ("research_imports", "research_datasets", "research_bars"):
            op.execute(f"DROP TRIGGER research_00_admission ON public.{table}")
        for table in ("research_datasets", "research_bars"):
            op.execute(f"DROP POLICY {table}_research_select ON public.{table}")
            op.execute(f"REVOKE SELECT ON TABLE public.{table} FROM vm_research")
        op.drop_constraint("fk_research_job_model", "research_jobs", type_="foreignkey")
        op.drop_constraint("fk_research_draft_latest", "research_drafts", type_="foreignkey")
    op.drop_table("research_control")
    for table in reversed(TABLES):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        for function in (
            "vm_research_workflow_guard",
            "vm_research_control_guard",
            "vm_research_draft_guard",
            "vm_research_model_admission",
            "vm_research_attempt_complete",
            "vm_research_admission_guard",
        ):
            op.execute(f"DROP FUNCTION public.{function}()")
        op.execute("REVOKE EXECUTE ON FUNCTION public.vm_deployment_owner_id() FROM vm_research")
        op.execute("REVOKE USAGE ON SCHEMA public FROM vm_research")
        # The provisioned login may still be a member; removing group membership is
        # separate credential maintenance, so retain this now-unprivileged role.
