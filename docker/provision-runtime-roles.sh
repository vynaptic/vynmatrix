#!/bin/sh
set -eu

# Local-stack counterpart of production database-user provisioning. Alembic
# owns group-role privileges; this script owns only LOGIN roles and passwords.
# Every runtime login is reset to exactly one vm_* group so privilege sets
# cannot silently union across services.

required_variables="
VM_BACKEND_DB_PASSWORD
VM_SCORING_DB_PASSWORD
VM_EXECUTION_DB_PASSWORD
VM_FEEDBACK_DB_PASSWORD
VM_MARKET_DATA_DB_PASSWORD
VM_INDICATOR_DB_PASSWORD
"

for variable_name in $required_variables; do
    eval "variable_value=\${$variable_name:-}"
    if [ -z "$variable_value" ]; then
        echo "[runtime-roles] required variable $variable_name is empty" >&2
        exit 1
    fi
done

provision_login() {
    login_name=$1
    group_name=$2
    role_password=$3

    psql \
        --set=ON_ERROR_STOP=1 \
        --set=login_name="$login_name" \
        --set=group_name="$group_name" \
        --set=role_password="$role_password" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'login_name'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'login_name'
)
\gexec

ALTER ROLE :"login_name"
    LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'role_password';
REVOKE vm_backend, vm_scoring, vm_execution, vm_feedback, vm_market_data, vm_indicator
    FROM :"login_name";
GRANT :"group_name" TO :"login_name";
SQL
}

provision_login vm_backend_login vm_backend "$VM_BACKEND_DB_PASSWORD"
provision_login vm_scoring_login vm_scoring "$VM_SCORING_DB_PASSWORD"
provision_login vm_execution_login vm_execution "$VM_EXECUTION_DB_PASSWORD"
provision_login vm_feedback_login vm_feedback "$VM_FEEDBACK_DB_PASSWORD"
provision_login vm_market_data_login vm_market_data "$VM_MARKET_DATA_DB_PASSWORD"
provision_login vm_indicator_login vm_indicator "$VM_INDICATOR_DB_PASSWORD"

psql --set=ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
    invalid_role text;
BEGIN
    WITH expected(login_name, group_name) AS (
        VALUES
            ('vm_backend_login', 'vm_backend'),
            ('vm_scoring_login', 'vm_scoring'),
            ('vm_execution_login', 'vm_execution'),
            ('vm_feedback_login', 'vm_feedback'),
            ('vm_market_data_login', 'vm_market_data'),
            ('vm_indicator_login', 'vm_indicator')
    ),
    actual AS (
        SELECT
            member.rolname AS login_name,
            parent.rolname AS group_name
        FROM pg_auth_members membership
        JOIN pg_roles parent ON parent.oid = membership.roleid
        JOIN pg_roles member ON member.oid = membership.member
        WHERE member.rolname IN (SELECT login_name FROM expected)
          AND parent.rolname LIKE 'vm\_%' ESCAPE '\'
    ),
    invalid AS (
        SELECT expected.login_name
        FROM expected
        LEFT JOIN actual USING (login_name)
        GROUP BY expected.login_name, expected.group_name
        HAVING count(actual.group_name) <> 1
            OR max(actual.group_name) <> expected.group_name
        UNION
        SELECT role.rolname
        FROM pg_roles role
        WHERE role.rolname IN (SELECT login_name FROM expected)
          AND (
              NOT role.rolcanlogin
              OR role.rolsuper
              OR role.rolcreatedb
              OR role.rolcreaterole
              OR role.rolreplication
              OR role.rolbypassrls
          )
    )
    SELECT string_agg(login_name, ', ' ORDER BY login_name)
    INTO invalid_role
    FROM invalid;

    IF invalid_role IS NOT NULL THEN
        RAISE EXCEPTION 'invalid runtime role configuration: %', invalid_role;
    END IF;
END
$$;
SQL

echo "[runtime-roles] six least-privilege service logins are ready"
