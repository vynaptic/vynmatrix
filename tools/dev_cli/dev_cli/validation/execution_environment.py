"""Installed-runtime and container attestation for strategy validation."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from dev_cli.core.builder import Builder
from dev_cli.core.config import load_config
from dev_cli.utils.helpers import find_python_executable
from dev_cli.validation.evidence import (
    atomic_replace_bytes,
    canonical_json_bytes,
    file_sha256,
    require_descendant,
    resolve_strategy_validation_artifact,
)

_CONTAINER_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CONTAINER_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]*$")
_DISTRIBUTION_NAME_RE = re.compile(r"[-_.]+")
_INSTALLED_PAYLOAD_SCHEMA = b"vynmatrix-installed-wheel-payload-v1\0"
_STRATEGY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_VMDEV_PAYLOAD_SCHEMA = b"vynmatrix-installed-vmdev-payload-v1\0"


def attest_execution_environment(
    strategy_name: str,
    container_image_values: tuple[str, ...],
    output: Path | None,
) -> tuple[dict[str, Any], Path]:
    """Verify and persist one strategy's installed execution environment."""

    container_references, container_digests = _inspect_container_images(container_image_values)
    payload, destination = _create_execution_attestation(
        builder=Builder(load_config()),
        strategy_name=strategy_name,
        container_references=container_references,
        container_digests=container_digests,
        output=output,
    )
    atomic_replace_bytes(destination, canonical_json_bytes(payload))
    return payload, destination


def container_repository(reference: str) -> str:
    """Return a container reference without its tag or digest."""

    without_digest = reference.split("@", maxsplit=1)[0]
    slash_index = without_digest.rfind("/")
    colon_index = without_digest.rfind(":")
    return without_digest[:colon_index] if colon_index > slash_index else without_digest


def local_container_image_id(reference: str) -> str:
    """Return one immutable local Docker image ID for a resolved reference."""

    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = f"locally built container image is unavailable: {reference}"
        raise RuntimeError(message)
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(output_lines) != 1 or not _CONTAINER_DIGEST_RE.fullmatch(output_lines[0]):
        message = f"docker returned no immutable image ID for {reference}"
        raise RuntimeError(message)
    return output_lines[0]


def installed_wheel_payload_sha256(
    wheel_path: Path,
    distribution_name: str,
    *,
    installation_root: Path,
) -> str:
    """Hash and verify the exact wheel members installed below one environment.

    Wheel ``RECORD`` is excluded because installers may legitimately rewrite it.
    Every other member is framed by path and byte length before hashing, so the
    aggregate is deterministic and cannot be changed by path/payload boundary
    ambiguity.
    """

    wheel = wheel_path.resolve()
    if not wheel.is_file():
        message = f"installed-payload wheel does not exist: {wheel}"
        raise FileNotFoundError(message)
    normalized_name = _normalize_distribution_name(distribution_name)
    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        message = f"validation wheel is not installed: {distribution_name}"
        raise RuntimeError(message) from exc
    observed_name = _normalize_distribution_name(str(distribution.metadata["Name"]))
    if observed_name != normalized_name:
        message = f"installed distribution identity differs for {distribution_name}"
        raise RuntimeError(message)

    site_root = Path(str(distribution.locate_file(""))).resolve()
    required_root = installation_root.resolve()
    if site_root == required_root or required_root not in site_root.parents:
        message = f"installed distribution escapes the active environment: {distribution_name}"
        raise ValueError(message)

    digest = hashlib.sha256(_INSTALLED_PAYLOAD_SCHEMA)
    with zipfile.ZipFile(wheel) as archive:
        members = sorted(
            (
                member
                for member in archive.infolist()
                if not member.is_dir() and not member.filename.endswith(".dist-info/RECORD")
            ),
            key=lambda member: member.filename,
        )
        if not members:
            message = f"validation wheel has no attestable payload: {wheel.name}"
            raise ValueError(message)
        for member in members:
            relative = _safe_wheel_member(member.filename)
            installed_path = Path(str(distribution.locate_file(relative))).resolve()
            if installed_path == site_root or site_root not in installed_path.parents:
                message = f"installed wheel member escapes site-packages: {member.filename!r}"
                raise ValueError(message)
            installed_payload = installed_path.read_bytes()
            if installed_payload != archive.read(member.filename):
                message = f"installed wheel payload differs from {wheel.name}: {member.filename}"
                raise RuntimeError(message)
            path_payload = member.filename.encode("utf-8")
            for framed in (path_payload, installed_payload):
                digest.update(len(framed).to_bytes(8, byteorder="big", signed=False))
                digest.update(framed)
    return digest.hexdigest()


