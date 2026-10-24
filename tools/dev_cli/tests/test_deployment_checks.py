"""``vmdev doctor`` prerequisites: Docker must answer, build with BuildKit, and keep bind sources writable."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from dev_cli.core.deployment import checks

_COMPOSE = ("docker", "compose", "version", "--short")
_BUILDX = ("docker", "buildx", "version")
_DAEMON = ("docker", "version", "--format", "{{.Server.Version}}")
_HEALTHY: dict[tuple[str, ...], tuple[int, str]] = {
    _COMPOSE: (0, "2.40.3\n"),
    _BUILDX: (0, "github.com/docker/buildx v0.30.1 792b8327a475a5d8c9d5f4ea6ce866e7da39ae8b\n"),
    _DAEMON: (0, "29.1.3\n"),
}


def _docker(answers: Mapping[tuple[str, ...], tuple[int, str] | Exception]) -> Callable[..., Any]:
    def run(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        assert kwargs["timeout"] > 0
        answer = answers[tuple(command)]
        if isinstance(answer, Exception):
            raise answer
        code, stdout = answer
        return CompletedProcess(command, code, stdout=stdout, stderr="")

    return run


def _docker_checks(
    answers: Mapping[tuple[str, ...], tuple[int, str] | Exception],
    environ: Mapping[str, str] | None = None,
) -> dict[str, checks.Check]:
    collected = checks.check_prerequisites(run=_docker(answers), environ=environ or {})
    return {check.name: check for check in collected if check.name.startswith("docker:")}


@pytest.fixture(autouse=True)
def _docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks.shutil, "which", lambda _name: "/usr/bin/docker")


def test_a_healthy_engine_with_compose_and_buildx_passes() -> None:
    found = _docker_checks(_HEALTHY)

    assert {name: check.status for name, check in found.items()} == {
        "docker:compose": checks.OK,
        "docker:buildx": checks.OK,
        "docker:daemon": checks.OK,
    }
    assert found["docker:buildx"].detail == "docker buildx v0.30.1"
    assert found["docker:daemon"].detail == "Docker Engine 29.1.3 is answering"


def test_a_stopped_daemon_fails_although_the_client_plugins_answer() -> None:
    """``docker compose version`` exits 0 with the daemon down; it proves nothing."""
    found = _docker_checks(_HEALTHY | {_DAEMON: (1, "")})

    assert found["docker:compose"].status == checks.OK
    assert found["docker:daemon"].status == checks.FAIL
    assert "docker group" in found["docker:daemon"].detail
    assert checks.worst(list(found.values())) == checks.FAIL


def test_a_daemon_that_hangs_fails_instead_of_blocking_doctor() -> None:
    found = _docker_checks(_HEALTHY | {_DAEMON: subprocess.TimeoutExpired(list(_DAEMON), 30)})

    assert found["docker:daemon"].status == checks.FAIL


def test_an_engine_without_buildx_fails_before_the_build_would() -> None:
    """The legacy builder rejects ``--build-context`` and ``RUN --mount``."""
    found = _docker_checks(_HEALTHY | {_BUILDX: (1, "")})

    assert found["docker:buildx"].status == checks.FAIL
    assert "docker-buildx-plugin" in found["docker:buildx"].detail


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " f "])
def test_buildkit_switched_off_fails_even_with_buildx_installed(value: str) -> None:
    found = _docker_checks(_HEALTHY, environ={"DOCKER_BUILDKIT": value})

    assert found["docker:buildx"].status == checks.FAIL
    assert "unset DOCKER_BUILDKIT" in found["docker:buildx"].detail


def test_buildkit_switched_on_explicitly_passes() -> None:
    found = _docker_checks(_HEALTHY, environ={"DOCKER_BUILDKIT": "1"})

    assert found["docker:buildx"].status == checks.OK


def test_a_missing_compose_plugin_fails() -> None:
    found = _docker_checks(_HEALTHY | {_COMPOSE: OSError("exec format error")})

    assert found["docker:compose"].status == checks.FAIL


def test_docker_missing_from_path_fails_without_running_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks.shutil, "which", lambda _name: None)

    found = _docker_checks({})

    assert {name: check.status for name, check in found.items()} == {
        "docker:available": checks.FAIL
    }


def test_bind_sources_pass_before_the_first_deploy_and_once_created(tmp_path: Path) -> None:
    (before,) = checks.check_bind_sources(tmp_path)
    (tmp_path / ".artifacts" / "deployments").mkdir(parents=True)
    (after,) = checks.check_bind_sources(tmp_path)

    assert before.status == checks.OK
    assert "will be created" in before.detail
    assert after.status == checks.OK


def test_a_bind_source_that_is_a_file_fails(tmp_path: Path) -> None:
    (tmp_path / ".artifacts").write_text("", encoding="utf-8")

    (found,) = checks.check_bind_sources(tmp_path)

    assert found.status == checks.FAIL


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions and a user that is not root",
)
@pytest.mark.parametrize("locked", [".artifacts", ".artifacts/deployments"])
def test_a_bind_source_this_user_cannot_write_fails_with_the_fix(
    tmp_path: Path, locked: str
) -> None:
    """What Docker Engine leaves behind when it created ``.artifacts`` as root."""
    (tmp_path / ".artifacts" / "deployments").mkdir(parents=True)
    target = tmp_path / locked
    target.chmod(0o555)
    try:
        (found,) = checks.check_bind_sources(tmp_path)
    finally:
        target.chmod(0o755)

    assert found.status == checks.FAIL
    assert locked in found.detail
    assert 'sudo chown -R "$(id -u):$(id -g)" .artifacts' in found.detail
