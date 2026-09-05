"""Owner-relative lifecycle CLI boundaries."""

import importlib
import json
from contextlib import contextmanager

import click
import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, User

user_module = importlib.import_module("dev_cli.commands.user")


@pytest.fixture
def session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:

        @contextmanager
        def injected(stage):
            yield session

        monkeypatch.setattr(user_module, "_database_session", injected)
        yield session
    engine.dispose()


def test_owner_init_show_and_no_commercial_user_selection(session):
    runner = CliRunner()
    args = [
        "init",
        "--email",
        "owner@example.invalid",
        "--base-currency",
        "EUR",
        "--timezone",
        "UTC",
    ]
    result = runner.invoke(user_module.user, args)
    assert result.exit_code == 0, result.output
    owner_id = json.loads(result.output)["user_id"]
    assert runner.invoke(user_module.user, args).exit_code == 0
    assert json.loads(runner.invoke(user_module.user, ["show"]).output)["user_id"] == owner_id
    assert runner.invoke(user_module.user, ["show", owner_id]).exit_code != 0
    assert runner.invoke(user_module.user, ["add"]).exit_code != 0
    assert len(session.scalars(select(User)).all()) == 1


def test_no_generic_database_url_fallback(monkeypatch):
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    result = CliRunner().invoke(user_module.user, ["init"])
    assert result.exit_code != 0
    assert "MIGRATION_DATABASE_URL" in result.output


def test_production_sqlite_rejected(monkeypatch):
    monkeypatch.setenv("BACKEND_DATABASE_URL", "sqlite://")
    result = CliRunner().invoke(user_module.user, ["show"])
    assert result.exit_code != 0
    assert "PostgreSQL" in result.output


def test_profile_update_changes_only_supplied_fields(session, tmp_path):
    runner = CliRunner()
    runner.invoke(
        user_module.user,
        ["init", "--email", "owner@example.invalid", "--base-currency", "EUR", "--timezone", "UTC"],
    )
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"expected": {"full_name": None}, "changes": {"full_name": "Owner"}})
    )
    result = runner.invoke(user_module.user, ["update", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["full_name"] == "Owner"
    assert json.loads(result.output)["base_ccy"] == "EUR"


def test_account_config_key_retry_preserves_account_and_rejects_public_secrets(
    session, tmp_path, monkeypatch
):
    from lib_application.db.models import Broker, BrokerEnvironment

    session.add(Broker(broker_id=1, code="paper", name="Paper"))
    session.add(BrokerEnvironment(broker_id=1, environment="paper", region="global", base_urls={}))
    session.commit()

    class Writer:
        def set_secret(self, secret_ref, plaintext, *, account_id, session):
            pass

    monkeypatch.setattr(
        "lib_infrastructure.brokers.secrets.create_secrets_provider", lambda **kwargs: Writer()
    )
    runner = CliRunner()
    runner.invoke(
        user_module.user,
        ["init", "--email", "owner@example.invalid", "--base-currency", "EUR", "--timezone", "UTC"],
    )
    path = tmp_path / "account.json"
    config = {
        "config_key": "paper-main",
        "broker_code": "paper",
        "environment": "paper",
        "base_ccy": "EUR",
        "paper_initial_equity": 100,
        "paper_initial_cash": 100,
    }
    path.write_text(json.dumps(config))
    first = runner.invoke(user_module.user, ["account", "--config", str(path)])
    assert first.exit_code == 0, first.output
    again = runner.invoke(user_module.user, ["account", "--config", str(path)])
    assert again.exit_code == 0, again.output
    assert json.loads(first.output)["account_id"] == json.loads(again.output)["account_id"]
    config["credentials"] = {"api_key": "never-print-this-secret"}
    path.write_text(json.dumps(config))
    denied = runner.invoke(user_module.user, ["account", "--config", str(path)])
    assert denied.exit_code != 0
    assert "never-print-this-secret" not in denied.output
    assert "--secrets-file" in denied.output


def test_secret_file_permissions_and_validation_never_echo_payload(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text('{"api_key":"private-material"}')
    path.chmod(0o644)
    with pytest.raises(click.ClickException, match="owner-only"):
        user_module._load_mapping(str(path), protected=True)
    path.chmod(0o600)
    assert user_module._load_mapping(str(path), protected=True)["api_key"] == "private-material"
    link = tmp_path / "symlink"
    link.symlink_to(path)
    with pytest.raises(click.ClickException, match="Unable to read"):
        user_module._load_mapping(str(link), protected=True)


def test_account_update_uses_stable_key_and_expected_values(session, tmp_path):
    from lib_application.db.models import Broker, LinkedBrokerAccount

    runner = CliRunner()
    initialized = runner.invoke(
        user_module.user,
        ["init", "--email", "owner@example.invalid", "--base-currency", "EUR", "--timezone", "UTC"],
    )
    owner_id = json.loads(initialized.output)["user_id"]
    session.add(Broker(broker_id=1, code="paper", name="Paper"))
    session.add(
        LinkedBrokerAccount(
            account_id=1,
            user_id=owner_id,
            broker_id=1,
            config_key="paper-main",
            environment="paper",
            base_ccy="EUR",
            display_name="Original",
            paper_initial_equity=100,
            paper_initial_cash=100,
        )
    )
    session.commit()
    path = tmp_path / "patch.json"
    path.write_text(
        json.dumps(
            {
                "config_key": "paper-main",
                "expected": {"display_name": "Original"},
                "changes": {"display_name": "Updated"},
            }
        )
    )
    result = runner.invoke(user_module.user, ["account", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["account_id"] == 1
    assert session.get(LinkedBrokerAccount, 1).display_name == "Updated"


def test_cli_command_import_does_not_require_built_application_libraries():
    import subprocess
    import sys

    source = str(user_module.__file__)
    script = """import importlib.abc, runpy, sys
class UnbuiltLibraries(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("lib_application", "lib_infrastructure")):
            raise ModuleNotFoundError("Application wheels are not installed")
sys.meta_path.insert(0, UnbuiltLibraries())
runpy.run_path(sys.argv[1], run_name="unbuilt_cli_command")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_real_user_command_loads_checkout_libraries_before_database_validation(monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.delenv("BACKEND_DATABASE_URL", raising=False)
    result = subprocess.run(
        [sys.executable, "-m", "dev_cli.main", "user", "show"],
        cwd=Path(__file__).resolve().parents[3],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "BACKEND_DATABASE_URL is required" in result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stdout + result.stderr


@pytest.mark.parametrize("capability", ["O_NOFOLLOW", "geteuid"])
def test_secret_file_without_posix_protection_fails_with_stdin_alternative(
    tmp_path, monkeypatch, capability
):
    monkeypatch.delattr(user_module.os, capability, raising=False)
    path = tmp_path / "secrets.json"
    path.write_text('{"api_key":"private-material"}')
    with pytest.raises(click.ClickException, match="protected redirected stdin") as error:
        user_module._load_mapping(str(path), protected=True)
    assert "private-material" not in str(error.value)


def test_protected_redirected_stdin_does_not_require_posix_apis(monkeypatch):
    import io

    monkeypatch.delattr(user_module.os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(user_module.os, "geteuid", raising=False)
    monkeypatch.setattr(user_module.sys, "stdin", io.StringIO('{"api_key":"private-material"}'))
    assert user_module._load_mapping("-", protected=True) == {"api_key": "private-material"}