def _create_execution_attestation(
    *,
    builder: Builder,
    strategy_name: str,
    container_references: Mapping[str, str],
    container_digests: Mapping[str, str],
    output: Path | None,
) -> tuple[dict[str, Any], Path]:
    if not _STRATEGY_NAME_RE.fullmatch(strategy_name):
        message = f"invalid strategy name: {strategy_name!r}"
        raise ValueError(message)

    repo_root = builder.root_dir.resolve()
    expected_venv = builder.strategy_validation_venv_path()
    expected_python = find_python_executable(expected_venv).absolute()
    active_python = Path(sys.executable).absolute()
    if os.path.normcase(str(active_python)) != os.path.normcase(str(expected_python)):
        message = (
            "vmdev strategy attest must run from build/venvs/strategy-validation; "
            f"active interpreter is {active_python}"
        )
        raise RuntimeError(message)
    if Path(sys.prefix).resolve() != expected_venv.resolve():
        message = "active Python prefix is not the strategy-validation virtual environment"
        raise RuntimeError(message)

    vmdev_payload = installed_vmdev_payload(repo_root, expected_venv)
    wheel_paths = builder.strategy_validation_wheel_paths()
    strategy_distribution = next(reversed(wheel_paths))
    library_distributions = tuple(wheel_paths)[:-1]

    wheel_rows: list[dict[str, str]] = []
    distributions: dict[str, importlib.metadata.Distribution] = {}
    for name, wheel_path in wheel_paths.items():
        distribution = _verify_installed_wheel(name, wheel_path, expected_venv)
        distributions[name] = distribution
        wheel_rows.append(
            {
                "name": name,
                "path": str(wheel_path),
                "sha256": file_sha256(wheel_path),
            }
        )

    for distribution_name in library_distributions:
        _verify_runtime_import(
            distribution_name,
            distributions[distribution_name],
            expected_relative=PurePosixPath(distribution_name) / "__init__.py",
        )

    for row in wheel_rows:
        row["installed_payload_sha256"] = installed_wheel_payload_sha256(
            Path(row["path"]),
            row["name"],
            installation_root=expected_venv,
        )

    strategy_root = (repo_root / "strategies" / "indicator").resolve()
    strategy_path = (strategy_root / strategy_name).resolve()
    require_descendant(strategy_path, strategy_root, field="strategy path")
    payload_paths, payload_hashes = _verify_strategy_payload(
        strategy_name,
        strategy_path,
        wheel_paths[strategy_distribution],
    )
    _verify_runtime_import(
        f"{strategy_name}.core",
        distributions[strategy_distribution],
        expected_relative=PurePosixPath(payload_paths["core"]),
    )

    destination = _resolve_attestation_output(repo_root, strategy_name, output)
    return (
        {
            "schema_version": "1.0",
            "venv_python": str(active_python),
            "venv_python_sha256": file_sha256(active_python),
            "vmdev": vmdev_payload,
            "wheels": wheel_rows,
            "strategy_payload_paths": payload_paths,
            "strategy_payload_sha256": payload_hashes,
            "container_image_digests": dict(sorted(container_digests.items())),
            "container_image_references": dict(sorted(container_references.items())),
        },
        destination,
    )


