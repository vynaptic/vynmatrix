"""Permit fixed reference registration without runtime table mutation privileges.

Revision ID: 0101_reference_catalogue
Revises: 0100_safe_catalogue
"""

from __future__ import annotations

from alembic import op

revision = "0101_reference_catalogue"
down_revision = "0100_safe_catalogue"
branch_labels = None
depends_on = None

_SIGNATURES = (
    "public.vm_catalogue_create_broker(text, text, jsonb)",
    "public.vm_catalogue_create_broker_environment(text, text, text, jsonb, jsonb)",
    "public.vm_catalogue_create_instrument(text, text, text, text, boolean)",
    "public.vm_catalogue_create_sector(text, text, text)",
    "public.vm_catalogue_link_sector(integer, text)",
    "public.vm_catalogue_patch_broker(text, jsonb, jsonb)",
    "public.vm_catalogue_patch_broker_environment(text, text, text, jsonb, jsonb)",
    "public.vm_catalogue_patch_sector(text, jsonb, jsonb)",
    "public.vm_catalogue_create_alias(text, text, text)",
    "public.vm_catalogue_create_broker_symbol(text, text, text, text, text)",
)
_INTERNAL_SIGNATURES = (
    "public.vm_catalogue_check_patch(jsonb, jsonb, jsonb, text[])",
    "public.vm_catalogue_check_environment(text, jsonb, jsonb)",
)


