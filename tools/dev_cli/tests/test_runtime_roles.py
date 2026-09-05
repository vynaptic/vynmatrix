"""Runtime login provisioning preserves installed authority and credentials."""

from __future__ import annotations

import pytest


def _role(*, login=False, memberships=None, **changes):
    return {
        "rolcanlogin": login,
        "rolinherit": True,
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "owns_objects": False,
        "memberships": memberships or [],
        **changes,
    }


def _catalog():
    from dev_cli.core.runtime_roles import RUNTIME_ROLES

    catalog = {}
    for login, group in RUNTIME_ROLES:
        catalog[group] = _role()
        catalog[login] = _role(
            login=True, memberships=[{"role": group, "inherit": True, "set": True, "admin": False}]
        )
    return catalog


def test_existing_six_roles_are_verified_without_password_replacement():
    from dev_cli.core.runtime_roles import plan_runtime_roles

    plan = plan_runtime_roles(_catalog(), rotate=False)
    assert plan.create == ()
    assert len(plan.authenticate) == 6
    assert plan.rotate == ()


def test_missing_roles_are_created_and_existing_roles_preserved():
    from dev_cli.core.runtime_roles import plan_runtime_roles

    catalog = _catalog()
    del catalog["vm_execution_login"]
    plan = plan_runtime_roles(catalog, rotate=False)
    assert plan.create == ("vm_execution_login",)
    assert len(plan.authenticate) == 5


@pytest.mark.parametrize(
    "change",
    [
        {"rolsuper": True},
        {"rolcreatedb": True},
        {"rolcreaterole": True},
        {"rolreplication": True},
        {"rolbypassrls": True},
        {"owns_objects": True},
        {"memberships": []},
        {"memberships": [{"role": "vm_execution", "inherit": True, "set": True, "admin": False}]},
    ],
)
def test_existing_bad_role_is_rejected_without_repair(change):
    from dev_cli.core.runtime_roles import RuntimeRoleError, plan_runtime_roles

    catalog = _catalog()
    catalog["vm_backend_login"].update(change)
    with pytest.raises(RuntimeRoleError, match="vm_backend_login"):
        plan_runtime_roles(catalog, rotate=False)


def test_rotation_is_explicit_and_does_not_repair_invalid_groups():
    from dev_cli.core.runtime_roles import RuntimeRoleError, plan_runtime_roles

    plan = plan_runtime_roles(_catalog(), rotate=True)
    assert len(plan.rotate) == 6
    assert plan.authenticate == ()
    catalog = _catalog()
    catalog["vm_backend"]["rolsuper"] = True
    with pytest.raises(RuntimeRoleError, match="vm_backend"):
        plan_runtime_roles(catalog, rotate=True)


@pytest.fixture
def provisioning(monkeypatch):
    from contextlib import contextmanager

    from dev_cli.core import runtime_roles as roles

    events = []
    catalog = _catalog()

    class Session:
        @contextmanager
        def begin(self):
            events.append("begin")
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        def execute(self, *args, **kwargs):
            return None

    @contextmanager
    def opened(url):
        yield Session()

    monkeypatch.setattr(roles, "_session", opened)
    monkeypatch.setattr(roles, "_require_admin", lambda session, login: None)
    monkeypatch.setattr(roles, "_catalog", lambda session: catalog)
    monkeypatch.setattr(
        roles, "_authenticate", lambda url, *, maintenance=False: events.append("authenticate")
    )
    monkeypatch.setattr(
        roles,
        "_create_login",
        lambda session, login, group, password: events.append(("create", login)),
    )
    monkeypatch.setattr(
        roles, "_rotate_login", lambda session, login, password: events.append(("rotate", login))
    )
    return roles, events, catalog


def _passwords():
    from dev_cli.core.runtime_roles import RUNTIME_ROLES

    return {login: "fixture-password" for login, _ in RUNTIME_ROLES}


def test_provisioning_reauthenticates_existing_passwords_and_performs_no_ddl(provisioning):
    roles, events, _ = provisioning
    result = roles.provision_runtime_roles(
        "postgresql://admin:fixture@localhost/test",
        "postgresql://migration:fixture@localhost/test",
        _passwords(),
    )
    assert result == {"created": 0, "verified": 6, "rotated": 0}
    assert events.count("authenticate") == 7
    assert not any(isinstance(event, tuple) for event in events)
    assert events[-1] == "commit"


