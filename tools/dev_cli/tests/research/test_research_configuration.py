"""Research credentials and the declared sandbox must retain their boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from dev_cli.core.deployment import configuration, environment
from dev_cli.core.runtime_roles import RUNTIME_ROLES


def test_research_has_distinct_credentials_and_host_resolution():
    values = configuration.build_values(
        configuration.OwnerInputs(
            email="owner@example.test", base_ccy="EUR", tz="UTC", paper_equity=Decimal("1000")
        ),
        host="postgres",
        port=5432,
    )
    url = values.get("RESEARCH_DATABASE_URL")
    assert url, "research supervisor needs its own least-privilege login"
    parsed = make_url(url)
    assert parsed.username == "vm_research_login"
    assert parsed.password == values["VM_RESEARCH_DB_PASSWORD"]
    assert parsed.password != make_url(values["BACKEND_DATABASE_URL"]).password
    resolved = environment.resolve_host_env({**values, "DB_PORT": "55432"})
    assert make_url(resolved["RESEARCH_DATABASE_URL"]).host == "127.0.0.1"
    assert make_url(resolved["RESEARCH_DATABASE_URL"]).port == 55432
    assert ("vm_research_login", "vm_research") in RUNTIME_ROLES


def test_declared_sandbox_renders_without_runtime_secrets_or_host_mounts():
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI unavailable; runtime isolation gate remains required")
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(root / ".env.example"),
            "-f",
            str(root / "docker/docker-compose.stack.yml"),
            "--profile",
            "research",
            "config",
            "--format",
            "json",
        ],
        env={"PATH": os.environ["PATH"], "VM_DEPLOY_IMAGE_TAG": "sha-123456789abc"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sandbox = json.loads(result.stdout)["services"].get("research")
    assert sandbox is not None, "research must use a declared service"
    assert sandbox["network_mode"] == "none"
    assert sandbox["read_only"] is True
    assert sandbox.get("volumes", []) == []
    assert sandbox.get("ports", []) == []
    assert sandbox["cap_drop"] == ["ALL"]
    assert sandbox["pids_limit"] == 32
    assert not any(
        "DATABASE_URL" in name or "KEY" in name or "PASSWORD" in name
        for name in sandbox["environment"]
    )
    assert sandbox["environment"]["EXECUTION_ENGINE_ALLOW_LIVE"] == "false"
    assert sandbox["image"] == "vynmatrix/platform:sha-123456789abc"


def test_research_worker_is_available_through_the_repository_cli():
    from click.testing import CliRunner

    from dev_cli.main import cli

    result = CliRunner().invoke(cli, ["research", "worker", "--help"])
    assert result.exit_code == 0, result.output
    assert "--once" in result.output
