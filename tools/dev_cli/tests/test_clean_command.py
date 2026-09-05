"""Safety tests for repository-scoped cleanup."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

from click.testing import CliRunner

clean_module = importlib.import_module("dev_cli.commands.clean")


def test_default_clean_never_calls_docker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    wheels = tmp_path / "build" / "wheels"
    venvs = tmp_path / "build" / "venvs"
    pycache = tmp_path / "package" / "__pycache__"
    for directory in (wheels, venvs, pycache):
        directory.mkdir(parents=True)
        (directory / "artifact").write_text("stale", encoding="utf-8")

    monkeypatch.setattr(clean_module, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        clean_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default clean must not call Docker")
        ),
    )

    result = CliRunner().invoke(clean_module.clean)

    assert result.exit_code == 0, result.output
    assert wheels.is_dir()
    assert not any(wheels.iterdir())
    assert venvs.is_dir()
    assert not any(venvs.iterdir())
    assert not pycache.exists()


def test_docker_clean_removes_only_project_image_refs(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=("vynmatrix/scoring-engine:latest\nvynmatrix/execution-engine:1.2.3\n"),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(clean_module.subprocess, "run", _run)

    result = CliRunner().invoke(clean_module.clean, ["--docker"])

    assert result.exit_code == 0, result.output
    assert calls[0] == [
        "docker",
        "image",
        "ls",
        "--filter",
        "reference=vynmatrix/*",
        "--format",
        "{{.Repository}}:{{.Tag}}",
    ]
    assert calls[1] == [
        "docker",
        "image",
        "rm",
        "vynmatrix/execution-engine:1.2.3",
        "vynmatrix/scoring-engine:latest",
    ]
    assert all("system" not in command for command in calls)
