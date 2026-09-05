from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from dev_cli.commands.build import build
from dev_cli.core.docker_builder import DockerBuilder


def test_build_team_inventory_matches_component_owners() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load((repo_root / "config" / "build.yaml").read_text(encoding="utf-8"))
    teams = {name: set(components) for name, components in config["teams"].items()}
    components = [
        *config["libs"]["components"],
        *config["strategies"]["groups"],
        *config["apps"]["components"],
    ]
    by_name = {component["name"]: component for component in components}

    for component in components:
        assert component["name"] in teams[component["owner_team"]]
    for team, names in teams.items():
        assert all(name in by_name and by_name[name]["owner_team"] == team for name in names)


def test_powershell_wrapper_disables_positional_binding() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "build_strategies.ps1").read_text(encoding="utf-8")

    assert "[CmdletBinding(PositionalBinding = $false)]" in script
    assert script.index("vmdev build libs") < script.index("vmdev build strategies")
    assert script.index("vmdev build strategies") < script.index("vmdev build venvs")


def test_shell_wrapper_builds_strategy_wheel_before_venvs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = (repo_root / "scripts" / "build_strategies.sh").read_text(encoding="utf-8")

    assert script.index("vmdev build libs") < script.index("vmdev build strategies")
    assert script.index("vmdev build strategies") < script.index("vmdev build venvs")


def test_platform_image_installs_verified_strategy_wheel_payload() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile = (repo_root / "docker" / "platform_runtime.Dockerfile").read_text(encoding="utf-8")
    strategy_setup = (repo_root / "strategies" / "indicator" / "setup.py").read_text(
        encoding="utf-8"
    )

    assert "vynmatrix_indicator-*.whl" in dockerfile
    assert "--target /opt/strategies/indicator" in dockerfile
    assert "COPY --from=wheel-builder" in dockerfile
    assert "COPY docker/constraints.txt /tmp/constraints.txt" in dockerfile
    assert "-c /tmp/constraints.txt" in dockerfile
    assert "COPY build/wheels/lib_common-*.whl /tmp/wheels/" in dockerfile
    assert "COPY build/wheels /tmp/wheels" not in dockerfile
    assert "COPY strategies/indicator" not in dockerfile
    assert "-type d \\( -name test -o -name tests \\)" in dockerfile
    assert "-name 'test_*.py'" in dockerfile
    assert "-name '*_test.py'" in dockerfile
    assert '_PACKAGE_DATA = {package: ["config.json"]' in strategy_setup
    assert "include_package_data=False" in strategy_setup
    assert "README.md" not in strategy_setup
    assert "validation_protocol.json" not in strategy_setup


def test_release_workflow_builds_strategy_wheel_before_images() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    workflow = (repo_root / ".github" / "workflows" / "build-and-push.yml").read_text(
        encoding="utf-8"
    )

    assert "tools/dev_cli/requirements.txt" not in workflow
    assert workflow.index("vmdev build strategies") < workflow.index(
        "vmdev build docker --from-config"
    )


def test_shared_service_base_uses_resolved_constraints() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile = (repo_root / "docker" / "svc-base.Dockerfile").read_text(encoding="utf-8")

    assert "FROM ${PYTHON_IMAGE} AS dependency-builder" in dockerfile
    assert "FROM ${PYTHON_IMAGE} AS runtime" in dockerfile
    assert "COPY docker/constraints.txt /tmp/constraints.txt" in dockerfile
    assert "-c /tmp/constraints.txt" in dockerfile
    assert "-r /tmp/requirements-svc-base.txt" in dockerfile
    assert "sharing=locked" in dockerfile
    assert "-type d \\( -name test -o -name tests \\)" in dockerfile
    assert "-name 'test_*.py'" in dockerfile
    assert "-name '*_test.py'" in dockerfile
    assert "pip uninstall --yes pip setuptools" in dockerfile
    assert "/usr/local/bin/python -m pip uninstall --yes pip setuptools" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "apt-get install" not in dockerfile
    assert "curl" not in dockerfile


