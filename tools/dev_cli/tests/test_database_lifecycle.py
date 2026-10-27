"""Container lifecycle stays bounded and preflight is free of side effects."""

import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from dev_cli.core.database_lifecycle import PlatformLifecycle


@pytest.fixture(autouse=True)
def isolated_research_guard(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        PlatformLifecycle,
        "_new_research_maintenance",
        lambda _self: SimpleNamespace(
            begin=lambda: None,
            complete=lambda: None,
            abort=lambda: None,
            after_restore=lambda: None,
            previous=None,
            stack_was_active=False,
        ),
    )


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
    assert tails[0] == ["up", "-d", "--wait", "postgres"]
    assert tails[1] == ["stop", "--timeout", "60", "workers", "application"]
    assert tails[2] == ["ps", "--status", "running", "--services"]
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


@pytest.mark.parametrize("subcommand", ["up", "run", "create"])
def test_bind_sources_exist_as_this_user_before_compose_creates_a_container(
    tmp_path: Path, subcommand: str
) -> None:
    """Docker Engine on Linux would create a missing bind source owned by root."""
    seen = []

    def run(command, **kwargs):
        seen.append((tmp_path / ".artifacts").is_dir())
        return CompletedProcess(command, 0, stdout="", stderr="")

    PlatformLifecycle(tmp_path, configured(), run=run).command(subcommand, "application")

    assert seen == [True]
    if hasattr(os, "getuid"):
        assert (tmp_path / ".artifacts").stat().st_uid == os.getuid()


@pytest.mark.parametrize("subcommand", ["ps", "stop", "exec"])
def test_commands_that_create_no_container_create_no_directory(
    tmp_path: Path, subcommand: str
) -> None:
    def run(command, **kwargs):
        return CompletedProcess(command, 0, stdout="", stderr="")

    PlatformLifecycle(tmp_path, configured(), run=run).command(subcommand)

    assert not (tmp_path / ".artifacts").exists()


def test_research_fence_precedes_stop_and_failed_stage_keeps_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []

    class Guard:
        def begin(self):
            events.append("fence_and_drain")

        def abort(self):
            events.append("unlock_keep_fence")

    lifecycle = PlatformLifecycle(
        tmp_path,
        configured(),
        run=lambda command, **_kwargs: (
            events.append("compose") or CompletedProcess(command, 1, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(lifecycle, "_new_research_maintenance", Guard)
    with pytest.raises(RuntimeError):
        lifecycle.stop_runtime()
    assert events == ["fence_and_drain", "compose", "unlock_keep_fence"]


@pytest.mark.parametrize(
    ("maintenance", "active", "expected"),
    [(False, True, "resume"), (False, False, "fenced"), (True, True, "fenced")],
)
def test_standalone_backup_resumes_only_its_previously_active_stack(
    tmp_path, maintenance, active, expected
):
    from types import SimpleNamespace

    events = []
    lifecycle = PlatformLifecycle(tmp_path, configured())
    lifecycle._research = SimpleNamespace(
        previous={"maintenance": maintenance},
        stack_was_active=active,
        complete=lambda: events.append("resume"),
        abort=lambda: events.append("fenced"),
    )
    lifecycle.finish_standalone_backup()
    assert events == [expected]
    assert lifecycle._research is None
