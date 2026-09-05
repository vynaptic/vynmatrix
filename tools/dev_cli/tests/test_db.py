"""The CLI uses the canonical lifecycle and never revives destructive seed/reset paths."""

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

db_module = importlib.import_module("dev_cli.commands.db")


def test_explicit_environment_overrides_project_defaults(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DB_NAME=project_default\nDB_PORT=5432\n")
    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DB_NAME", "explicit_verification_target")
    monkeypatch.delenv("DB_PORT", raising=False)
    environment = db_module._compose_env()
    assert environment["DB_NAME"] == "explicit_verification_target"
    assert environment["DB_PORT"] == "5432"


def test_load_project_env_parses_comments_and_quotes(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("# comment\nENV=dev  # inline\nDB_USER='owner'\n")
    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)
    assert db_module._load_project_env() == {"ENV": "dev", "DB_USER": "owner"}


def test_start_only_selects_postgres(monkeypatch) -> None:
    calls = []

    class Lifecycle:
        def command(self, *args):
            calls.append(args)

    monkeypatch.setattr(db_module, "_lifecycle", Lifecycle)
    result = CliRunner().invoke(db_module.db, ["start"])
    assert result.exit_code == 0, result.output
    assert calls == [("up", "-d", "--wait", "postgres")]


def test_stop_drains_runtime_before_database(monkeypatch) -> None:
    calls = []

    class Lifecycle:
        def stop_runtime(self):
            calls.append("drain")

        def command(self, *args):
            calls.append(args)

    monkeypatch.setattr(db_module, "_lifecycle", Lifecycle)
    result = CliRunner().invoke(db_module.db, ["stop"])
    assert result.exit_code == 0, result.output
    assert calls == ["drain", ("stop", "--timeout", "60", "postgres")]


def test_retired_destructive_and_extra_container_commands_are_absent() -> None:
    runner = CliRunner()
    for command in ("reset", "init", "pgadmin"):
        result = runner.invoke(db_module.db, [command])
        assert result.exit_code == 2
        assert "No such command" in result.output


def test_bootstrap_requires_reviewed_owner_input_before_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(
        db_module, "_lifecycle", lambda: (_ for _ in ()).throw(AssertionError("side effect"))
    )
    result = CliRunner().invoke(db_module.db, ["bootstrap"])
    assert result.exit_code == 1
    assert "--owner-config" in result.output


def test_catalogue_requires_one_explicit_operation() -> None:
    runner = CliRunner()
    for arguments in ([], ["--check", "--apply"]):
        result = runner.invoke(db_module.db, ["catalogue", *arguments])
        assert result.exit_code == 2, result.output


def test_canary_command_is_registered_with_required_exact_release() -> None:
    runner = CliRunner()
    help_result = runner.invoke(db_module.db, ["activate-canary", "--help"])
    assert help_result.exit_code == 0, help_result.output
    missing_release = runner.invoke(db_module.db, ["activate-canary", "--strategy-id", "canary"])
    assert missing_release.exit_code == 2
    assert "--version" in missing_release.output


def test_roles_rejects_invalid_url_without_exposing_credential_text(monkeypatch) -> None:
    secret = "unparseable-private-credential"
    monkeypatch.setattr(db_module, "_compose_env", lambda: {"MIGRATION_DATABASE_URL": secret})
    result = CliRunner().invoke(db_module.db, ["roles"])
    assert result.exit_code == 1
    assert "Error: Invalid database role configuration" in result.output
    assert secret not in result.output


def test_catalogue_uncertain_commit_reopens_session_and_rechecks_stable_state(monkeypatch) -> None:
    user_module = importlib.import_module("dev_cli.commands.user")
    catalogue_module = importlib.import_module("dev_cli.core.catalogue")
    state = {"exists": False, "audit_rows": 0}
    sessions = []
    delays = []

    class Session:
        def commit(self):
            if len(sessions) == 1:
                # The durable commit succeeded, but the transport lost its response.
                raise OperationalError(
                    None, None, Exception("disconnect"), connection_invalidated=True
                )

    @contextmanager
    def database_session(stage):
        assert stage == "backend"
        session = Session()
        sessions.append(session)
        yield session

    def reconcile(session, sources, *, apply):
        assert session is sessions[-1]
        assert apply
        changed = not state["exists"]
        if changed:
            state["exists"] = True
            state["audit_rows"] += 1
        return {"changed": changed}

    monkeypatch.setattr(user_module, "_database_session", database_session)
    monkeypatch.setattr(catalogue_module, "load_catalogue", lambda *args, **kwargs: object())
    monkeypatch.setattr(catalogue_module, "reconcile_catalogue", reconcile)
    monkeypatch.setattr(db_module.time, "sleep", delays.append)
    result = CliRunner().invoke(db_module.db, ["catalogue", "--apply"])
    assert result.exit_code == 0, result.output
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]
    assert state == {"exists": True, "audit_rows": 1}
    assert delays == [0.1]
    assert '"changed": false' in result.output


def test_platform_processes_import_does_not_depend_on_caller_sys_path(monkeypatch) -> None:
    """The console script has no repository root on sys.path; the helper must add it."""
    root = str(db_module.PROJECT_ROOT)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != root])
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        monkeypatch.delitem(sys.modules, name)

    module = db_module._platform_processes()

    assert callable(module.build_processes)
    assert "MIGRATION_DATABASE_URL" in module._MAINTENANCE
    assert sys.path[0] == root