def test_platform_migration_dependencies_are_constrained() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    profile = (repo_root / "docker" / "requirements-platform.txt").read_text(encoding="utf-8")
    constraints = (repo_root / "docker" / "constraints.txt").read_text(encoding="utf-8")
    dockerfile = (repo_root / "docker" / "platform_runtime.Dockerfile").read_text(encoding="utf-8")

    assert "alembic==1.18.5" in profile
    assert "alembic==1.18.5" in constraints
    assert "Mako==1.3.10" in constraints
    assert "MarkupSafe==3.0.3" in constraints
    assert "rm -rf /opt/service/lib/python3.11/site-packages/alembic/testing" in dockerfile


def test_platform_image_copies_only_verified_library_and_strategy_wheels() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    inventory = yaml.safe_load((repo_root / "config/containers.yaml").read_text())
    for service in inventory["services"]:
        dockerfile = (repo_root / service["dockerfile"]).read_text(encoding="utf-8")
        assert "AS wheel-builder" in dockerfile
        assert "AS runtime" in dockerfile
        assert "COPY build/wheels /tmp/wheels" not in dockerfile
        for library in (
            "common",
            "data",
            "indicators",
            "strategy",
            "application",
            "infrastructure",
        ):
            assert f"COPY build/wheels/lib_{library}-*.whl /tmp/wheels/" in dockerfile
        assert "/opt/service/bin/python -m pip check" in dockerfile
        assert "pip uninstall --yes pip setuptools" in dockerfile
        assert "urllib.request.urlopen" in dockerfile
        assert dockerfile.index("-r /tmp/requirements-") < dockerfile.index(
            "COPY build/wheels/lib_common-*.whl"
        )


def test_docker_builder_emits_scoped_gha_cache_command(tmp_path: Path) -> None:
    builder = DockerBuilder({}, tmp_path)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.touch()

    command = builder._build_command(
        dockerfile=dockerfile,
        image_name="vynmatrix/scoring-engine:pr",
        cache_backend="gha",
        cache_scope="vynmatrix-scoring-engine",
        build_contexts={"vynmatrix/svc-base:latest": ("docker-image://vynmatrix/svc-base:latest")},
    )

    assert command[:3] == ["docker", "buildx", "build"]
    assert "--load" in command
    assert "type=gha,scope=vynmatrix-scoring-engine" in command
    assert "type=gha,mode=max,scope=vynmatrix-scoring-engine" in command
    assert "vynmatrix/svc-base:latest=docker-image://vynmatrix/svc-base:latest" in command
    assert "com.vynmatrix.managed-by=vmdev-v1" in command
    assert "com.vynmatrix.source-repository=vynmatrix" in command
    assert "com.vynmatrix.image-repository=vynmatrix/scoring-engine" in command


def test_docker_builder_labels_classic_local_build(tmp_path: Path) -> None:
    command = DockerBuilder({}, tmp_path)._build_command(
        dockerfile=tmp_path / "Dockerfile",
        image_name="vynmatrix/execution-engine:latest",
        cache_backend=None,
        cache_scope="unused-for-classic-build",
    )

    assert command[:2] == ["docker", "build"]
    assert "com.vynmatrix.managed-by=vmdev-v1" in command
    assert "com.vynmatrix.source-repository=vynmatrix" in command
    assert "com.vynmatrix.image-repository=vynmatrix/execution-engine" in command


