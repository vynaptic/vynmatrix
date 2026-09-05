"""Tests for dependency-complete application virtual environments."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev_cli.core import builder as builder_module
from dev_cli.core.builder import Builder
from dev_cli.core.venv_manager import VenvManager


def test_builder_installs_app_source_under_service_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_path = tmp_path / "apps" / "market_data_ingestor"
    app_path.mkdir(parents=True)
    (app_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    constraints_file = tmp_path / "docker" / "constraints.txt"
    constraints_file.parent.mkdir()
    constraints_file.write_text("uvicorn==0.27.1\n")

    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "apps": {
            "components": [
                {
                    "name": "market_data_ingestor",
                    "path": "apps/market_data_ingestor",
                    "dependencies": [],
                }
            ]
        },
        "strategies": {"base_dependencies": [], "groups": []},
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    create_venv = Mock()
    builder.venv_manager.create_venv = create_venv

    builder.create_venv_for_app("market_data_ingestor")

    create_venv.assert_called_once_with(
        name="app-market_data_ingestor",
        requirements=[],
        package_path=app_path,
        import_name="market_data_ingestor",
        constraints_file=constraints_file,
    )


def test_indicator_app_venv_installs_exact_strategy_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_path = tmp_path / "apps" / "indicator_runner"
    app_path.mkdir(parents=True)
    (app_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    wheel = tmp_path / "build" / "wheels" / "vynmatrix_indicator-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.touch()
    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "apps": {
            "components": [
                {
                    "name": "indicator_runner",
                    "path": "apps/indicator_runner",
                    "dependencies": [],
                    "strategy_groups": ["indicator"],
                }
            ]
        },
        "strategies": {
            "base_dependencies": [],
            "groups": [
                {
                    "name": "indicator",
                    "path": "strategies/indicator",
                    "wheel_distribution": "vynmatrix_indicator",
                    "dependencies": [],
                }
            ],
        },
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    create_venv = Mock()
    builder.venv_manager.create_venv = create_venv

    builder.create_venv_for_app("indicator_runner")

    create_venv.assert_called_once_with(
        name="app-indicator_runner",
        requirements=[str(wheel)],
        package_path=app_path,
        import_name="indicator_runner",
        constraints_file=tmp_path / "docker" / "constraints.txt",
    )


def test_wheel_build_cleans_stale_deleted_modules_and_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component_path = tmp_path / "libs" / "python" / "lib_example"
    package_path = component_path / "lib_example"
    package_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("\n")
    (component_path / "pyproject.toml").write_text("[build-system]\n")

    stale_build = component_path / "build" / "lib" / "lib_example"
    stale_build.mkdir(parents=True)
    (stale_build / "source_deleted.py").write_text("stale = True\n")
    stale_build_cache = stale_build / "__pycache__"
    stale_build_cache.mkdir()
    (stale_build_cache / "source_deleted.cpython-311.pyc").write_bytes(b"stale")
    source_cache = package_path / "__pycache__"
    source_cache.mkdir()
    (source_cache / "__init__.cpython-311.pyc").write_bytes(b"stale")
    egg_info = component_path / "lib_example.egg-info"
    egg_info.mkdir()
    (egg_info / "SOURCES.txt").write_text("lib_example/source_deleted.py\n")

    config = {
        "global": {"build_dir": "build-output", "python_version": "3.11.13"},
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "log"]:
            # SOURCE_DATE_EPOCH lookup for reproducible wheels.
            return subprocess.CompletedProcess(command, 0, stdout="1700000000\n", stderr="")
        assert kwargs["cwd"] == component_path
        assert not (component_path / "build").exists()
        assert not egg_info.exists()
        assert not source_cache.exists()
        env = kwargs.get("env")
        assert isinstance(env, dict)
        assert env["SOURCE_DATE_EPOCH"] == "1700000000"
        output_dir = Path(command[command.index("--outdir") + 1])
        wheel_path = output_dir / "lib_example-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as archive:
            archive.write(package_path / "__init__.py", "lib_example/__init__.py")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    builder._build_wheel({"name": "lib_example", "path": "libs/python/lib_example"})

    assert not stale_build.exists()
    assert (tmp_path / "build-output" / "wheels" / "lib_example-0.1.0-py3-none-any.whl").exists()


def test_active_category_builds_prune_unconfigured_wheel_distributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "libs": {"components": [{"name": "lib_common"}]},
        "strategies": {
            "groups": [
                {
                    "name": "indicator",
                    "wheel_distribution": "vynmatrix_indicator",
                }
            ]
        },
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    builder._build_wheel = Mock()

    wheel_names = (
        "lib_common-0.1.0-py3-none-any.whl",
        "lib_orphaned-0.1.0-py3-none-any.whl",
        "vynmatrix_indicator-0.1.0-py3-none-any.whl",
        "vynmatrix_retired-0.1.0-py3-none-any.whl",
        "third_party-1.0.0-py3-none-any.whl",
    )
    for wheel_name in wheel_names:
        (builder.wheels_dir / wheel_name).touch()

    builder.build_all_libs()
    builder.build_all_strategies()

    remaining = {wheel.name for wheel in builder.wheels_dir.glob("*.whl")}
    assert remaining == {
        "lib_common-0.1.0-py3-none-any.whl",
        "third_party-1.0.0-py3-none-any.whl",
        "vynmatrix_indicator-0.1.0-py3-none-any.whl",
    }


def test_wheel_payload_rejects_source_deleted_module(tmp_path: Path) -> None:
    component_path = tmp_path / "component"
    package_path = component_path / "lib_example"
    package_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("\n")
    wheel_path = tmp_path / "lib_example-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(package_path / "__init__.py", "lib_example/__init__.py")
        archive.writestr("lib_example/source_deleted.py", "stale = True\n")

    with pytest.raises(RuntimeError, match=r"source_deleted\.py"):
        Builder._validate_wheel_payload(wheel_path, component_path)


def test_wheel_payload_rejects_cached_bytecode(tmp_path: Path) -> None:
    component_path = tmp_path / "component"
    package_path = component_path / "lib_example"
    package_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("\n")
    wheel_path = tmp_path / "lib_example-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(package_path / "__init__.py", "lib_example/__init__.py")
        archive.writestr("lib_example/__pycache__/__init__.cpython-311.pyc", b"stale")

    with pytest.raises(RuntimeError, match="cached bytecode"):
        Builder._validate_wheel_payload(wheel_path, component_path)


def test_production_wheel_rejects_validation_only_module(tmp_path: Path) -> None:
    component_path = tmp_path / "component"
    module = component_path / "lib_strategy" / "backtest" / "engine.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 1\n")
    wheel_path = tmp_path / "lib_strategy-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(module, "lib_strategy/backtest/engine.py")

    with pytest.raises(RuntimeError, match="validation-only modules"):
        Builder._validate_wheel_payload(wheel_path, component_path)


def test_production_wheel_rejects_development_tool_import(tmp_path: Path) -> None:
    component_path = tmp_path / "component"
    module = component_path / "lib_example" / "runtime.py"
    module.parent.mkdir(parents=True)
    module.write_text("from dev_cli.validation import campaign\n")
    wheel_path = tmp_path / "lib_example-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(module, "lib_example/runtime.py")

    with pytest.raises(RuntimeError, match="development-only tooling"):
        Builder._validate_wheel_payload(wheel_path, component_path)


def test_strategy_wheel_payload_excludes_research_documents(tmp_path: Path) -> None:
    component_path = tmp_path / "strategies" / "indicator"
    strategy_path = component_path / "RegisteredCampaign"
    strategy_path.mkdir(parents=True)
    (strategy_path / "core.py").write_text("VALUE = 1\n")
    (strategy_path / "config.json").write_text("{}\n")
    (strategy_path / "README.md").write_text("Research notes\n")
    (strategy_path / "validation_protocol.json").write_text("{}\n")
    wheel_path = tmp_path / "vynmatrix_indicator-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(strategy_path / "core.py", "RegisteredCampaign/core.py")
        archive.write(strategy_path / "config.json", "RegisteredCampaign/config.json")

    Builder._validate_wheel_payload(
        wheel_path,
        component_path,
        verify_strategy_payload=True,
    )

    with zipfile.ZipFile(wheel_path, "a") as archive:
        archive.write(
            strategy_path / "validation_protocol.json",
            "RegisteredCampaign/validation_protocol.json",
        )

    with pytest.raises(RuntimeError, match="research-only strategy payloads"):
        Builder._validate_wheel_payload(
            wheel_path,
            component_path,
            verify_strategy_payload=True,
        )


def test_strategy_venv_installs_exact_built_strategy_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "build" / "wheels" / "vynmatrix_indicator-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.touch()
    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "apps": {"components": []},
        "strategies": {
            "base_dependencies": [],
            "groups": [
                {
                    "name": "indicator",
                    "path": "strategies/indicator",
                    "dependencies": [],
                    "wheel_distribution": "vynmatrix_indicator",
                    "verify_import": "RegisteredCampaign.core",
                }
            ],
        },
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    create_venv = Mock()
    builder.venv_manager.create_venv = create_venv

    builder.create_venv_for_strategy("indicator")

    create_venv.assert_called_once_with(
        name="strategy-indicator",
        requirements=[str(wheel)],
        import_name="RegisteredCampaign.core",
        constraints_file=tmp_path / "docker" / "constraints.txt",
    )


def test_builder_creates_dedicated_strategy_validation_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_path = tmp_path / "tools" / "dev_cli"
    tool_path.mkdir(parents=True)
    (tool_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    application_path = tmp_path / "apps" / "market_data_ingestor"
    application_path.mkdir(parents=True)
    (application_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    constraints = tmp_path / "docker" / "constraints.txt"
    constraints.parent.mkdir()
    constraints.write_text("click==8.4.1\n")
    names = [
        "lib_common",
        "lib_data",
        "lib_indicators",
        "lib_strategy",
        "lib_infrastructure",
        "lib_application",
        "vynmatrix_indicator",
    ]
    wheel_paths = {
        name: tmp_path / "build" / "wheels" / f"{name}-0.1.0-py3-none-any.whl" for name in names
    }
    verify_imports = [
        "dev_cli",
        "lib_common",
        "lib_data",
        "lib_indicators",
        "lib_strategy",
        "lib_infrastructure",
        "lib_application",
        "lib_data.sessions",
        "RegisteredCampaign.core",
        "market_data_ingestor",
    ]
    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "apps": {
            "components": [
                {
                    "name": "market_data_ingestor",
                    "path": "apps/market_data_ingestor",
                }
            ]
        },
        "strategies": {
            "groups": [
                {
                    "name": "indicator",
                    "wheel_distribution": "vynmatrix_indicator",
                    "verify_import": "RegisteredCampaign.core",
                }
            ]
        },
        "strategy_validation": {
            "venv_name": "strategy-validation",
            "applications": ["market_data_ingestor"],
            "external_requirements": ["pandas==3.0.3", "exchange_calendars==4.13.2"],
            "strategy_group": "indicator",
            "tool_path": "tools/dev_cli",
        },
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    monkeypatch.setattr(builder, "strategy_validation_wheel_paths", lambda: wheel_paths)
    create_venv = Mock()
    builder.venv_manager.create_venv = create_venv

    builder.create_strategy_validation_venv()

    create_venv.assert_called_once_with(
        name="strategy-validation",
        requirements=[
            *(str(path) for path in wheel_paths.values()),
            "pandas==3.0.3",
            "exchange_calendars==4.13.2",
            str(application_path),
        ],
        package_path=tool_path,
        import_names=verify_imports,
        constraints_file=constraints,
    )


def test_validation_wheel_set_rejects_duplicate_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "global": {"build_dir": "build", "python_version": "3.11.13"},
        "libs": {"components": []},
        "strategies": {
            "groups": [
                {
                    "name": "indicator",
                    "path": "strategies/indicator",
                    "wheel_distribution": "vynmatrix_indicator",
                }
            ]
        },
        "strategy_validation": {
            "venv_name": "strategy-validation",
            "libraries": ["lib_common", "lib_common"],
            "strategy_group": "indicator",
            "tool_path": "tools/dev_cli",
        },
    }
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)

    with pytest.raises(ValueError, match="unique non-empty names"):
        Builder(config).strategy_validation_wheel_paths()


def test_exact_validation_payload_rejects_missing_current_module(tmp_path: Path) -> None:
    component_path = tmp_path / "libs" / "python" / "lib_example"
    package_path = component_path / "lib_example"
    package_path.mkdir(parents=True)
    (package_path / "__init__.py").write_text("\n")
    (package_path / "current.py").write_text("CURRENT = True\n")
    wheel_path = tmp_path / "lib_example-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.write(package_path / "__init__.py", "lib_example/__init__.py")

    with pytest.raises(RuntimeError, match=r"stale.*missing=.*current\.py"):
        Builder._validate_wheel_payload(
            wheel_path,
            component_path,
            verify_exact_python_payload=True,
        )


def test_exact_wheel_resolver_rejects_multiple_build_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"global": {"build_dir": "build", "python_version": "3.11.13"}}
    monkeypatch.setattr(builder_module, "_find_repo_root", lambda: tmp_path)
    builder = Builder(config)
    for version in ("0.1.0", "0.2.0"):
        (builder.wheels_dir / f"lib_common-{version}-py3-none-any.whl").touch()

    with pytest.raises(RuntimeError, match="exactly one built wheel"):
        builder._exact_wheel_path("lib_common")


def test_venv_manager_installs_package_last_and_verifies_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venvs_dir = tmp_path / "venvs"
    package_path = tmp_path / "apps" / "market_data_ingestor"
    package_path.mkdir(parents=True)
    (package_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    constraints_file = tmp_path / "constraints.txt"
    constraints_file.write_text("uvicorn==0.27.1\n")
    wheel = tmp_path / "wheels" / "lib_common-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.touch()

    manager = VenvManager(venvs_dir)
    monkeypatch.setattr(manager, "_find_python_executable", lambda: "/python3.11")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "source-shadow"))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manager.create_venv(
        "app-market_data_ingestor",
        [str(wheel)],
        package_path=package_path,
        import_name="market_data_ingestor",
        constraints_file=constraints_file,
    )

    pip_path = str(venvs_dir / "app-market_data_ingestor" / "bin" / "pip")
    python_path = str(venvs_dir / "app-market_data_ingestor" / "bin" / "python")
    install_calls = [command for command, _kwargs in calls if command[:2] == [pip_path, "install"]]
    assert install_calls == [
        [
            pip_path,
            "install",
            "--constraint",
            str(constraints_file),
            "--find-links",
            str(wheel.parent),
            str(wheel),
        ],
        [
            pip_path,
            "install",
            "--constraint",
            str(constraints_file),
            "--find-links",
            str(wheel.parent),
            str(package_path),
        ],
    ]
    assert [python_path, "-m", "pip", "check"] in [command for command, _kwargs in calls]
    import_call, import_kwargs = calls[-2]
    assert import_call[:2] == [python_path, "-c"]
    assert import_call[-1] == "market_data_ingestor"
    assert import_kwargs["cwd"] == venvs_dir
    assert "PYTHONPATH" not in import_kwargs["env"]
    entrypoint_call, entrypoint_kwargs = calls[-1]
    assert entrypoint_call[:2] == [python_path, "-c"]
    assert "packages_distributions" in entrypoint_call[2]
    assert entrypoint_call[-1] == "market_data_ingestor"
    assert entrypoint_kwargs["cwd"] == venvs_dir
    assert "PYTHONPATH" not in entrypoint_kwargs["env"]


def test_venv_manager_cleans_stale_package_artifacts_before_source_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_path = tmp_path / "apps" / "execution_engine"
    source_package = package_path / "execution_engine"
    source_package.mkdir(parents=True)
    (package_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    (source_package / "__init__.py").write_text("\n")
    stale_paths = [
        package_path / "build" / "lib" / "execution_engine" / "deleted.py",
        package_path / "dist" / "old.whl",
        package_path / "execution_engine.egg-info" / "SOURCES.txt",
        source_package / "__pycache__" / "deleted.cpython-311.pyc",
    ]
    for path in stale_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale\n")

    manager = VenvManager(tmp_path / "venvs")
    monkeypatch.setattr(manager, "_find_python_executable", lambda: "/python3.11")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == [
            str(tmp_path / "venvs" / "app-execution_engine" / "bin" / "pip"),
            "install",
        ]:
            assert not any(path.exists() for path in stale_paths)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manager.create_venv(
        "app-execution_engine",
        [],
        package_path=package_path,
        import_name="execution_engine",
    )

    assert not any(path.exists() for path in stale_paths)


def test_venv_manager_rejects_missing_package_metadata_before_recreation(
    tmp_path: Path,
) -> None:
    venvs_dir = tmp_path / "venvs"
    existing_venv = venvs_dir / "app-market_data_ingestor"
    existing_venv.mkdir(parents=True)
    marker = existing_venv / "keep"
    marker.touch()
    package_path = tmp_path / "not-a-package"
    package_path.mkdir()

    manager = VenvManager(venvs_dir)

    with pytest.raises(FileNotFoundError, match="No Python package metadata"):
        manager.create_venv(
            "app-market_data_ingestor",
            [],
            package_path=package_path,
        )

    assert marker.exists()


def test_venv_manager_rejects_duplicate_import_verification_before_recreation(
    tmp_path: Path,
) -> None:
    existing_venv = tmp_path / "venvs" / "strategy-validation"
    existing_venv.mkdir(parents=True)
    marker = existing_venv / "keep"
    marker.touch()
    manager = VenvManager(tmp_path / "venvs")

    with pytest.raises(ValueError, match="duplicate package names"):
        manager.create_venv(
            "strategy-validation",
            [],
            import_name="lib_common",
            import_names=["lib_common"],
        )

    assert marker.exists()


def test_venv_manager_installs_app_with_no_separate_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_path = tmp_path / "apps" / "standalone_app"
    package_path.mkdir(parents=True)
    (package_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    manager = VenvManager(tmp_path / "venvs")
    monkeypatch.setattr(manager, "_find_python_executable", lambda: "/python3.11")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manager.create_venv(
        "app-standalone_app",
        [],
        package_path=package_path,
        import_name="standalone_app",
    )

    pip_path = str(tmp_path / "venvs" / "app-standalone_app" / "bin" / "pip")
    assert [pip_path, "install", str(package_path)] in calls
