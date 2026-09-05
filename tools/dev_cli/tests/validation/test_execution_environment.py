"""Execution-environment attestation contracts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest

from dev_cli.validation import execution_environment
from dev_cli.validation.execution_environment import (
    _inspect_container_images,
    _resolve_attestation_output,
    _safe_wheel_member,
    _verify_installed_wheel,
    _verify_strategy_payload,
    installed_vmdev_payload,
)


def test_container_image_inspection_derives_digest_and_rejects_fabrication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'0' * 64}"
    inspected_commands: list[list[str]] = []

    def inspect_success(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        inspected_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"{digest}\n", stderr="")

    monkeypatch.setattr(execution_environment.subprocess, "run", inspect_success)
    references, digests = _inspect_container_images(
        ("indicator-runner=vynmatrix/indicator-runner:latest",)
    )

    assert references == {"indicator-runner": "vynmatrix/indicator-runner:latest"}
    assert digests == {"indicator-runner": digest}
    assert inspected_commands == [
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "vynmatrix/indicator-runner:latest",
        ]
    ]
    with pytest.raises(ValueError, match="duplicate container"):
        _inspect_container_images(
            (
                "indicator-runner=vynmatrix/indicator-runner:latest",
                "indicator-runner=vynmatrix/indicator-runner:latest",
            )
        )

    def inspect_missing(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such image")

    monkeypatch.setattr(execution_environment.subprocess, "run", inspect_missing)
    with pytest.raises(RuntimeError, match="locally built container image is unavailable"):
        _inspect_container_images(("indicator-runner=vynmatrix/indicator-runner:fabricated",))


def test_attestation_output_is_confined_to_ignored_artifact_root(tmp_path: Path) -> None:
    default = _resolve_attestation_output(tmp_path, "RegisteredCampaign", None)

    assert default == (
        tmp_path
        / ".artifacts"
        / "research"
        / "strategy-validation"
        / "RegisteredCampaign-execution-environment.json"
    )
    with pytest.raises(ValueError, match="attestation output escapes"):
        _resolve_attestation_output(
            tmp_path,
            "RegisteredCampaign",
            tmp_path / "outside.json",
        )


def test_strategy_payload_attestation_records_exact_paths_and_hashes(tmp_path: Path) -> None:
    strategy_name = "RegisteredCampaign"
    strategy_path = tmp_path / strategy_name
    strategy_path.mkdir()
    contents = {
        "core.py": b"VALUE = 1\n",
        "config.json": b"{}\n",
        "validation_protocol.json": b'{"protocol_version":"1.0"}\n',
    }
    for filename, content in contents.items():
        (strategy_path / filename).write_bytes(content)
    wheel = tmp_path / "vynmatrix_indicator-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for filename in ("core.py", "config.json"):
            content = contents[filename]
            archive.writestr(f"{strategy_name}/{filename}", content)

    paths, hashes = _verify_strategy_payload(strategy_name, strategy_path, wheel)

    assert paths == {
        "core": "RegisteredCampaign/core.py",
        "config": "RegisteredCampaign/config.json",
        "protocol": "RegisteredCampaign/validation_protocol.json",
    }
    assert hashes == {
        "core": hashlib.sha256(contents["core.py"]).hexdigest(),
        "config": hashlib.sha256(contents["config.json"]).hexdigest(),
        "protocol": hashlib.sha256(contents["validation_protocol.json"]).hexdigest(),
    }

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(
            f"{strategy_name}/validation_protocol.json",
            contents["validation_protocol.json"],
        )
    with pytest.raises(RuntimeError, match="research-only validation protocol"):
        _verify_strategy_payload(strategy_name, strategy_path, wheel)


def test_wheel_member_rejects_path_escape() -> None:
    with pytest.raises(ValueError, match="escapes"):
        _safe_wheel_member("../RegisteredCampaign/core.py")


def test_installed_wheel_attestation_compares_exact_installed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv = tmp_path / "strategy-validation"
    site = venv / "lib" / "python3.11" / "site-packages"
    package = site / "lib_common"
    package.mkdir(parents=True)
    package_payload = b'VALUE = "installed"\n'
    (package / "__init__.py").write_bytes(package_payload)
    dist_info = site / "lib_common-0.1.0.dist-info"
    dist_info.mkdir()
    metadata_payload = b"Metadata-Version: 2.1\nName: lib-common\nVersion: 0.1.0\n"
    (dist_info / "METADATA").write_bytes(metadata_payload)
    wheel_payload = (
        b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    (dist_info / "WHEEL").write_bytes(wheel_payload)
    distribution = PathDistribution(dist_info)
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _name: distribution,
    )
    wheel = tmp_path / "lib_common-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("lib_common/__init__.py", package_payload)
        archive.writestr("lib_common-0.1.0.dist-info/METADATA", metadata_payload)
        archive.writestr("lib_common-0.1.0.dist-info/WHEEL", wheel_payload)
        archive.writestr("lib_common-0.1.0.dist-info/RECORD", "")

    assert _verify_installed_wheel("lib_common", wheel, venv) is distribution

    (package / "__init__.py").write_text("TAMPERED = True\n")
    with pytest.raises(RuntimeError, match="installed payload differs"):
        _verify_installed_wheel("lib_common", wheel, venv)


def test_installed_vmdev_payload_binds_runner_source_and_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "tools" / "dev_cli" / "dev_cli"
    venv = tmp_path / "strategy-validation"
    site = venv / "lib" / "python3.11" / "site-packages"
    installed_root = site / "dev_cli"
    files = {
        "__init__.py": b"",
        "main.py": b"def cli(): pass\n",
        "commands/strategy.py": b"VALUE = 'command'\n",
        "validation/execution_environment.py": b"VALUE = 'runner'\n",
    }
    for relative, payload in files.items():
        source = source_root / relative
        installed = installed_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        installed.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
        installed.write_bytes(payload)

    dist_info = site / "vmdev-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vmdev\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[console_scripts]\nvmdev = dev_cli.main:cli\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _name: PathDistribution(dist_info),
    )
    monkeypatch.setattr(
        execution_environment,
        "__file__",
        str(installed_root / "validation" / "execution_environment.py"),
    )

    payload = installed_vmdev_payload(repo_root, venv)

    assert payload["file_count"] == len(files)
    assert set(payload["files"]) == set(files)
    assert len(payload["payload_sha256"]) == 64

    (installed_root / "commands" / "strategy.py").write_text(
        "VALUE = 'tampered'\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="stale relative to source"):
        installed_vmdev_payload(repo_root, venv)
