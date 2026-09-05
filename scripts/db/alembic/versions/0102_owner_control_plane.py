"""Restrict backend configuration to the designated owner and freeze used account terms.

Revision ID: 0102_owner_control_plane
Revises: 0101_reference_catalogue
"""

from __future__ import annotations

from alembic import op

revision = "0102_owner_control_plane"
down_revision = "0101_reference_catalogue"
branch_labels = None
depends_on = None

_PROFILE_COLUMNS = "email, full_name, tz, base_ccy"
_ACCOUNT_COLUMNS = (
    "config_key, display_name, status, external_ref, base_ccy, "
    "paper_initial_equity, paper_initial_cash"
)
_OWNER = "public.vm_deployment_owner_id()"
_SCOPED_OWNER = f"user_id = {_OWNER} AND user_id = current_setting('app.current_tenant', true)"
_ACCOUNT_OWNER = (
    "account_id IN (SELECT a.account_id FROM public.linked_broker_accounts AS a "
    f"WHERE a.user_id = {_OWNER} "
    "AND a.user_id = current_setting('app.current_tenant', true))"
)
_RESTRICTED_TABLES = {
    "users": _SCOPED_OWNER,
    "linked_broker_accounts": _SCOPED_OWNER,
    "user_strategy_bindings": _SCOPED_OWNER,
    "user_strategy_configs": _SCOPED_OWNER,
    "risk_mandates": _SCOPED_OWNER,
    "api_audit_logs": _SCOPED_OWNER,
    "broker_credentials": _ACCOUNT_OWNER,
    "managed_secrets": _ACCOUNT_OWNER,
}


def _install_activity_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION public.vm_owner_account_has_activity(p_account_id bigint)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        DECLARE
            owner_id text := public.vm_deployment_owner_id();
        BEGIN
            IF owner_id IS NULL
               OR owner_id IS DISTINCT FROM current_setting('app.current_tenant', true)
               OR NOT EXISTS (
                   SELECT 1 FROM public.linked_broker_accounts AS a
                   WHERE a.account_id = p_account_id AND a.user_id = owner_id
               ) THEN
                RAISE EXCEPTION 'designated owner account scope is required'
                    USING ERRCODE = '42501';
            END IF;
            -- Same key/hash as AccountExecutionSerializer: transaction and session
            -- advisory locks conflict, preventing a check/new-submission race.
            IF NOT pg_try_advisory_xact_lock(hashtextextended(
                'execution-account:' || owner_id || ':' || p_account_id::text, 0
            )) THEN
                RAISE EXCEPTION 'account has execution in progress' USING ERRCODE = '55P03';
            END IF;
            RETURN
                EXISTS (SELECT 1 FROM public.order_intents WHERE account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.orders WHERE account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.pending_orders
                           WHERE broker_account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.execution_logs WHERE account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.daily_nav WHERE account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.positions WHERE account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.account_rebalance_plans
                           WHERE broker_account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.execution_decision_logs
                           WHERE broker_account_id = p_account_id)
                OR EXISTS (SELECT 1 FROM public.account_execution_generations
                           WHERE broker_account_id = p_account_id AND active_owner IS NOT NULL)
                OR EXISTS (
                    SELECT 1 FROM public.outbox_events AS e
                    WHERE e.topic IN ('execution.commands', 'execution.rebalance.commands')
                      AND e.status <> 'published'
                      AND (
                        e.payload #>> '{broker_route,broker_account_id}' = p_account_id::text
                        OR (
                          e.payload->>'user_id' = owner_id
                          AND e.payload #>> '{broker_route,broker_account_id}' IS NULL
                        )
                      )
                );
        END;
        $function$;
        REVOKE ALL ON FUNCTION public.vm_owner_account_has_activity(bigint) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.vm_owner_account_has_activity(bigint) TO vm_backend;

        CREATE FUNCTION public.vm_guard_account_financial_terms()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
        BEGIN
            IF public.vm_owner_account_has_activity(OLD.account_id) THEN
                RAISE EXCEPTION 'account financial identity is immutable after execution activity'
                    USING ERRCODE = '23514', CONSTRAINT = 'guard_account_financial_terms';
            END IF;
            RETURN NEW;
        END;
        $function$;
        REVOKE ALL ON FUNCTION public.vm_guard_account_financial_terms() FROM PUBLIC;

        CREATE TRIGGER guard_account_financial_terms
        BEFORE UPDATE OF base_ccy, external_ref, paper_initial_equity, paper_initial_cash
        ON public.linked_broker_accounts
        FOR EACH ROW WHEN (
            OLD.base_ccy IS DISTINCT FROM NEW.base_ccy
            OR OLD.external_ref IS DISTINCT FROM NEW.external_ref
            OR OLD.paper_initial_equity IS DISTINCT FROM NEW.paper_initial_equity
            OR OLD.paper_initial_cash IS DISTINCT FROM NEW.paper_initial_cash
        ) EXECUTE FUNCTION public.vm_guard_account_financial_terms();
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("GRANT SELECT ON TABLE public.users TO vm_backend")
    op.execute(f"GRANT UPDATE ({_PROFILE_COLUMNS}) ON TABLE public.users TO vm_backend")
    op.execute(
        f"GRANT UPDATE ({_ACCOUNT_COLUMNS}) ON TABLE public.linked_broker_accounts TO vm_backend"
    )
    op.execute(
        "CREATE POLICY users_backend_select ON public.users FOR SELECT TO vm_backend "
        f"USING ({_SCOPED_OWNER})"
    )
    op.execute(
        "CREATE POLICY users_backend_update ON public.users FOR UPDATE TO vm_backend "
        f"USING ({_SCOPED_OWNER}) WITH CHECK ({_SCOPED_OWNER})"
    )
    op.execute(
        "CREATE POLICY linked_broker_accounts_backend_update ON public.linked_broker_accounts "
        f"FOR UPDATE TO vm_backend USING ({_SCOPED_OWNER}) WITH CHECK ({_SCOPED_OWNER})"
    )
    # Restrictive policies AND the existing command policies. They never add
    # privileges, and forging the former tenant GUC cannot select another user.
    for table, predicate in _RESTRICTED_TABLES.items():
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_backend_deployment_owner ON public.{table} "
            f"AS RESTRICTIVE FOR ALL TO vm_backend USING ({predicate}) WITH CHECK ({predicate})"
        )
    _install_activity_guard()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER guard_account_financial_terms ON public.linked_broker_accounts")
    op.execute("DROP FUNCTION public.vm_guard_account_financial_terms()")
    op.execute("DROP FUNCTION public.vm_owner_account_has_activity(bigint)")
    for table in _RESTRICTED_TABLES:
        op.execute(f"DROP POLICY {table}_backend_deployment_owner ON public.{table}")
    op.execute("DROP POLICY linked_broker_accounts_backend_update ON public.linked_broker_accounts")
    op.execute("DROP POLICY users_backend_update ON public.users")
    op.execute("DROP POLICY users_backend_select ON public.users")
    op.execute("REVOKE SELECT ON TABLE public.users FROM vm_backend")
    op.execute(f"REVOKE UPDATE ({_PROFILE_COLUMNS}) ON TABLE public.users FROM vm_backend")
    op.execute(
        f"REVOKE UPDATE ({_ACCOUNT_COLUMNS}) ON TABLE public.linked_broker_accounts FROM vm_backend"
    )
