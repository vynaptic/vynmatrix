"""Container lifecycle stays bounded and preflight is free of side effects."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from dev_cli.core.database_lifecycle import PlatformLifecycle


def configured() -> dict[str, str]:
    return {
        "DB_PASSWORD": "unit-only-not-a-deployment-credential",
        "EXECUTION_MODE": "paper",
        "EXECUTION_ENGINE_ALLOW_LIVE": "false",
        "COMPOSE_PROFILES": "workers",
        "PLATFORM_APPLICATION_GROUP": "application",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("COMPOSE_PROFILES", "*"),
        ("COMPOSE_PROFILES", "maintenance"),
        ("PLATFORM_APPLICATION_GROUP", "workers"),
        ("EXECUTION_MODE", "live"),
        ("EXECUTION_ENGINE_ALLOW_LIVE", "true"),
    ],
)
def test_rejects_unsafe_topology_before_commands(tmp_path: Path, field: str, value: str) -> None:
    env = configured() | {field: value}
    calls = []
    with pytest.raises(ValueError, match=r"requires|may contain|Use application"):
        PlatformLifecycle(tmp_path, env, run=lambda *a, **k: calls.append(a))
    assert calls == []


def test_bootstrap_stops_and_verifies_before_one_shot_then_starts_groups(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(command, 0, stdout="", stderr="")

    lifecycle = PlatformLifecycle(tmp_path, configured(), run=run)
    lifecycle.bootstrap('{"profile":{}}', start_runtime=True)
    tails = [
        command[command.index("docker-compose.stack.yml") + 1 :]
        if "docker-compose.stack.yml" in command
        else command[command.index(str(tmp_path / "docker/docker-compose.stack.yml")) + 1 :]
        for command, _ in calls
    ]
    assert tails[0] == ["stop", "--timeout", "60", "workers", "application"]
    assert tails[1] == ["ps", "--status", "running", "--services"]
    assert tails[2] == ["up", "-d", "--wait", "postgres"]
    assert tails[3] == ["run", "--rm", "--no-deps", "-T", "bootstrap"]
    assert calls[3][1]["input"] == '{"profile":{}}'
    assert tails[4] == ["up", "-d", "--wait", "application", "workers"]
    assert all(
        "maintenance" not in kwargs["env"].get("COMPOSE_PROFILES", "") for _, kwargs in calls
    )


def test_failed_bootstrap_leaves_runtime_stopped(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(
            command, 17 if "run" in command else 0, stdout="", stderr="secret-should-not-escape"
        )

    lifecycle = PlatformLifecycle(tmp_path, configured(), run=run)
    with pytest.raises(RuntimeError, match="bootstrap") as exc:
        lifecycle.bootstrap("{}", start_runtime=True)
    assert "secret-should-not-escape" not in str(exc.value)
    assert len(calls) == 4


def test_unknown_existing_container_blocks_maintenance(tmp_path: Path) -> None:
    def run(command, **kwargs):
        return CompletedProcess(
            command, 0, stdout="postgres\nlegacy-relay\n" if "ps" in command else "", stderr=""
        )

    with pytest.raises(ValueError, match="Unexpected running services"):
        PlatformLifecycle(tmp_path, configured(), run=run).bootstrap("{}", start_runtime=False)


def test_combined_variant_has_no_workers_container(tmp_path: Path) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 0, stdout="", stderr="")

    PlatformLifecycle(
        tmp_path,
        configured() | {"COMPOSE_PROFILES": "", "PLATFORM_APPLICATION_GROUP": "all"},
        run=run,
    ).bootstrap("{}", start_runtime=True)
    assert calls[-1][-4:] == ["up", "-d", "--wait", "application"]


def test_bootstrap_error_exposes_only_allowlisted_stage(tmp_path):
    def run(command, **kwargs):
        return CompletedProcess(
            command, 1, stdout="[bootstrap] stage=migration\nsecret-output\n", stderr="credential"
        )

    with pytest.raises(RuntimeError, match="during migration") as exc:
        PlatformLifecycle(tmp_path, configured(), run=run).command("run", "--rm", "bootstrap")
    assert "credential" not in str(exc.value)
    assert "secret-output" not in str(exc.value)
