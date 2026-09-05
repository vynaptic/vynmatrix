"""Post-schema lifecycle authority checks. Never used for fresh schema bootstrap."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

_SERVICE_ROLES = frozenset(
    {"vm_backend", "vm_scoring", "vm_execution", "vm_feedback", "vm_market_data", "vm_indicator"}
)


class DatabaseAuthorityError(ValueError):
    """The connection lacks the narrowly assigned lifecycle authority."""


def _usable_service_roles(actor: str, edges: Sequence[Mapping[str, Any]]) -> set[str]:
    """Resolve SET ROLE paths, then privileges inherited from each reachable role.

    PostgreSQL 16 ADMIN-only grants are intentionally excluded. Administrative
    capability belongs to maintenance; runtime privileges must not be ambient.
    """
    reachable = {actor}
    for option in ("set", "inherit"):
        while True:
            expanded = reachable | {
                edge["role"] for edge in edges if edge[option] and edge["member"] in reachable
            }
            if expanded == reachable:
                break
            reachable = expanded
    return reachable & (_SERVICE_ROLES | {"vm_app"})


def _facts(session: Session) -> RowMapping | None:
    if session.get_bind().dialect.name != "postgresql":
        msg = "Lifecycle operations require PostgreSQL"
        raise DatabaseAuthorityError(msg)
    return (
        session.execute(
            text("""
        WITH RECURSIVE assigned(roleid) AS (
          SELECT m.roleid FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles actor ON actor.oid=m.member
          WHERE actor.rolname=current_user
          UNION
          SELECT m.roleid FROM pg_catalog.pg_auth_members m
          JOIN assigned a ON a.roleid=m.member
        )
        SELECT current_user AS actor, session_user AS login,
          r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls,
          pg_catalog.pg_get_userbyid(c.relowner) AS table_owner,
          pg_catalog.pg_get_userbyid(n.nspowner) AS schema_owner,
          pg_catalog.pg_get_userbyid(d.datdba) AS database_owner,
          COALESCE((SELECT pg_catalog.json_agg(pg_catalog.json_build_object(
            'member', child.rolname, 'role', parent.rolname,
            'inherit', m.inherit_option, 'set', m.set_option))
            FROM pg_catalog.pg_auth_members m
            JOIN pg_catalog.pg_roles child ON child.oid=m.member
            JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid
            WHERE m.member=r.oid OR m.member IN (SELECT roleid FROM assigned)), '[]'::json)
            AS membership_edges,
          ARRAY(SELECT parent.rolname FROM pg_catalog.pg_auth_members m
                JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid
                WHERE m.member=r.oid) AS memberships,
          ARRAY(SELECT g.rolname FROM pg_catalog.pg_roles g JOIN assigned a ON a.roleid=g.oid)
            AS effective_memberships,
          ARRAY(SELECT g.rolname FROM pg_catalog.pg_roles g WHERE g.rolname IN
            ('vm_backend','vm_scoring','vm_execution','vm_feedback','vm_market_data','vm_indicator')
            AND (g.oid=r.oid OR g.oid IN (SELECT roleid FROM assigned))) AS service_roles
        FROM pg_catalog.pg_roles r
        JOIN pg_catalog.pg_class c ON c.oid=pg_catalog.to_regclass('public.users')
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_database d ON d.datname=pg_catalog.current_database()
        WHERE r.rolname=current_user
    """)
        )
        .mappings()
        .one_or_none()
    )


def require_backend_database_role(session: Session) -> None:
    """Require the dedicated backend login with only its service-group membership."""
    facts = _facts(session)
    if facts is None or (
        facts["actor"] != "vm_backend_login"
        or facts["login"] != "vm_backend_login"
        or set(facts["memberships"]) != {"vm_backend"}
        or set(facts["service_roles"]) != {"vm_backend"}
        or set(facts["effective_memberships"]) != {"vm_backend"}
        or any(
            facts[key]
            for key in (
                "rolsuper",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "rolbypassrls",
            )
        )
        or facts["table_owner"] in {"vm_backend_login", "vm_backend"}
        or facts["schema_owner"] in {"vm_backend_login", "vm_backend"}
        or facts["database_owner"] in {"vm_backend_login", "vm_backend"}
    ):
        msg = (
            "Routine lifecycle requires vm_backend_login with only vm_backend membership "
            "and no elevated authority"
        )
        raise DatabaseAuthorityError(msg)


def require_maintenance_database_role(session: Session) -> None:
    """Require post-schema ownership without inherited or settable runtime roles."""
    facts = _facts(session)
    if facts is None or (
        facts["actor"] != facts["login"]
        or facts["actor"] in {"vm_backend_login", "vm_backend"}
        or facts["actor"] != facts["table_owner"]
        or (
            facts["actor"] != facts["schema_owner"]
            and not (
                facts["schema_owner"] == "pg_database_owner"
                and facts["database_owner"] == facts["actor"]
            )
        )
        or _usable_service_roles(facts["actor"], facts["membership_edges"])
    ):
        msg = (
            "Owner initialization requires the post-schema maintenance owner, "
            "without inherited or settable runtime privileges"
        )
        raise DatabaseAuthorityError(msg)
