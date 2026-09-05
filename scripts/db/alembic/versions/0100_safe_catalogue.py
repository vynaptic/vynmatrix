"""Register new strategies and releases without granting execution authority.

Existing identities and activation states are retained. Fixed registration
functions compare supplied metadata on retries and never patch existing rows.
NULL description/image/repository/commit arguments mean omitted, preserving an
installed value; they cannot clear metadata. JSON documents are required in full.

Revision ID: 0100_safe_catalogue
Revises: 0099_single_owner_authority
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100_safe_catalogue"
down_revision = "0099_single_owner_authority"
branch_labels = None
depends_on = None

_READ_TABLES = (
    "broker_environments",
    "sectors",
    "instrument_sectors",
    "instrument_aliases",
    "instrument_broker_symbols",
)
_STRATEGY_FUNCTION = "public.vm_catalogue_create_strategy(text, text, text, text)"
_VERSION_FUNCTION = "public.vm_catalogue_create_version(text, text, jsonb, jsonb, text, text, text)"


def _install_registration_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_catalogue_create_strategy(
            p_id text, p_name text, p_asset_class text, p_description text
        ) RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            installed public.strategies%ROWTYPE;
        BEGIN
            IF p_id IS NULL OR btrim(p_id) = ''
               OR p_name IS NULL OR btrim(p_name) = ''
               OR p_asset_class IS NULL OR btrim(p_asset_class) = '' THEN
                RAISE EXCEPTION 'Strategy ID, name and asset class are required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT * INTO installed
            FROM public.strategies
            WHERE strategy_id = p_id
            FOR UPDATE;
            IF FOUND THEN
                IF installed.strategy_name IS DISTINCT FROM p_name
                   OR installed.asset_class IS DISTINCT FROM p_asset_class
                   OR (p_description IS NOT NULL
                       AND installed.description IS DISTINCT FROM p_description) THEN
                    RAISE EXCEPTION 'Strategy catalogue conflict for %', p_id
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.strategy_id;
            END IF;
            INSERT INTO public.strategies (
                strategy_id, strategy_name, asset_class, description, is_active
            ) VALUES (p_id, p_name, p_asset_class, p_description, false);
            RETURN p_id;
        END;
        $function$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.vm_catalogue_create_version(
            p_strategy_id text, p_semver text, p_param_schema jsonb,
            p_default_params jsonb, p_docker_image text, p_git_repo text, p_git_commit text
        ) RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            installed public.strategy_versions%ROWTYPE;
            registered_id bigint;
        BEGIN
            IF p_strategy_id IS NULL OR btrim(p_strategy_id) = ''
               OR p_semver IS NULL OR btrim(p_semver) = ''
               OR p_param_schema IS NULL OR p_default_params IS NULL
               OR jsonb_typeof(p_param_schema) <> 'object'
               OR jsonb_typeof(p_default_params) <> 'object' THEN
                RAISE EXCEPTION 'Strategy ID, semantic version and JSON objects are required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT * INTO installed
            FROM public.strategy_versions
            WHERE strategy_id = p_strategy_id AND semver = p_semver
            FOR UPDATE;
            IF FOUND THEN
                IF installed.param_schema::jsonb IS DISTINCT FROM p_param_schema
                   OR installed.default_params::jsonb IS DISTINCT FROM p_default_params
                   OR (p_docker_image IS NOT NULL
                       AND installed.docker_image IS DISTINCT FROM p_docker_image)
                   OR (p_git_repo IS NOT NULL
                       AND installed.git_repo IS DISTINCT FROM p_git_repo)
                   OR (p_git_commit IS NOT NULL
                       AND installed.git_commit IS DISTINCT FROM p_git_commit) THEN
                    RAISE EXCEPTION 'Strategy version conflict for % at %', p_strategy_id, p_semver
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.strat_ver_id;
            END IF;
            INSERT INTO public.strategy_versions (
                strategy_id, semver, param_schema, default_params,
                docker_image, git_repo, git_commit, status
            ) VALUES (
                p_strategy_id, p_semver, p_param_schema, p_default_params,
                p_docker_image, p_git_repo, p_git_commit, 'registered'
            ) RETURNING strat_ver_id INTO registered_id;
            RETURN registered_id;
        END;
        $function$;
        """
    )
    for signature in (_STRATEGY_FUNCTION, _VERSION_FUNCTION):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO vm_backend")
    tables = ", ".join(f"public.{table}" for table in _READ_TABLES)
    op.execute(f"GRANT SELECT ON TABLE {tables} TO vm_backend")


def upgrade() -> None:
    with op.batch_alter_table("strategies") as batch:
        batch.alter_column("is_active", existing_type=sa.Boolean(), server_default=sa.false())
    with op.batch_alter_table("strategy_versions") as batch:
        batch.drop_constraint("ck_version_status", type_="check")
        batch.create_check_constraint(
            "ck_version_status", "status IN ('registered', 'active', 'deprecated', 'pulled')"
        )
        batch.alter_column("status", existing_type=sa.String(20), server_default="registered")
    if op.get_bind().dialect.name == "postgresql":
        _install_registration_functions()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SELECT pg_catalog.pg_advisory_xact_lock(18472, 1)")
        op.execute(
            "LOCK TABLE public.strategies, public.strategy_versions IN ACCESS EXCLUSIVE MODE"
        )
    registered = bind.execute(
        sa.text("SELECT count(*) FROM strategy_versions WHERE status = 'registered'")
    ).scalar_one()
    if registered:
        message = (
            "Cannot downgrade registered strategy versions; explicit backup and disposition "
            "are required. Registration must never be converted to execution authority."
        )
        raise RuntimeError(message)
    if bind.dialect.name == "postgresql":
        for signature in (_VERSION_FUNCTION, _STRATEGY_FUNCTION):
            op.execute(f"DROP FUNCTION {signature}")
        tables = ", ".join(f"public.{table}" for table in _READ_TABLES)
        op.execute(f"REVOKE SELECT ON TABLE {tables} FROM vm_backend")
    with op.batch_alter_table("strategy_versions") as batch:
        batch.drop_constraint("ck_version_status", type_="check")
        batch.create_check_constraint(
            "ck_version_status", "status IN ('active', 'deprecated', 'pulled')"
        )
        batch.alter_column("status", existing_type=sa.String(20), server_default="active")
    with op.batch_alter_table("strategies") as batch:
        batch.alter_column("is_active", existing_type=sa.Boolean(), server_default=sa.true())
