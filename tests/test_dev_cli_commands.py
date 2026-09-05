from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_CLI_ROOT = REPO_ROOT / "tools" / "dev_cli"
if str(DEV_CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(DEV_CLI_ROOT))

test_module = importlib.import_module("dev_cli.commands.test")
lint_module = importlib.import_module("dev_cli.commands.lint")
main_module = importlib.import_module("dev_cli.main")


def test_test_all_uses_pytest_configured_paths(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run_pytest(args: list[str]) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(test_module, "_run_pytest", fake_run_pytest)

    result = CliRunner().invoke(test_module.test, ["all"])

    assert result.exit_code == 0
    assert calls == [[]]


def test_test_all_propagates_pytest_failures(monkeypatch) -> None:
    monkeypatch.setattr(test_module, "_run_pytest", lambda _args: 2)

    result = CliRunner().invoke(test_module.test, ["all"])

    assert result.exit_code == 2


def test_pytest_env_drops_operator_pythonpath(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/tmp/source-shadow")

    assert "PYTHONPATH" not in test_module._pytest_env()


def test_lint_command_is_registered() -> None:
    result = CliRunner().invoke(main_module.cli, ["--help"])

    assert result.exit_code == 0
    assert "lint" in result.output


def test_base_cli_does_not_import_validation_command_during_bootstrap() -> None:
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'dev_cli.commands.strategy':
        raise AssertionError('strategy command imported during base CLI bootstrap')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from dev_cli.main import cli
from click.testing import CliRunner
result = CliRunner().invoke(cli, ['build', '--help'])
assert result.exit_code == 0, result.output
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(DEV_CLI_ROOT)

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
    )


def test_lint_propagates_failures(monkeypatch) -> None:
    monkeypatch.setattr(lint_module, "_run", lambda _label, _cmd: 1)

    result = CliRunner().invoke(lint_module.lint, ["--no-mypy"])

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Smoke coverage for ``vmdev db`` / ``vmdev user`` / ``vmdev git`` groups.
# These groups were undocumented in CLAUDE.md prior to the audit and went
# untested. The tests below intentionally exercise only registration and the
# ``--help`` surface — wiring tests for individual subcommands belong with the
# subsystems they call (Postgres, gh, etc.) and require integration setup.
# ---------------------------------------------------------------------------

db_module = importlib.import_module("dev_cli.commands.db")
user_module = importlib.import_module("dev_cli.commands.user")
git_module = importlib.import_module("dev_cli.commands.git")


def _registered_top_level_commands() -> set[str]:
    return set(main_module.cli.commands.keys())


def test_db_group_is_registered_on_top_level_cli() -> None:
    assert "db" in _registered_top_level_commands()


def test_db_group_help_lists_subcommands() -> None:
    result = CliRunner().invoke(main_module.cli, ["db", "--help"])

    assert result.exit_code == 0
    # ``vmdev db`` should expose a non-empty subcommand surface.
    assert db_module.db.commands, "expected at least one db subcommand"


def test_user_group_is_registered_on_top_level_cli() -> None:
    assert "user" in _registered_top_level_commands()


def test_user_group_help_lists_subcommands() -> None:
    result = CliRunner().invoke(main_module.cli, ["user", "--help"])

    assert result.exit_code == 0
    assert user_module.user.commands, "expected at least one user subcommand"


def test_git_group_is_registered_on_top_level_cli() -> None:
    assert "git" in _registered_top_level_commands()


def test_git_group_help_lists_subcommands() -> None:
    result = CliRunner().invoke(main_module.cli, ["git", "--help"])

    assert result.exit_code == 0
    assert git_module.git.commands, "expected at least one git subcommand"