def test_config_image_inventory_builds_base_then_each_service_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "containers.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "services:\n"
        "  - app: scoring_engine\n"
        "    image: vynmatrix/scoring-engine\n"
        "  - app: execution_engine\n"
        "    image: vynmatrix/execution-engine\n"
        "  - app: indicator_runner\n"
        "    image: vynmatrix/indicator-runner\n",
        encoding="utf-8",
    )
    builder = DockerBuilder({}, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(builder, "validate_wheelhouse", lambda: calls.append("validate"))
    monkeypatch.setattr(
        builder,
        "build_svc_base",
        lambda *, cache_backend=None: calls.append(("base", cache_backend)),
    )
    monkeypatch.setattr(
        builder,
        "build_app",
        lambda app, tag, **kwargs: calls.append(("app", app, tag, kwargs)),
    )
    monkeypatch.setattr(
        builder,
        "_cleanup_owned_dangling_images",
        lambda **kwargs: calls.append(("cleanup", kwargs)),
    )

    builder.build_from_containers_config(
        tag="candidate",
        config_path=config_path,
        cache_backend="gha",
    )

    assert calls == [
        "validate",
        ("base", "gha"),
        (
            "app",
            "scoring_engine",
            "candidate",
            {
                "image_repository": "vynmatrix/scoring-engine",
                "cache_backend": "gha",
                "validate_wheelhouse": False,
            },
        ),
        (
            "app",
            "execution_engine",
            "candidate",
            {
                "image_repository": "vynmatrix/execution-engine",
                "cache_backend": "gha",
                "validate_wheelhouse": False,
            },
        ),
        (
            "app",
            "indicator_runner",
            "candidate",
            {
                "image_repository": "vynmatrix/indicator-runner",
                "cache_backend": "gha",
                "validate_wheelhouse": False,
            },
        ),
        (
            "cleanup",
            {
                "allowed_repositories": frozenset(
                    {
                        "vynmatrix/svc-base",
                        "vynmatrix/scoring-engine",
                        "vynmatrix/execution-engine",
                        "vynmatrix/indicator-runner",
                    }
                ),
                "current_refs": {
                    "vynmatrix/svc-base:latest": "vynmatrix/svc-base",
                    "vynmatrix/scoring-engine:candidate": ("vynmatrix/scoring-engine"),
                    "vynmatrix/execution-engine:candidate": ("vynmatrix/execution-engine"),
                    "vynmatrix/indicator-runner:candidate": ("vynmatrix/indicator-runner"),
                },
            },
        ),
    ]


def test_config_image_inventory_never_cleans_after_partial_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "containers.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "services:\n"
        "  - app: scoring_engine\n"
        "    image: vynmatrix/scoring-engine\n"
        "  - app: execution_engine\n"
        "    image: vynmatrix/execution-engine\n",
        encoding="utf-8",
    )
    builder = DockerBuilder({}, tmp_path)
    cleanup_calls: list[object] = []
    monkeypatch.setattr(builder, "validate_wheelhouse", lambda: None)
    monkeypatch.setattr(builder, "build_svc_base", lambda **kwargs: None)

    def fail_build(app_name: str, tag: str, **kwargs: object) -> None:
        if app_name == "execution_engine":
            raise subprocess.CalledProcessError(1, ["docker", "build"])

    monkeypatch.setattr(builder, "build_app", fail_build)
    monkeypatch.setattr(
        builder,
        "_cleanup_owned_dangling_images",
        lambda **kwargs: cleanup_calls.append(kwargs),
    )

    with pytest.raises(subprocess.CalledProcessError):
        builder.build_from_containers_config(config_path=config_path)

    assert cleanup_calls == []


def _owned_image_payload(
    image_id: str,
    repository: str,
    *,
    tags: list[str] | None,
    digests: list[str] | None = None,
) -> dict[str, object]:
    return {
        "Id": image_id,
        "RepoTags": tags,
        "RepoDigests": digests,
        "Config": {
            "Labels": {
                "com.vynmatrix.managed-by": "vmdev-v1",
                "com.vynmatrix.source-repository": "vynmatrix",
                "com.vynmatrix.image-repository": repository,
            }
        },
    }


def test_owned_image_cleanup_revalidates_then_removes_exact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_id = f"sha256:{'1' * 64}"
    candidate_id = f"sha256:{'2' * 64}"
    repository = "vynmatrix/scoring-engine"
    current_ref = f"{repository}:latest"
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            payload = (
                _owned_image_payload(current_id, repository, tags=[current_ref])
                if command[-1] == current_ref
                else _owned_image_payload(candidate_id, repository, tags=None)
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(command, 0, f"{candidate_id}\n", "")
        if command[:3] == ["docker", "container", "ls"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "image", "rm"]:
            return subprocess.CompletedProcess(command, 0, candidate_id, "")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", run)

    DockerBuilder._cleanup_owned_dangling_images(
        allowed_repositories=frozenset({repository}),
        current_refs={current_ref: repository},
    )

    list_command = next(command for command in calls if command[:3] == ["docker", "image", "ls"])
    assert "dangling=true" in list_command
    assert "label=com.vynmatrix.managed-by=vmdev-v1" in list_command
    assert "label=com.vynmatrix.source-repository=vynmatrix" in list_command
    assert calls.count(["docker", "image", "inspect", "--format", "{{json .}}", candidate_id]) == 2
    assert [
        "docker",
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"ancestor={candidate_id}",
    ] in calls
    assert ["docker", "image", "rm", "--no-prune", candidate_id] in calls
    assert all("--force" not in command for command in calls)
    assert all(command[:3] != ["docker", "image", "prune"] for command in calls)


def test_owned_image_cleanup_fails_closed_when_current_ref_is_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "No such image")

    monkeypatch.setattr(subprocess, "run", run)

    DockerBuilder._cleanup_owned_dangling_images(
        allowed_repositories=frozenset({"vynmatrix/scoring-engine"}),
        current_refs={"vynmatrix/scoring-engine:latest": "vynmatrix/scoring-engine"},
    )

    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]


