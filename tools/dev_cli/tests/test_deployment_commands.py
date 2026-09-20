"""End-to-end tests for ``vmdev init`` against a temporary project root."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from dev_cli.commands import deployment as command_module
from dev_cli.core.bootstrap import BootstrapSettings, validate_owner_input
from dev_cli.core.deployment import configuration, environment

_ARGS = [
    "--email",
    "owner@example.test",
    "--base-currency",
    "eur",
    "--timezone",
    "Europe/Amsterdam",
    "--paper-equity",
    "25000",
]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout containing only the template ``vmdev init`` renders from."""
    root = Path(__file__).resolve().parents[3]
    (tmp_path / configuration.ENV_EXAMPLE).write_text(
        (root / configuration.ENV_EXAMPLE).read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(command_module, "PROJECT_ROOT", tmp_path)
    return tmp_path


def test_init_writes_two_private_files_the_lifecycle_accepts(project: Path) -> None:
    result = CliRunner().invoke(command_module.init, _ARGS)

    assert result.exit_code == 0, result.output
    env_path = project / configuration.ENV_FILE
    owner_path = project / configuration.OWNER_FILE
    for path in (env_path, owner_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name

    values = environment.load_env_file(env_path)
    # The generated configuration is one bootstrap would accept unchanged.
    BootstrapSettings.parse(environment.resolve_host_env(values))
    profile = validate_owner_input(yaml.safe_load(owner_path.read_text(encoding="utf-8")))
    assert profile["profile"] == {
        "email": "owner@example.test",
        "base_ccy": "EUR",
        "tz": "Europe/Amsterdam",
    }
    assert values[configuration.PAPER_EQUITY_KEY] == "25000"
    # Compose must not be able to resolve anything until a deploy names a tag.
    assert values["VM_DEPLOY_IMAGE_TAG"] == ""


def test_init_keeps_every_template_key_and_generates_every_secret(project: Path) -> None:
    CliRunner().invoke(command_module.init, _ARGS)

    declared = environment.declared_keys(project / configuration.ENV_EXAMPLE)
    written = environment.declared_keys(project / configuration.ENV_FILE)
    values = environment.load_env_file(project / configuration.ENV_FILE)

    assert written == declared
    secrets = [values[key] for key in configuration.SECRET_KEYS]
    assert all(secrets)
    assert len(set(secrets)) == len(configuration.SECRET_KEYS)
    assert "CHANGE_ME_BEFORE_USE" not in (project / configuration.ENV_FILE).read_text(
        encoding="utf-8"
    )


def test_init_never_overwrites_an_existing_configuration(project: Path) -> None:
    CliRunner().invoke(command_module.init, _ARGS)
    before = (project / configuration.ENV_FILE).read_text(encoding="utf-8")

    result = CliRunner().invoke(command_module.init, _ARGS)

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert (project / configuration.ENV_FILE).read_text(encoding="utf-8") == before


def test_init_update_adds_new_keys_and_preserves_every_installed_value(project: Path) -> None:
    CliRunner().invoke(command_module.init, _ARGS)
    before = environment.load_env_file(project / configuration.ENV_FILE)
    template = project / configuration.ENV_EXAMPLE
    template.write_text(
        template.read_text(encoding="utf-8")
        + "\n# added upstream\nNEW_TUNABLE=7\nFEEDBACK_API_KEY_V2=\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(command_module.init, ["--update"])

    assert result.exit_code == 0, result.output
    after = environment.load_env_file(project / configuration.ENV_FILE)
    assert all(after[key] == value for key, value in before.items())
    assert after["NEW_TUNABLE"] == "7"
    assert stat.S_IMODE((project / configuration.ENV_FILE).stat().st_mode) == 0o600


def test_init_update_refuses_before_there_is_anything_to_update(project: Path) -> None:
    result = CliRunner().invoke(command_module.init, ["--update"])

    assert result.exit_code != 0
    assert "does not exist yet" in result.output


def test_init_refuses_a_non_interactive_run_without_the_four_answers(project: Path) -> None:
    result = CliRunner().invoke(command_module.init, ["--email", "owner@example.test"])

    assert result.exit_code != 0
    assert "required" in result.output


def test_init_refuses_a_starting_equity_that_is_not_capital(project: Path) -> None:
    result = CliRunner().invoke(command_module.init, [*_ARGS[:-1], "0"])

    assert result.exit_code != 0
    assert not (project / configuration.ENV_FILE).exists()
