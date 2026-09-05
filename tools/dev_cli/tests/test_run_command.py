"""Tests for fail-closed local application launching."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

run_module = importlib.import_module("dev_cli.commands.run")

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("app_name", "module_name"),
    [
        ("backend", "backend.main"),
        ("execution_engine", "main"),
        ("feedback_loop_engine", "feedback_loop_engine.main"),
        ("indicator_runner", "indicator_runner.main"),
        ("market_data_ingestor", "market_data_ingestor.main"),
        ("scoring_engine", "scoring_engine.main"),
    ],
)
def test_every_configured_app_has_an_executable_module(
    app_name: str,
    module_name: str,
) -> None:
    assert run_module._resolve_app_module(_ROOT / "apps" / app_name, app_name) == module_name


def test_run_app_uses_resolved_module_and_propagates_child_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_path = tmp_path / "apps" / "scoring_engine"
    (app_path / "scoring_engine").mkdir(parents=True)
    (app_path / "scoring_engine" / "main.py").write_text("", encoding="utf-8")
    python_path = tmp_path / "build" / "venvs" / "app-scoring_engine" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.touch()

    monkeypatch.setattr(run_module, "_find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(run_module, "find_python_executable", lambda _venv: python_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command: list[str], **kwargs: dict) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(run_module.subprocess, "run", fake_run)

    result = CliRunner().invoke(run_module.run, ["app", "--name", "scoring_engine"])

    assert result.exit_code == 7
    assert calls == [
        (
            [str(python_path), "-m", "scoring_engine.main"],
            {"cwd": app_path, "check": False},
        )
    ]


def test_run_app_rejects_missing_venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_module, "_find_repo_root", lambda: tmp_path)

    result = CliRunner().invoke(run_module.run, ["app", "--name", "scoring_engine"])

    assert result.exit_code == 1
    assert "Venv not found" in result.output


def test_resolve_app_module_rejects_non_executable_layout(tmp_path: Path) -> None:
    app_path = tmp_path / "apps" / "unknown"
    app_path.mkdir(parents=True)

    with pytest.raises(run_module.click.ClickException, match=r"no executable main[.]py"):
        run_module._resolve_app_module(app_path, "unknown")