def _install_metadata_changes() -> None:
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_check_patch(
            p_row jsonb, p_expected jsonb, p_changes jsonb, p_fields text[]
        ) RETURNS void LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE field text;
        BEGIN
            IF jsonb_typeof(p_expected) IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_changes) IS DISTINCT FROM 'object' OR p_changes = '{}'::jsonb
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_expected) key)
                  IS DISTINCT FROM
                  (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_changes) key) THEN
                RAISE EXCEPTION 'Every changed field requires an expected current value'
                    USING ERRCODE = '22023';
            END IF;
            FOR field IN SELECT jsonb_object_keys(p_changes) LOOP
                IF NOT field = ANY(p_fields) THEN
                    RAISE EXCEPTION 'Immutable or unsupported catalogue field'
                        USING ERRCODE = '22023';
                END IF;
                IF p_row->field IS DISTINCT FROM p_expected->field
                   AND p_row->field IS DISTINCT FROM p_changes->field THEN
                    RAISE EXCEPTION 'Catalogue expected-value conflict' USING ERRCODE = '23505';
                END IF;
            END LOOP;
            IF p_changes ? 'name' AND (
                jsonb_typeof(p_changes->'name') IS DISTINCT FROM 'string'
                OR length(btrim(p_changes->>'name')) NOT BETWEEN 1 AND 255
                OR p_changes->>'name' <> btrim(p_changes->>'name')
            ) THEN
                RAISE EXCEPTION 'Invalid catalogue display name' USING ERRCODE = '22023';
            END IF;
            IF p_changes ? 'capabilities'
               AND jsonb_typeof(p_changes->'capabilities') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Broker capabilities must be an object' USING ERRCODE = '22023';
            END IF;
            IF p_changes ? 'description' AND (
                jsonb_typeof(p_changes->'description') NOT IN ('string', 'null')
                OR length(p_changes->>'description') > 10000
            ) THEN
                RAISE EXCEPTION 'Invalid sector description' USING ERRCODE = '22023';
            END IF;
        END;
        $function$;

        CREATE FUNCTION public.vm_catalogue_check_environment(
            p_code text, p_urls jsonb, p_rates jsonb
        ) RETURNS void LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE item record; endpoint text; authority text; port_text text;
        BEGIN
            IF jsonb_typeof(p_urls) IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_rates) IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Endpoints and rate limits must be objects' USING ERRCODE = '22023';
            END IF;
            FOR item IN SELECT key, value FROM jsonb_each(p_urls) LOOP
                IF item.key NOT IN ('rest', 'ws', 'gateway', 'transport') THEN
                    RAISE EXCEPTION 'Unsupported endpoint key' USING ERRCODE = '22023';
                END IF;
                IF item.key = 'transport' THEN
                    IF p_code <> 'paper' OR item.value <> '"in_process"'::jsonb THEN
                        RAISE EXCEPTION 'Unsupported in-process transport' USING ERRCODE = '22023';
                    END IF;
                ELSIF item.value <> 'null'::jsonb THEN
                    endpoint := item.value #>> '{}';
                    IF jsonb_typeof(item.value) <> 'string'
                       OR length(endpoint) > 2048
                       OR endpoint !~ '^(https|wss)://[^/?#@%[:space:]]+(/[^?#[:space:]]*)?$' THEN
                        RAISE EXCEPTION 'Endpoint requires https/wss without credentials or query'
                            USING ERRCODE = '22023';
                    END IF;
                    authority := split_part(split_part(endpoint, '://', 2), '/', 1);
                    IF left(authority, 1) = '[' THEN
                        port_text := substring(authority FROM '\\]:(.*)$');
                    ELSE
                        port_text := substring(authority FROM ':(.*)$');
                    END IF;
                    IF port_text IS NOT NULL AND port_text <> '' THEN
                        IF port_text !~ '^[0-9]+$' THEN
                            RAISE EXCEPTION 'Invalid broker endpoint port'
                                USING ERRCODE = '22023';
                        END IF;
                        port_text := ltrim(port_text, '0');
                        IF length(port_text) NOT BETWEEN 1 AND 5 THEN
                            RAISE EXCEPTION 'Invalid broker endpoint port'
                                USING ERRCODE = '22023';
                        END IF;
                        IF port_text::integer NOT BETWEEN 1 AND 65535 THEN
                            RAISE EXCEPTION 'Invalid broker endpoint port'
                                USING ERRCODE = '22023';
                        END IF;
                    END IF;
                END IF;
            END LOOP;
            FOR item IN SELECT key, value FROM jsonb_each(p_rates) LOOP
                IF jsonb_typeof(item.value) <> 'number'
                   OR length(btrim(item.key)) NOT BETWEEN 1 AND 100 THEN
                    RAISE EXCEPTION 'Invalid rate limit' USING ERRCODE = '22023';
                END IF;
                IF (item.value #>> '{}')::numeric <= 0 THEN
                    RAISE EXCEPTION 'Rate limits must be positive' USING ERRCODE = '22023';
                END IF;
            END LOOP;
        END;
        $function$;

        CREATE FUNCTION public.vm_catalogue_patch_broker(
            p_code text, p_expected jsonb, p_changes jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.brokers%ROWTYPE;
        BEGIN
            PERFORM pg_advisory_xact_lock(18472, 1);
            SELECT * INTO STRICT installed FROM public.brokers WHERE code = p_code FOR UPDATE;
            PERFORM public.vm_catalogue_check_patch(to_jsonb(installed), p_expected, p_changes,
                                                   ARRAY['name', 'capabilities']);
            UPDATE public.brokers SET
                name = CASE WHEN p_changes ? 'name' THEN p_changes->>'name' ELSE name END,
                capabilities = CASE WHEN p_changes ? 'capabilities'
                                    THEN (p_changes->'capabilities')::json ELSE capabilities END
            WHERE broker_id = installed.broker_id;
            RETURN installed.broker_id;
        END;
        $function$;

        CREATE FUNCTION public.vm_catalogue_patch_broker_environment(
            p_code text, p_environment text, p_region text, p_expected jsonb, p_changes jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.broker_environments%ROWTYPE; broker_key integer;
        BEGIN
            PERFORM pg_advisory_xact_lock(18472, 1);
            SELECT broker_id INTO STRICT broker_key FROM public.brokers WHERE code = p_code;
            SELECT * INTO STRICT installed FROM public.broker_environments
            WHERE broker_id = broker_key AND environment = p_environment AND region = p_region
            FOR UPDATE;
            PERFORM public.vm_catalogue_check_patch(to_jsonb(installed), p_expected, p_changes,
                                                   ARRAY['base_urls', 'rate_limits']);
            PERFORM public.vm_catalogue_check_environment(p_code,
                CASE WHEN p_changes ? 'base_urls' THEN p_changes->'base_urls'
                     ELSE '{}'::jsonb END,
                CASE WHEN p_changes ? 'rate_limits' THEN p_changes->'rate_limits'
                     ELSE '{}'::jsonb END);
            UPDATE public.broker_environments SET
                base_urls = CASE WHEN p_changes ? 'base_urls'
                                 THEN (p_changes->'base_urls')::json ELSE base_urls END,
                rate_limits = CASE WHEN p_changes ? 'rate_limits'
                                   THEN (p_changes->'rate_limits')::json ELSE rate_limits END
            WHERE broker_env_id = installed.broker_env_id;
            RETURN installed.broker_env_id;
        END;
        $function$;

        CREATE FUNCTION public.vm_catalogue_patch_sector(
            p_code text, p_expected jsonb, p_changes jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.sectors%ROWTYPE;
        BEGIN
            PERFORM pg_advisory_xact_lock(18472, 1);
            SELECT * INTO STRICT installed FROM public.sectors WHERE code = p_code FOR UPDATE;
            PERFORM public.vm_catalogue_check_patch(to_jsonb(installed), p_expected, p_changes,
                                                   ARRAY['name', 'description']);
            UPDATE public.sectors SET
                name = CASE WHEN p_changes ? 'name' THEN p_changes->>'name' ELSE name END,
                description = CASE WHEN p_changes ? 'description'
                                   THEN p_changes->>'description' ELSE description END
            WHERE sector_id = installed.sector_id;
            RETURN installed.sector_id;
        END;
        $function$;
    """)


def _install_instrument_links() -> None:
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_create_alias(
            p_canonical text, p_alias text, p_source text
        ) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE instrument_key integer; installed public.instrument_aliases%ROWTYPE;
        BEGIN
            IF p_alias IS NULL OR length(btrim(p_alias)) NOT BETWEEN 1 AND 100
               OR p_source IS NULL OR length(btrim(p_source)) NOT BETWEEN 1 AND 50 THEN
                RAISE EXCEPTION 'Explicit alias and source required' USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(18472, 1);
            SELECT instr_id INTO STRICT instrument_key FROM public.instruments
            WHERE upper(translate(canonical, '/-_', '')) = upper(translate(p_canonical, '/-_', ''));
            IF EXISTS (SELECT 1 FROM public.instrument_aliases
                       WHERE upper(translate(alias, '/-_', ''))
                           = upper(translate(p_alias, '/-_', ''))
                         AND instr_id <> instrument_key)
               OR EXISTS (SELECT 1 FROM public.instruments
                          WHERE upper(translate(canonical, '/-_', ''))
                              = upper(translate(p_alias, '/-_', ''))
                            AND instr_id <> instrument_key) THEN
                RAISE EXCEPTION 'Alias belongs to another instrument' USING ERRCODE = '23505';
            END IF;
            SELECT * INTO installed FROM public.instrument_aliases WHERE alias = p_alias FOR UPDATE;
            IF FOUND THEN
                IF installed.instr_id <> instrument_key OR installed.source
                    IS DISTINCT FROM p_source THEN
                    RAISE EXCEPTION 'Alias reference conflict' USING ERRCODE = '23505';
                END IF;
                RETURN installed.alias_id;
            END IF;
            INSERT INTO public.instrument_aliases(instr_id, alias, source)
            VALUES (instrument_key, p_alias, p_source) RETURNING alias_id INTO installed.alias_id;
            RETURN installed.alias_id;
        END;
        $function$;

        CREATE FUNCTION public.vm_catalogue_create_broker_symbol(
            p_canonical text, p_broker_code text, p_symbol text,
            p_venue_id text, p_venue_type text
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE instrument public.instruments%ROWTYPE; broker_key integer;
                installed public.instrument_broker_symbols%ROWTYPE;
        BEGIN
            IF p_symbol IS NULL OR length(btrim(p_symbol)) NOT BETWEEN 1 AND 100
               OR (p_venue_id IS NOT NULL AND length(btrim(p_venue_id)) NOT BETWEEN 1 AND 255)
               OR (p_venue_type IS NOT NULL AND length(btrim(p_venue_type)) NOT BETWEEN 1
                   AND 100) THEN
                RAISE EXCEPTION 'Invalid broker instrument identity' USING ERRCODE = '22023';
            END IF;
            PERFORM pg_advisory_xact_lock(18472, 1);
            SELECT * INTO STRICT instrument FROM public.instruments
            WHERE upper(translate(canonical, '/-_', '')) = upper(translate(p_canonical, '/-_', ''));
            SELECT broker_id INTO STRICT broker_key FROM public.brokers WHERE code = p_broker_code;
            IF instrument.asset_class = 'crypto'
               AND (p_broker_code IN ('deribit', 'delta')
                    OR upper(p_symbol) LIKE '%PERPETUAL%') THEN
                RAISE EXCEPTION 'Derivative mappings require a concrete derivative instrument'
                    USING ERRCODE = '22023';
            END IF;
            SELECT * INTO installed FROM public.instrument_broker_symbols
            WHERE instr_id = instrument.instr_id AND broker_id = broker_key FOR UPDATE;
            IF FOUND THEN
                IF installed.broker_symbol IS DISTINCT FROM p_symbol
                   OR (p_venue_id IS NOT NULL AND installed.broker_instrument_id
                       IS DISTINCT FROM p_venue_id)
                   OR (p_venue_type IS NOT NULL
                       AND installed.broker_instrument_type IS DISTINCT FROM p_venue_type) THEN
                    RAISE EXCEPTION 'Immutable broker instrument mapping conflict' USING ERRCODE
                        = '23505';
                END IF;
                RETURN instrument.instr_id;
            END IF;
            INSERT INTO public.instrument_broker_symbols(
                instr_id, broker_id, broker_symbol, broker_instrument_id, broker_instrument_type
            ) VALUES (instrument.instr_id, broker_key, p_symbol, p_venue_id, p_venue_type);
            RETURN instrument.instr_id;
        END;
        $function$;
    """)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 0067 granted table-wide UPDATE for calendar assignment. Keep that exact
    # workflow without permitting changes to instrument identity or terms.
    op.execute("REVOKE UPDATE ON TABLE public.instruments FROM vm_backend")
    op.execute("GRANT UPDATE (market_calendar_id) ON TABLE public.instruments TO vm_backend")
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_create_broker(
            p_code text, p_name text, p_capabilities jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.brokers%ROWTYPE;
        BEGIN
            IF p_code IS NULL OR btrim(p_code) = '' OR p_name IS NULL OR btrim(p_name) = ''
               OR p_capabilities IS NULL OR jsonb_typeof(p_capabilities) <> 'object' THEN
                RAISE EXCEPTION 'Explicit broker code, name and capabilities required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT * INTO installed FROM public.brokers WHERE code = p_code FOR UPDATE;
            IF FOUND THEN
                IF installed.name IS DISTINCT FROM p_name
                   OR installed.capabilities::jsonb IS DISTINCT FROM p_capabilities THEN
                    RAISE EXCEPTION 'Broker reference conflict for %', p_code
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.broker_id;
            END IF;
            INSERT INTO public.brokers(code, name, capabilities)
            VALUES (p_code, p_name, p_capabilities) RETURNING * INTO installed;
            RETURN installed.broker_id;
        END;
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_create_broker_environment(
            p_broker_code text, p_environment text, p_region text,
            p_base_urls jsonb, p_rate_limits jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.broker_environments%ROWTYPE; broker_key integer;
        BEGIN
            IF p_environment IS NULL OR p_environment NOT IN ('paper', 'live')
               OR p_region IS NULL OR btrim(p_region) = ''
               OR p_base_urls IS NULL OR jsonb_typeof(p_base_urls) <> 'object'
               OR p_rate_limits IS NULL OR jsonb_typeof(p_rate_limits) <> 'object' THEN
                RAISE EXCEPTION 'Explicit broker environment, region, endpoints and limits required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM public.vm_catalogue_check_environment(
                p_broker_code, p_base_urls, p_rate_limits);
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT broker_id INTO STRICT broker_key FROM public.brokers WHERE code = p_broker_code;
            SELECT * INTO installed FROM public.broker_environments
            WHERE broker_id = broker_key AND environment = p_environment AND region = p_region
            FOR UPDATE;
            IF FOUND THEN
                IF installed.base_urls::jsonb IS DISTINCT FROM p_base_urls
                   OR installed.rate_limits::jsonb IS DISTINCT FROM p_rate_limits THEN
                    RAISE EXCEPTION 'Broker environment reference conflict for %', p_broker_code
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.broker_env_id;
            END IF;
            INSERT INTO public.broker_environments(
                broker_id, environment, region, base_urls, rate_limits
            )
            VALUES (broker_key, p_environment, p_region, p_base_urls, p_rate_limits)
            RETURNING * INTO installed;
            RETURN installed.broker_env_id;
        END;
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_create_instrument(
            p_symbol text, p_asset_class text, p_currency text,
            p_session_policy text, p_tradable boolean
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.instruments%ROWTYPE;
        BEGIN
            IF p_symbol IS NULL OR btrim(p_symbol) = '' OR p_asset_class IS NULL
               OR p_currency IS NULL OR p_currency <> upper(btrim(p_currency))
               OR length(p_currency) NOT BETWEEN 3 AND 10
               OR p_currency !~ '^[[:alpha:]]+$' OR p_session_policy IS NULL
               OR p_tradable IS NULL THEN
                RAISE EXCEPTION 'Explicit instrument, currency and session terms required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT * INTO installed FROM public.instruments
            WHERE upper(translate(canonical, '/-_', '')) = upper(translate(p_symbol, '/-_', ''))
            FOR UPDATE;
            IF FOUND THEN
                IF installed.asset_class IS DISTINCT FROM p_asset_class
                   OR installed.settlement_currency IS DISTINCT FROM p_currency
                   OR installed.market_session_policy IS DISTINCT FROM p_session_policy
                   OR installed.is_tradable IS DISTINCT FROM p_tradable THEN
                    RAISE EXCEPTION 'Instrument reference conflict for %', p_symbol
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.instr_id;
            END IF;
            INSERT INTO public.instruments(
                canonical, asset_class, settlement_currency, market_session_policy, is_tradable
            ) VALUES (p_symbol, p_asset_class, p_currency, p_session_policy, p_tradable)
            RETURNING * INTO installed;
            RETURN installed.instr_id;
        END;
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_create_sector(
            p_code text, p_asset_class text, p_parent_code text
        ) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE installed public.sectors%ROWTYPE; parent_key integer;
        BEGIN
            IF p_code IS NULL OR btrim(p_code) = '' OR p_asset_class IS NULL THEN
                RAISE EXCEPTION 'Explicit sector code and asset class required'
                    USING ERRCODE = '22023';
            END IF;
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            IF p_parent_code IS NOT NULL THEN
                SELECT sector_id INTO STRICT parent_key FROM public.sectors
                WHERE code = p_parent_code AND asset_class = p_asset_class;
            END IF;
            SELECT * INTO installed FROM public.sectors WHERE code = p_code FOR UPDATE;
            IF FOUND THEN
                IF installed.asset_class IS DISTINCT FROM p_asset_class
                   OR installed.parent_sector_id IS DISTINCT FROM parent_key THEN
                    RAISE EXCEPTION 'Sector reference conflict for %', p_code
                        USING ERRCODE = '23505';
                END IF;
                RETURN installed.sector_id;
            END IF;
            INSERT INTO public.sectors(code, name, asset_class, parent_sector_id)
            VALUES (p_code, p_code, p_asset_class, parent_key) RETURNING * INTO installed;
            RETURN installed.sector_id;
        END;
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.vm_catalogue_link_sector(p_instr_id integer, p_sector_code text)
        RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $function$
        DECLARE sector_key integer;
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(18472, 1);
            SELECT sector.sector_id INTO STRICT sector_key
            FROM public.sectors sector JOIN public.instruments instrument
              ON instrument.asset_class = sector.asset_class
            WHERE sector.code = p_sector_code AND instrument.instr_id = p_instr_id;
            INSERT INTO public.instrument_sectors(instr_id, sector_id, weight)
            VALUES (p_instr_id, sector_key, 1) ON CONFLICT (instr_id, sector_id) DO NOTHING;
        END;
        $function$;
    """)
    _install_metadata_changes()
    _install_instrument_links()
    for signature in _INTERNAL_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    for signature in _SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO vm_backend")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for signature in reversed(_SIGNATURES):
            op.execute(f"DROP FUNCTION {signature}")
        for signature in reversed(_INTERNAL_SIGNATURES):
            op.execute(f"DROP FUNCTION {signature}")
        op.execute("REVOKE UPDATE (market_calendar_id) ON TABLE public.instruments FROM vm_backend")
        op.execute("GRANT UPDATE ON TABLE public.instruments TO vm_backend")
