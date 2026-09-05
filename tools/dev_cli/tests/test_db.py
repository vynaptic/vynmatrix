"""Tests for dev_cli database commands."""

import importlib
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

db_module = importlib.import_module("dev_cli.commands.db")


def test_load_project_env_parses_inline_comments_and_quotes(monkeypatch, tmp_path: Path) -> None:
    """Repo-root .env parsing should match the helper script behavior."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# Comment",
                "ENV=dev  # inline comment",
                "PGADMIN_EMAIL='admin@example.com'",
                'PGADMIN_PASSWORD="secret-value"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)

    assert db_module._load_project_env() == {
        "ENV": "dev",
        "PGADMIN_EMAIL": "admin@example.com",
        "PGADMIN_PASSWORD": "secret-value",
    }


def test_start_delegates_to_manage_db_with_repo_root_env(monkeypatch, tmp_path: Path) -> None:
    """vmdev db start delegates to scripts/db/manage_db with repo .env values.

    start/stop/status share ONE implementation (the manage_db script pair);
    vmdev is the ergonomic wrapper, not a second copy of the compose logic.
    """
    scripts_dir = tmp_path / "scripts" / "db"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "manage_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts_dir / "manage_db.ps1").write_text("param()\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PGADMIN_EMAIL=admin@example.com",
                "PGADMIN_PASSWORD=strong-password",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(db_module, "SCRIPTS_DIR", scripts_dir)

    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd: list[str], **kwargs: dict) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(db_module.subprocess, "run", fake_run)

    result = CliRunner().invoke(db_module.db, ["start"])

    assert result.exit_code == 0
    delegate_call, delegate_kwargs = next(
        (cmd, kwargs) for cmd, kwargs in calls if any("manage_db" in str(part) for part in cmd)
    )
    assert delegate_call[-1] == "start"
    assert delegate_kwargs["env"]["PGADMIN_EMAIL"] == "admin@example.com"
    assert delegate_kwargs["env"]["PGADMIN_PASSWORD"] == "strong-password"
    assert delegate_kwargs["cwd"] == str(tmp_path)


def test_pgadmin_uses_the_same_repo_env_for_compose_and_output(monkeypatch, tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PGADMIN_EMAIL=operator@example.com",
                "PGADMIN_PASSWORD=strong-password",
                "PGADMIN_PORT=5151",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(db_module, "DOCKER_DIR", docker_dir)
    monkeypatch.setattr(db_module, "_get_docker_compose_cmd", lambda: ["docker", "compose"])
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd: list[str], **kwargs: dict) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(db_module.subprocess, "run", fake_run)

    result = CliRunner().invoke(db_module.db, ["pgadmin"])

    assert result.exit_code == 0
    assert "http://localhost:5151" in result.output
    assert "operator@example.com" in result.output
    assert "admin123" not in result.output
    assert calls[0][1]["env"]["PGADMIN_PASSWORD"] == "strong-password"


def test_pgadmin_rejects_missing_or_placeholder_credentials(monkeypatch, tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "PGADMIN_EMAIL=operator@example.com\nPGADMIN_PASSWORD=CHANGE_ME_BEFORE_USE\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(db_module, "DOCKER_DIR", docker_dir)
    monkeypatch.setattr(db_module, "_get_docker_compose_cmd", lambda: ["docker", "compose"])

    result = CliRunner().invoke(db_module.db, ["pgadmin"])

    assert result.exit_code == 2
    assert "non-placeholder PGADMIN_PASSWORD" in result.output