def test_password_authentication_failure_precedes_any_role_mutations(provisioning, monkeypatch):
    roles, events, catalog = provisioning
    del catalog["vm_backend_login"]

    def denied(url, *, maintenance=False):
        if not maintenance:
            raise roles.RuntimeRoleError("Authentication failed")

    monkeypatch.setattr(roles, "_authenticate", denied)
    with pytest.raises(roles.RuntimeRoleError, match="Authentication"):
        roles.provision_runtime_roles(
            "postgresql://admin:fixture@localhost/test",
            "postgresql://migration:fixture@localhost/test",
            _passwords(),
        )
    assert events == ["begin", "rollback"]


def test_all_missing_role_creates_share_one_rollback_boundary(provisioning, monkeypatch):
    roles, events, catalog = provisioning
    for login, _ in roles.RUNTIME_ROLES:
        del catalog[login]

    def create(session, login, group, password):
        events.append(("create", login))
        if login == "vm_execution_login":
            raise roles.RuntimeRoleError("Injected creation failure")

    monkeypatch.setattr(roles, "_create_login", create)
    with pytest.raises(roles.RuntimeRoleError):
        roles.provision_runtime_roles(
            "postgresql://admin:fixture@localhost/test",
            "postgresql://migration:fixture@localhost/test",
            _passwords(),
        )
    assert events[-1] == "rollback"
    assert "commit" not in events


def test_provisioning_refuses_different_database_targets(provisioning):
    roles, events, _ = provisioning
    with pytest.raises(roles.RuntimeRoleError, match="same"):
        roles.provision_runtime_roles(
            "postgresql://admin:fixture@localhost/other",
            "postgresql://migration:fixture@localhost/test",
            _passwords(),
        )
    assert events == []


def test_explicit_rotation_changes_passwords_in_one_transaction(provisioning):
    roles, events, _ = provisioning
    result = roles.provision_runtime_roles(
        "postgresql://admin:fixture@localhost/test",
        "postgresql://migration:fixture@localhost/test",
        _passwords(),
        rotate=True,
    )
    assert result == {"created": 0, "verified": 0, "rotated": 6}
    assert events.count("authenticate") == 1
    assert (
        len([event for event in events if isinstance(event, tuple) and event[0] == "rotate"]) == 6
    )
    assert events[-1] == "commit"


def test_driver_failure_is_sanitized_and_rolls_back(provisioning, monkeypatch):
    import psycopg2

    roles, events, _ = provisioning

    def rejected(session, login, password):
        raise psycopg2.OperationalError("private-fixture-material")

    monkeypatch.setattr(roles, "_rotate_login", rejected)
    with pytest.raises(roles.RuntimeRoleError) as caught:
        roles.provision_runtime_roles(
            "postgresql://admin:fixture@localhost/test",
            "postgresql://migration:fixture@localhost/test",
            _passwords(),
            rotate=True,
        )
    assert "private-fixture-material" not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert events[-1] == "rollback"


def test_password_ddl_uses_driver_parameters_without_sqlalchemy_logging():
    from types import SimpleNamespace

    from dev_cli.core.runtime_roles import _create_login, _rotate_login

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, parameters=None):
            statements.append((statement, parameters))

    driver = SimpleNamespace(cursor=lambda: Cursor())
    session = SimpleNamespace(
        connection=lambda: SimpleNamespace(connection=SimpleNamespace(driver_connection=driver))
    )
    secret = "fixture'credential;with-special-characters"
    _create_login(session, "vm_backend_login", "vm_backend", secret)
    _rotate_login(session, "vm_backend_login", secret)
    assert sum(parameters == (secret,) for _, parameters in statements) == 2
    assert all(secret not in repr(statement) for statement, _ in statements)


def test_incomplete_postcondition_rolls_back_creation(provisioning):
    roles, events, catalog = provisioning
    del catalog["vm_backend_login"]
    with pytest.raises(roles.RuntimeRoleError, match="incomplete"):
        roles.provision_runtime_roles(
            "postgresql://admin:fixture@localhost/test",
            "postgresql://migration:fixture@localhost/test",
            _passwords(),
        )
    assert events[-1] == "rollback"
