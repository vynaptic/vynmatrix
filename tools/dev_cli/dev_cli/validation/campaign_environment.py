"""Installed-runtime validation for strategy campaigns."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import platform
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from dev_cli.validation.campaign_composition import CampaignMixinContract
from dev_cli.validation.campaign_contracts import (
    _CONTAINER_REFERENCE_RE,
    _runtime_distribution_snapshot,
    _validated_frozen_runtime_distribution_lock,
)
from dev_cli.validation.evidence import file_sha256
from dev_cli.validation.execution_environment import (
    container_repository,
    installed_vmdev_payload,
    installed_wheel_payload_sha256,
    local_container_image_id,
)
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import PureSignalStrategy


def load_installed_pure_strategy_core(
    module_name: str,
    *,
    expected_class_name: str,
    expected_source_sha256: str,
) -> type[PureSignalStrategy]:
    """Load an exact strategy core from the active validation environment."""

    module = importlib.import_module(module_name)
    raw_origin = getattr(module, "__file__", None)
    if not raw_origin:
        message = f"Installed strategy module has no file origin: {module_name}"
        raise RuntimeError(message)
    origin = Path(raw_origin).resolve()
    try:
        origin.relative_to(Path(sys.prefix).resolve())
    except ValueError as exc:
        message = f"Strategy module is not installed in the active environment: {origin}"
        raise RuntimeError(message) from exc

    if file_sha256(origin) != expected_source_sha256:
        message = f"Installed strategy core differs from frozen source: {origin}"
        raise RuntimeError(message)
    candidate = getattr(module, expected_class_name, None)
    if (
        not inspect.isclass(candidate)
        or not issubclass(candidate, PureSignalStrategy)
        or candidate is PureSignalStrategy
        or candidate.__module__ != module.__name__
    ):
        message = (
            f"Expected installed PureSignalStrategy class {expected_class_name!r} "
            f"was not found in {module_name}"
        )
        raise RuntimeError(message)
    return candidate


class CampaignEnvironmentMixin(CampaignMixinContract):
    """Validate source and installed execution environments for campaigns."""

    _container_repository = staticmethod(container_repository)
    _local_container_image_id = staticmethod(local_container_image_id)

    def _load_and_validate_core(
        self,
        strategy_path: Path,
        *,
        protocol: Mapping[str, Any],
        runtime_config: Mapping[str, Any],
    ) -> type[Any]:
        strategy = self._mapping(protocol, "strategy")
        if runtime_config.get("runner_kind") != "signal_worker":
            message = "strategy validator only supports production signal_worker cores"
            raise ValueError(message)
        return cast(
            type[Any],
            load_pure_strategy_core(
                strategy_path,
                expected_class_name=str(strategy["core_class"]),
            ),
        )

    def _load_installed_core(self, manifest: Mapping[str, Any]) -> type[Any]:
        strategy = self._mapping(manifest, "strategy")
        strategy_directory = Path(str(strategy["core_path"])).parent.name
        core_class = load_installed_pure_strategy_core(
            f"{strategy_directory}.core",
            expected_class_name=str(strategy["core_class"]),
            expected_source_sha256=str(strategy["core_sha256"]),
        )
        installed = self._mapping(self._mapping(manifest, "environment"), "installed_artifacts")
        expected_path = Path(str(installed["installed_strategy_core_path"])).resolve()
        actual_path = Path(str(sys.modules[core_class.__module__].__file__)).resolve()
        if actual_path != expected_path:
            message = "installed strategy core path differs from the frozen environment"
            raise RuntimeError(message)
        return cast(type[Any], core_class)

    def _environment_manifest(
        self,
        strategy_path: Path,
        *,
        execution_environment: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        validation_root = self._repo_root / "tools" / "dev_cli" / "dev_cli" / "validation"
        validation_sources = (
            path for path in validation_root.rglob("*.py") if "__pycache__" not in path.parts
        )
        source_files = sorted(
            {
                strategy_path / "core.py",
                strategy_path / "config.json",
                strategy_path / "validation_protocol.json",
                *validation_sources,
            },
            key=lambda path: path.as_posix(),
        )
        distributions, distribution_lock_sha256 = _runtime_distribution_snapshot()
        git_commit = self._git_output(["rev-parse", "HEAD"])
        dirty_paths = self._git_output(["status", "--porcelain=v1"]).splitlines()
        manifest = {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "repo_commit": git_commit,
            "dirty_tree": bool(dirty_paths),
            "dirty_path_count": len(dirty_paths),
            "runtime_distribution_lock_sha256": distribution_lock_sha256,
            "runtime_distributions": list(distributions),
            "source_files": {
                self._relative(path): self._file_sha256(path) for path in source_files
            },
            "execution_installation": "source_tree",
            "container_execution": False,
            "container_artifact_attested": False,
            "container_digests": {},
            "execution_authorized": False,
        }
        if execution_environment is not None:
            installed_artifacts = self._validate_installed_artifacts(
                strategy_path,
                execution_environment,
            )
            manifest["installed_artifacts"] = installed_artifacts
            manifest["execution_installation"] = "installed_wheels_and_pinned_container"
            manifest["container_artifact_attested"] = True
            manifest["container_digests"] = installed_artifacts["container_image_digests"]
            manifest["execution_authorized"] = True
        return manifest

    def _validate_installed_artifacts(  # noqa: PLR0912, PLR0915
        self,
        strategy_path: Path,
        attestation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if attestation.get("schema_version") != "1.0":
            message = "execution-environment schema_version must be 1.0"
            raise ValueError(message)
        # Preserve the venv's own executable path rather than resolving its
        # symlink to the shared base interpreter. Two venvs commonly point at
        # the same base binary but do not contain the same installed payload.
        interpreter = Path(str(attestation.get("venv_python", ""))).absolute()
        if interpreter != Path(sys.executable).absolute():
            message = "vmdev must run from the exact attested validation virtual environment"
            raise ValueError(message)
        if not interpreter.is_file():
            message = f"attested virtual-environment interpreter does not exist: {interpreter}"
            raise FileNotFoundError(message)
        interpreter_sha256 = self._file_sha256(interpreter)
        if attestation.get("venv_python_sha256") != interpreter_sha256:
            message = "attested virtual-environment interpreter digest differs from sys.executable"
            raise ValueError(message)
        observed_vmdev = installed_vmdev_payload(self._repo_root, Path(sys.prefix))
        if attestation.get("vmdev") != observed_vmdev:
            message = "attested vmdev runner payload differs from the active environment"
            raise ValueError(message)

        wheel_rows = self._list_of_mappings(attestation, "wheels")
        required_names = {
            "lib_application",
            "lib_common",
            "lib_data",
            "lib_indicators",
            "lib_infrastructure",
            "lib_strategy",
            "vynmatrix_indicator",
        }
        by_name = {str(row.get("name")): row for row in wheel_rows}
        if len(wheel_rows) != len(required_names) or set(by_name) != required_names:
            message = "execution-environment wheels must identify the exact validation wheel set"
            raise ValueError(message)
        normalized_wheels: list[dict[str, str]] = []
        for name in sorted(by_name):
            path = Path(str(by_name[name].get("path", ""))).resolve()
            self._require_within_repo(path)
            actual = self._file_sha256(path)
            if by_name[name].get("sha256") != actual:
                message = f"attested wheel digest differs from wheel payload: {name}"
                raise ValueError(message)
            installed_payload_sha256 = installed_wheel_payload_sha256(
                path,
                name,
                installation_root=Path(sys.prefix),
            )
            if by_name[name].get("installed_payload_sha256") != installed_payload_sha256:
                message = f"attested installed payload digest differs from active venv: {name}"
                raise ValueError(message)
            normalized_wheels.append(
                {
                    "name": name,
                    "path": self._relative(path),
                    "sha256": actual,
                    "installed_payload_sha256": installed_payload_sha256,
                }
            )

        strategy_wheel = Path(
            next(row["path"] for row in normalized_wheels if row["name"] == "vynmatrix_indicator")
        )
        strategy_wheel = self._repo_root / strategy_wheel
        payload_paths = self._mapping(attestation, "strategy_payload_paths")
        payload_hashes = self._mapping(attestation, "strategy_payload_sha256")
        required_payload = {
            "core": strategy_path / "core.py",
            "config": strategy_path / "config.json",
            "protocol": strategy_path / "validation_protocol.json",
        }
        if set(payload_paths) != set(required_payload) or set(payload_hashes) != set(
            required_payload
        ):
            message = "strategy payload paths and hashes must identify core, config, and protocol"
            raise ValueError(message)
        source_hashes = {
            key: self._file_sha256(source_path) for key, source_path in required_payload.items()
        }
        if any(payload_hashes[key] != source_hashes[key] for key in required_payload):
            message = "attested strategy payload digest differs from checked-in source"
            raise ValueError(message)
        with zipfile.ZipFile(strategy_wheel) as archive:
            archive_names = set(archive.namelist())
            for key in ("core", "config"):
                archive_path = str(payload_paths[key])
                if archive_path not in archive_names:
                    message = f"strategy wheel omits registered {key}: {archive_path}"
                    raise ValueError(message)
                if hashlib.sha256(archive.read(archive_path)).hexdigest() != source_hashes[key]:
                    message = f"strategy wheel {key} differs from checked-in source"
                    raise ValueError(message)
            protocol_archive_path = str(payload_paths["protocol"])
            if protocol_archive_path in archive_names:
                message = "strategy wheel contains research-only validation protocol"
                raise ValueError(message)

        protocol = self._load_json_object(strategy_path / "validation_protocol.json")
        expected_class = str(self._mapping(protocol, "strategy")["core_class"])
        installed_class = load_installed_pure_strategy_core(
            f"{strategy_path.name}.core",
            expected_class_name=expected_class,
            expected_source_sha256=self._file_sha256(strategy_path / "core.py"),
        )
        installed_module = sys.modules[installed_class.__module__]
        installed_core_path = Path(str(installed_module.__file__)).resolve()

        raw_digests = self._mapping(attestation, "container_image_digests")
        if not raw_digests:
            message = "at least one immutable container image digest is required"
            raise ValueError(message)
        container_digests: dict[str, str] = {}
        for name, digest in raw_digests.items():
            value = str(digest)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                message = f"container image {name} must use an immutable sha256 digest"
                raise ValueError(message)
            container_digests[str(name)] = value
        if "indicator-runner" not in container_digests:
            message = "indicator-runner container digest is required"
            raise ValueError(message)
        raw_references = self._mapping(attestation, "container_image_references")
        if set(raw_references) != set(container_digests):
            message = "container image references must match the attested digest names"
            raise ValueError(message)
        container_references = {
            str(name): str(reference) for name, reference in raw_references.items()
        }
        for name, reference in container_references.items():
            expected_repository = f"vynmatrix/{name}"
            if (
                not _CONTAINER_REFERENCE_RE.fullmatch(reference)
                or self._container_repository(reference) != expected_repository
            ):
                message = f"container image {name} does not use {expected_repository}"
                raise ValueError(message)
            if self._local_container_image_id(reference) != container_digests[name]:
                message = f"local container image digest differs from attestation: {name}"
                raise ValueError(message)

        return {
            "schema_version": "1.0",
            "venv_python": str(interpreter),
            "venv_python_sha256": interpreter_sha256,
            "vmdev": observed_vmdev,
            "wheels": normalized_wheels,
            "strategy_payload_paths": dict(payload_paths),
            "strategy_payload_sha256": dict(payload_hashes),
            "validation_protocol_path": self._relative(required_payload["protocol"]),
            "validation_protocol_sha256": source_hashes["protocol"],
            "installed_strategy_core_path": str(installed_core_path),
            "installed_strategy_core_sha256": self._file_sha256(installed_core_path),
            "container_image_digests": dict(sorted(container_digests.items())),
            "container_image_references": dict(sorted(container_references.items())),
        }

    def _require_execution_environment(self, manifest: Mapping[str, Any]) -> None:
        """Verify frozen runtime/wheels plus local container execution artifacts."""

        installed = self._require_frozen_runtime_environment(manifest)
        protocol_path = (
            self._repo_root / str(installed.get("validation_protocol_path", ""))
        ).resolve()
        self._require_within_repo(protocol_path)
        if not protocol_path.is_file() or self._file_sha256(protocol_path) != installed.get(
            "validation_protocol_sha256"
        ):
            message = "frozen validation protocol changed or disappeared"
            raise RuntimeError(message)
        container_digests = self._mapping(installed, "container_image_digests")
        container_references = self._mapping(installed, "container_image_references")
        for name, reference in container_references.items():
            if self._local_container_image_id(str(reference)) != container_digests[name]:
                message = f"frozen local container image changed or disappeared: {name}"
                raise RuntimeError(message)

    def _require_frozen_runtime_environment(
        self,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify code-bearing runtime artifacts without requiring a local image."""

        environment = self._mapping(manifest, "environment")
        if environment.get("execution_authorized") is not True:
            message = (
                "manifest is registration-only; rebuild wheels/venv/container and freeze an "
                "installed-artifact manifest before executing trials"
            )
            raise RuntimeError(message)
        if environment.get("execution_installation") != "installed_wheels_and_pinned_container":
            message = "authorized execution manifest has an invalid installation mode"
            raise RuntimeError(message)
        if environment.get("container_artifact_attested") is not True:
            message = "authorized execution manifest lacks an attested container artifact"
            raise RuntimeError(message)
        try:
            frozen_distributions, frozen_distribution_sha256 = (
                _validated_frozen_runtime_distribution_lock(environment)
            )
            active_distributions, active_distribution_sha256 = _runtime_distribution_snapshot()
        except (TypeError, ValueError) as exc:
            message = f"runtime distribution lock validation failed: {exc}"
            raise RuntimeError(message) from exc
        if (
            active_distributions != frozen_distributions
            or active_distribution_sha256 != frozen_distribution_sha256
        ):
            message = "active runtime distributions differ from the frozen manifest"
            raise RuntimeError(message)
        installed = self._mapping(environment, "installed_artifacts")
        interpreter = Path(str(installed.get("venv_python", ""))).absolute()
        if interpreter != Path(sys.executable).absolute():
            message = "active interpreter differs from the frozen validation environment"
            raise RuntimeError(message)
        if not interpreter.is_file() or self._file_sha256(interpreter) != installed.get(
            "venv_python_sha256"
        ):
            message = "frozen validation interpreter changed or disappeared"
            raise RuntimeError(message)
        try:
            observed_vmdev = installed_vmdev_payload(self._repo_root, Path(sys.prefix))
        except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
            message = f"frozen vmdev runner validation failed: {exc}"
            raise RuntimeError(message) from exc
        if installed.get("vmdev") != observed_vmdev:
            message = "active vmdev runner differs from the frozen manifest"
            raise RuntimeError(message)
        for wheel in self._list_of_mappings(installed, "wheels"):
            wheel_path = (self._repo_root / str(wheel["path"])).resolve()
            self._require_within_repo(wheel_path)
            if not wheel_path.is_file() or self._file_sha256(wheel_path) != wheel.get("sha256"):
                message = f"frozen wheel changed or disappeared: {wheel.get('name')}"
                raise RuntimeError(message)
            installed_payload_sha256 = installed_wheel_payload_sha256(
                wheel_path,
                str(wheel.get("name")),
                installation_root=Path(sys.prefix),
            )
            if installed_payload_sha256 != wheel.get("installed_payload_sha256"):
                message = f"frozen installed wheel payload changed: {wheel.get('name')}"
                raise RuntimeError(message)
        core_path = Path(str(installed.get("installed_strategy_core_path", ""))).resolve()
        if not core_path.is_file() or self._file_sha256(core_path) != installed.get(
            "installed_strategy_core_sha256"
        ):
            message = "frozen installed strategy core changed or disappeared"
            raise RuntimeError(message)
        container_digests = self._mapping(installed, "container_image_digests")
        container_references = self._mapping(installed, "container_image_references")
        if (
            set(container_digests) != set(container_references)
            or "indicator-runner" not in container_digests
        ):
            message = "frozen container references and digests are incomplete"
            raise RuntimeError(message)
        return installed

    def _git_output(self, args: list[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self._repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()


__all__ = ["CampaignEnvironmentMixin", "load_installed_pure_strategy_core"]
