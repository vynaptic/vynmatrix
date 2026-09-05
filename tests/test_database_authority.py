"""Database lifecycle roles fail closed outside PostgreSQL."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_sqlite_is_not_production_lifecycle_authority():
    from lib_application.services.database_authority import (
        DatabaseAuthorityError,
        require_backend_database_role,
        require_maintenance_database_role,
    )

    with Session(create_engine("sqlite://")) as session:
        for checker in (require_backend_database_role, require_maintenance_database_role):
            with pytest.raises(DatabaseAuthorityError, match="PostgreSQL"):
                checker(session)


def _backend_facts():
    return {
        "actor": "vm_backend_login",
        "login": "vm_backend_login",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "table_owner": "vm_maintenance",
        "database_owner": "vm_maintenance",
        "schema_owner": "vm_maintenance",
        "memberships": ["vm_backend"],
        "effective_memberships": ["vm_backend"],
        "membership_edges": [],
        "service_roles": ["vm_backend"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"actor": "vm_backend"},
        {"login": "postgres"},
        {"memberships": ["vm_backend", "other_group"]},
        {"service_roles": ["vm_backend", "vm_execution"]},
        {"rolsuper": True},
        {"rolcreaterole": True},
        {"rolcreatedb": True},
        {"rolreplication": True},
        {"rolbypassrls": True},
        {"table_owner": "vm_backend"},
    ],
)
def test_backend_role_rejects_ambient_or_elevated_authority(monkeypatch, change):
    from lib_application.services import database_authority as authority

    facts = _backend_facts() | change
    monkeypatch.setattr(authority, "_facts", lambda session: facts)
    with pytest.raises(authority.DatabaseAuthorityError):
        authority.require_backend_database_role(object())


def test_backend_role_accepts_only_dedicated_login(monkeypatch):
    from lib_application.services import database_authority as authority

    monkeypatch.setattr(authority, "_facts", lambda session: _backend_facts())
    authority.require_backend_database_role(object())


def test_maintenance_requires_explicit_table_and_schema_owner(monkeypatch):
    from lib_application.services import database_authority as authority

    facts = _backend_facts() | {
        "actor": "vm_maintenance",
        "login": "vm_maintenance",
        "memberships": [],
        "service_roles": [],
    }
    monkeypatch.setattr(authority, "_facts", lambda session: facts)
    authority.require_maintenance_database_role(object())
    facts["service_roles"] = ["vm_execution"]
    facts["membership_edges"] = [
        {"member": "vm_maintenance", "role": "vm_execution", "inherit": True, "set": False}
    ]
    with pytest.raises(authority.DatabaseAuthorityError):
        authority.require_maintenance_database_role(object())


def test_maintenance_accepts_pg_database_owner_schema_only_for_actual_database_owner(monkeypatch):
    from lib_application.services import database_authority as authority

    facts = _backend_facts() | {
        "actor": "vm_maintenance",
        "login": "vm_maintenance",
        "memberships": [],
        "service_roles": [],
        "schema_owner": "pg_database_owner",
        "database_owner": "vm_maintenance",
    }
    monkeypatch.setattr(authority, "_facts", lambda session: facts)
    authority.require_maintenance_database_role(object())
    facts["database_owner"] = "different_owner"
    with pytest.raises(authority.DatabaseAuthorityError):
        authority.require_maintenance_database_role(object())


def test_backend_role_rejects_indirect_unrelated_group(monkeypatch):
    from lib_application.services import database_authority as authority

    facts = _backend_facts() | {"effective_memberships": ["vm_backend", "unrelated_admin"]}
    monkeypatch.setattr(authority, "_facts", lambda session: facts)
    with pytest.raises(authority.DatabaseAuthorityError):
        authority.require_backend_database_role(object())


@pytest.mark.parametrize(
    ("inherit", "set_role", "allowed"),
    [(False, False, True), (True, False, False), (False, True, False), (True, True, False)],
)
def test_maintenance_allows_only_administration_only_runtime_membership(
    monkeypatch, inherit, set_role, allowed
):
    from lib_application.services import database_authority as authority

    facts = _backend_facts() | {
        "actor": "vm_maintenance",
        "login": "vm_maintenance",
        "memberships": ["vm_execution"],
        "service_roles": ["vm_execution"],
        "membership_edges": [
            {
                "member": "vm_maintenance",
                "role": "vm_execution",
                "inherit": inherit,
                "set": set_role,
            }
        ],
    }
    monkeypatch.setattr(authority, "_facts", lambda session: facts)
    if allowed:
        authority.require_maintenance_database_role(object())
    else:
        with pytest.raises(authority.DatabaseAuthorityError):
            authority.require_maintenance_database_role(object())


@pytest.mark.parametrize(
    ("edges", "expected"),
    [
        (
            [
                {"member": "maint", "role": "middle", "inherit": False, "set": True},
                {"member": "middle", "role": "vm_execution", "inherit": True, "set": False},
            ],
            {"vm_execution"},
        ),
        (
            [
                {"member": "maint", "role": "middle", "inherit": True, "set": False},
                {"member": "middle", "role": "vm_execution", "inherit": False, "set": True},
            ],
            set(),
        ),
        (
            [
                {"member": "maint", "role": "middle", "inherit": False, "set": False},
                {"member": "middle", "role": "vm_execution", "inherit": True, "set": True},
            ],
            set(),
        ),
    ],
)
def test_runtime_privilege_paths_follow_set_then_inheritance(edges, expected):
    from lib_application.services.database_authority import _usable_service_roles

    assert _usable_service_roles("maint", edges) == expected


def test_historical_vm_app_grant_is_also_runtime_authority():
    from lib_application.services.database_authority import _usable_service_roles

    edges = [{"member": "maint", "role": "vm_app", "inherit": True, "set": False}]
    assert _usable_service_roles("maint", edges) == {"vm_app"}