def _inspect_container_images(
    values: tuple[str, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    references: dict[str, str] = {}
    digests: dict[str, str] = {}
    for value in values:
        name, separator, reference = value.partition("=")
        if not separator or not _CONTAINER_NAME_RE.fullmatch(name):
            message = f"container image must use NAME=IMAGE_REF: {value!r}"
            raise ValueError(message)
        if name in references:
            message = f"duplicate container image name: {name}"
            raise ValueError(message)
        if not _CONTAINER_REFERENCE_RE.fullmatch(reference):
            message = f"container image reference is invalid: {reference!r}"
            raise ValueError(message)
        expected_repository = f"vynmatrix/{name}"
        if container_repository(reference) != expected_repository:
            message = (
                f"container image {name} must reference the intended local repository "
                f"{expected_repository}"
            )
            raise ValueError(message)
        references[name] = reference
        digests[name] = local_container_image_id(reference)
    if "indicator-runner" not in references:
        message = "indicator-runner container image is required"
        raise ValueError(message)
    return references, digests


def _verify_installed_wheel(
    expected_name: str,
    wheel_path: Path,
    expected_venv: Path,
) -> importlib.metadata.Distribution:
    try:
        distribution = importlib.metadata.distribution(expected_name)
    except importlib.metadata.PackageNotFoundError as exc:
        message = f"validation wheel is not installed: {expected_name}"
        raise RuntimeError(message) from exc

    site_root = Path(str(distribution.locate_file(""))).resolve()
    require_descendant(
        site_root,
        expected_venv.resolve(),
        field=f"{expected_name} installation",
    )
    installed_name = _normalize_distribution_name(str(distribution.metadata["Name"]))
    if installed_name != expected_name:
        message = f"installed distribution identity differs for {expected_name}"
        raise RuntimeError(message)

    with zipfile.ZipFile(wheel_path) as archive:
        wheel_name, wheel_version = _wheel_identity(archive)
        if wheel_name != expected_name or wheel_version != distribution.version:
            message = f"installed distribution differs from exact wheel: {expected_name}"
            raise RuntimeError(message)
        for member in archive.infolist():
            if member.is_dir() or member.filename.endswith(".dist-info/RECORD"):
                continue
            relative = _safe_wheel_member(member.filename)
            installed_path = Path(str(distribution.locate_file(relative))).resolve()
            require_descendant(
                installed_path,
                site_root,
                field=f"{expected_name} installed wheel member",
            )
            if not installed_path.is_file() or installed_path.read_bytes() != archive.read(
                member.filename
            ):
                message = f"installed payload differs from {wheel_path.name}: {member.filename}"
                raise RuntimeError(message)
    return distribution


def _wheel_identity(archive: zipfile.ZipFile) -> tuple[str, str]:
    metadata_names = [
        name
        for name in archive.namelist()
        if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        message = "wheel must contain exactly one top-level METADATA record"
        raise RuntimeError(message)
    fields: dict[str, str] = {}
    for line in archive.read(metadata_names[0]).decode("utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"Name", "Version"} and key not in fields:
            fields[key] = value.strip()
    if set(fields) != {"Name", "Version"}:
        message = "wheel METADATA omits Name or Version"
        raise RuntimeError(message)
    return _normalize_distribution_name(fields["Name"]), fields["Version"]


def _verify_runtime_import(
    module_name: str,
    distribution: importlib.metadata.Distribution,
    *,
    expected_relative: PurePosixPath,
) -> None:
    module = importlib.import_module(module_name)
    origin_value = getattr(module, "__file__", None)
    if not origin_value:
        message = f"installed module has no concrete source path: {module_name}"
        raise RuntimeError(message)
    origin = Path(str(origin_value)).resolve()
    expected = Path(str(distribution.locate_file(expected_relative))).resolve()
    if origin != expected:
        message = f"source-tree or foreign import shadows installed wheel: {module_name}"
        raise RuntimeError(message)


def installed_vmdev_payload(
    repo_root: Path,
    expected_venv: Path,
) -> dict[str, Any]:
    """Attest the exact installed runner and matching repository source."""

    try:
        distribution = importlib.metadata.distribution("vmdev")
    except importlib.metadata.PackageNotFoundError as exc:
        message = "tools/dev_cli is not installed in the validation virtual environment"
        raise RuntimeError(message) from exc
    site_root = Path(str(distribution.locate_file(""))).resolve()
    require_descendant(site_root, expected_venv.resolve(), field="vmdev installation")
    source_root = repo_root / "tools" / "dev_cli" / "dev_cli"
    installed_root = Path(str(distribution.locate_file("dev_cli"))).resolve()
    source_files = {
        path.relative_to(source_root): path
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    installed_files = {
        path.relative_to(installed_root): path
        for path in installed_root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    if set(source_files) != set(installed_files):
        message = "installed vmdev file set differs from current tools/dev_cli source"
        raise RuntimeError(message)
    for relative, source in source_files.items():
        if source.read_bytes() != installed_files[relative].read_bytes():
            message = f"installed vmdev is stale relative to source: {relative}"
            raise RuntimeError(message)
    expected_module = (installed_root / "validation" / "execution_environment.py").resolve()
    if Path(__file__).resolve() != expected_module:
        message = "execution-environment validation is source-loaded instead of installed"
        raise RuntimeError(message)
    entry_points = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    if entry_points.get("vmdev") != "dev_cli.main:cli":
        message = "installed vmdev console entry point is missing or changed"
        raise RuntimeError(message)
    file_hashes = {
        relative.as_posix(): file_sha256(source_files[relative])
        for relative in sorted(source_files, key=lambda path: path.as_posix())
    }
    payload: dict[str, Any] = {
        "distribution": "vmdev",
        "version": str(distribution.version),
        "console_entry_point": entry_points["vmdev"],
        "file_count": len(file_hashes),
        "files": file_hashes,
    }
    payload["payload_sha256"] = hashlib.sha256(
        _VMDEV_PAYLOAD_SCHEMA + canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def _verify_strategy_payload(
    strategy_name: str,
    strategy_path: Path,
    wheel_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    payload_paths = {
        "core": f"{strategy_name}/core.py",
        "config": f"{strategy_name}/config.json",
        "protocol": f"{strategy_name}/validation_protocol.json",
    }
    source_paths = {
        key: strategy_path / Path(relative).name for key, relative in payload_paths.items()
    }
    for key, source in source_paths.items():
        if not source.is_file():
            message = f"strategy {key} source is missing: {source}"
            raise FileNotFoundError(message)
    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        for key in ("core", "config"):
            relative = payload_paths[key]
            _safe_wheel_member(relative)
            if relative not in names or archive.read(relative) != source_paths[key].read_bytes():
                message = f"strategy wheel {key} differs from current source"
                raise RuntimeError(message)
        if payload_paths["protocol"] in names:
            message = "strategy wheel contains research-only validation protocol"
            raise RuntimeError(message)
    hashes = {key: file_sha256(source) for key, source in source_paths.items()}
    return payload_paths, hashes


def _safe_wheel_member(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        message = f"wheel member escapes its installation root: {value!r}"
        raise ValueError(message)
    return path


def _resolve_attestation_output(
    repo_root: Path,
    strategy_name: str,
    output: Path | None,
) -> Path:
    return resolve_strategy_validation_artifact(
        repo_root,
        expected_name=f"{strategy_name}-execution-environment.json",
        output=output,
        field="attestation output",
    )


def _normalize_distribution_name(value: str) -> str:
    return _DISTRIBUTION_NAME_RE.sub("_", value).lower()


__all__ = [
    "attest_execution_environment",
    "container_repository",
    "installed_wheel_payload_sha256",
    "local_container_image_id",
]