@pytest.mark.parametrize(
    ("candidate_repository", "digests"),
    [
        ("vynmatrix/not-configured", None),
        ("vynmatrix/scoring-engine", ["vynmatrix/scoring-engine@sha256:abc"]),
    ],
)
def test_owned_image_cleanup_keeps_unallowlisted_or_referenced_candidates(
    monkeypatch: pytest.MonkeyPatch,
    candidate_repository: str,
    digests: list[str] | None,
) -> None:
    current_id = f"sha256:{'3' * 64}"
    candidate_id = f"sha256:{'4' * 64}"
    repository = "vynmatrix/scoring-engine"
    current_ref = f"{repository}:latest"
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            payload = (
                _owned_image_payload(current_id, repository, tags=[current_ref])
                if command[-1] == current_ref
                else _owned_image_payload(
                    candidate_id,
                    candidate_repository,
                    tags=None,
                    digests=digests,
                )
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(command, 0, f"{candidate_id}\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", run)

    DockerBuilder._cleanup_owned_dangling_images(
        allowed_repositories=frozenset({repository}),
        current_refs={current_ref: repository},
    )

    assert all(command[:3] != ["docker", "container", "ls"] for command in calls)
    assert all(command[:3] != ["docker", "image", "rm"] for command in calls)


def test_owned_image_cleanup_keeps_images_used_by_stopped_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_id = f"sha256:{'5' * 64}"
    candidate_id = f"sha256:{'6' * 64}"
    repository = "vynmatrix/scoring-engine"
    current_ref = f"{repository}:latest"
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            payload = (
                _owned_image_payload(current_id, repository, tags=[current_ref])
                if command[-1] == current_ref
                else _owned_image_payload(candidate_id, repository, tags=None)
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(command, 0, f"{candidate_id}\n", "")
        if command[:3] == ["docker", "container", "ls"]:
            return subprocess.CompletedProcess(command, 0, "stopped-container\n", "")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", run)

    DockerBuilder._cleanup_owned_dangling_images(
        allowed_repositories=frozenset({repository}),
        current_refs={current_ref: repository},
    )

    assert all(command[:3] != ["docker", "image", "rm"] for command in calls)


@pytest.mark.parametrize(
    ("content", "error", "match"),
    [
        ("services: []\n", ValueError, "non-empty services"),
        ("services:\n  - image: missing-app\n", ValueError, r"services\[0\]\.app"),
        (
            "services:\n  - app: scoring_engine\n",
            ValueError,
            r"services\[0\]\.image",
        ),
        (
            "services:\n"
            "  - app: scoring_engine\n"
            "    image: vynmatrix/scoring-engine\n"
            "  - app: scoring_engine\n"
            "    image: vynmatrix/scoring-engine-copy\n",
            ValueError,
            "duplicate service apps",
        ),
        (
            "services:\n  - app: scoring_engine\n    image: vynmatrix/scoring-engine:latest\n",
            ValueError,
            "repository-only reference",
        ),
        (
            "services:\n  - app: scoring_engine\n    image: vynmatrix/scoring-engine@sha256:abc\n",
            ValueError,
            "repository-only reference",
        ),
        (
            "services:\n"
            "  - app: scoring_engine\n"
            "    image: vynmatrix/pipeline\n"
            "  - app: execution_engine\n"
            "    image: vynmatrix/pipeline\n",
            ValueError,
            "duplicate service image repositories",
        ),
    ],
)
def test_config_image_inventory_fails_closed(
    tmp_path: Path,
    content: str,
    error: type[Exception],
    match: str,
) -> None:
    config_path = tmp_path / "config" / "containers.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(error, match=match):
        DockerBuilder({}, tmp_path).build_from_containers_config(config_path=config_path)


def test_config_image_inventory_rejects_missing_config(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Containers config not found"):
        DockerBuilder({}, tmp_path).build_from_containers_config(
            config_path=tmp_path / "missing.yaml"
        )


def test_docker_builder_rejects_missing_dockerfiles(tmp_path: Path) -> None:
    builder = DockerBuilder({}, tmp_path)

    with pytest.raises(FileNotFoundError, match="svc-base Dockerfile not found"):
        builder.build_svc_base()
    with pytest.raises(FileNotFoundError, match="Dockerfile not found for app"):
        builder.build_app("missing_app", validate_wheelhouse=False)


def test_declared_dockerfile_builds_composed_image_without_app_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dockerfile = tmp_path / "docker" / "composed.Dockerfile"
    dockerfile.parent.mkdir()
    dockerfile.write_text("FROM scratch\n")
    config = tmp_path / "containers.yaml"
    config.write_text(
        "services:\n  - app: platform_runtime\n    image: vynmatrix/platform\n    dockerfile: docker/composed.Dockerfile\n"
    )
    builder = DockerBuilder({}, tmp_path)
    monkeypatch.setattr(builder, "validate_wheelhouse", lambda: None)
    monkeypatch.setattr(builder, "build_svc_base", lambda **kwargs: None)
    monkeypatch.setattr(builder, "_cleanup_owned_dangling_images", lambda **kwargs: None)
    commands: list[list[str]] = []

    def record(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", record)
    builder.build_from_containers_config(config_path=config)
    assert len(commands) == 1
    assert str(dockerfile) in commands[0]
    assert "vynmatrix/platform:latest" in commands[0]


@pytest.mark.parametrize(
    "declared",
    [
        "../outside.Dockerfile",
        "/tmp/outside.Dockerfile",
        "",
        "config/containers.yaml",
        "docker/missing.Dockerfile",
    ],
)
def test_invalid_declared_dockerfile_fails_before_docker_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
) -> None:
    config = tmp_path / "containers.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "services": [
                    {
                        "app": "platform_runtime",
                        "image": "vynmatrix/platform",
                        "dockerfile": declared,
                    }
                ]
            }
        )
    )

    def reject_docker(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid build configuration contacted Docker")

    monkeypatch.setattr(subprocess, "run", reject_docker)
    with pytest.raises((ValueError, FileNotFoundError), match=r"[Dd]ockerfile"):
        DockerBuilder({}, tmp_path).build_from_containers_config(config_path=config)


def _write_current_wheel(root: Path) -> tuple[dict[str, object], Path, Path]:
    component = root / "libs" / "python" / "lib_example"
    package = component / "lib_example"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    setup = component / "setup.py"
    setup.write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    wheels = root / "build" / "wheels"
    wheels.mkdir(parents=True)
    wheel = wheels / "lib_example-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.write(module, "lib_example/__init__.py")
    wheel_time = wheel.stat().st_mtime_ns + 1_000_000
    os.utime(wheel, ns=(wheel_time, wheel_time))
    config: dict[str, object] = {
        "global": {"wheels_dir": "./build/wheels"},
        "libs": {
            "components": [
                {
                    "name": "lib_example",
                    "path": "libs/python/lib_example",
                }
            ]
        },
        "strategies": {"groups": []},
    }
    return config, wheel, setup


def test_docker_builder_accepts_exact_current_wheel(tmp_path: Path) -> None:
    config, _, _ = _write_current_wheel(tmp_path)

    DockerBuilder(config, tmp_path).validate_wheelhouse()


def test_docker_builder_rejects_packaging_newer_than_wheel(tmp_path: Path) -> None:
    config, wheel, setup = _write_current_wheel(tmp_path)
    stale_time = wheel.stat().st_mtime_ns + 1_000_000
    os.utime(setup, ns=(stale_time, stale_time))

    with pytest.raises(RuntimeError, match="predates packaging metadata"):
        DockerBuilder(config, tmp_path).validate_wheelhouse()


def test_venv_command_exposes_dedicated_validation_environment() -> None:
    result = CliRunner().invoke(build, ["venvs", "--help"])

    assert result.exit_code == 0
    assert "--validation" in result.output


def test_venv_command_rejects_ambiguous_environment_selection() -> None:
    result = CliRunner().invoke(
        build,
        ["venvs", "--group", "indicator", "--validation"],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
