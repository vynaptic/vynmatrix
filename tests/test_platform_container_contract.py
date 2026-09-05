"""Container budget and environment boundaries for the single-owner runtime."""

import re
from pathlib import Path

import pytest
import yaml

from scripts.platform_processes import build_processes

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker/docker-compose.stack.yml").read_text())


def test_normal_runtime_has_only_three_possible_container_identities() -> None:
    services = _compose()["services"]
    assert set(services) == {"postgres", "application", "workers", "bootstrap"}
    assert services["workers"]["profiles"] == ["workers"]
    assert services["bootstrap"]["profiles"] == ["maintenance"]
    assert "profiles" not in services["application"]
    for name in ("application", "workers"):
        service = services[name]
        assert "bootstrap" not in service.get("depends_on", {})
        assert service["init"] is True
        assert service["stop_grace_period"] == "60s"
        assert service["healthcheck"]["test"][-1].endswith("/health')")


def test_legacy_database_entrypoints_cannot_bypass_the_supported_lifecycle() -> None:
    retired = (
        "docker/docker-compose.yml",
        "scripts/db/manage_db.sh",
        "scripts/db/manage_db.ps1",
        "scripts/db/migrate_and_seed.sh",
    )
    assert not [path for path in retired if (ROOT / path).exists()]


def test_runtime_publishes_only_loopback_backend_and_database() -> None:
    services = _compose()["services"]
    for name, service in services.items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
            assert port.endswith(":8081") if name == "application" else port.endswith(":5432")
    assert "ports" not in services["workers"]


def test_privileged_bootstrap_never_inherits_runtime_start_dependencies() -> None:
    services = _compose()["services"]
    bootstrap = services["bootstrap"]
    assert bootstrap["restart"] == "no"
    assert bootstrap["command"] == [
        "python",
        "-m",
        "dev_cli.main",
        "db",
        "bootstrap",
        "--inside-container",
    ]
    assert set(bootstrap["depends_on"]) == {"postgres"}
    assert "ADMIN_DATABASE_URL" in bootstrap["environment"]
    assert "MIGRATION_DATABASE_URL" in bootstrap["environment"]
    assert "DATABASE_URL" not in bootstrap["environment"]
    for name in ("application", "workers"):
        env = services[name]["environment"]
        assert "DATABASE_URL" not in env
        assert "DB_PASSWORD" not in env
        assert "POSTGRES_PASSWORD" not in env
        assert "VM_BACKEND_DB_PASSWORD" not in env
        assert env["EXECUTION_MODE"] == "paper"
        assert env["EXECUTION_ENGINE_ALLOW_LIVE"] == "false"


def test_declared_image_inventory_has_one_runtime_artifact_and_no_orphan_dockerfiles() -> None:
    inventory = yaml.safe_load((ROOT / "config/containers.yaml").read_text())
    assert len(inventory["services"]) == 1
    entry = inventory["services"][0]
    assert entry["image"] == "vynmatrix/platform"
    assert entry["dockerfile"] == "docker/platform_runtime.Dockerfile"
    allowed = {ROOT / "docker/svc-base.Dockerfile", ROOT / entry["dockerfile"]}
    assert set((ROOT / "docker").glob("*.Dockerfile")) == allowed
    services = _compose()["services"]
    assert (
        services["application"]["image"]
        == services["workers"]["image"]
        == services["bootstrap"]["image"]
    )


def _resolved_runtime(service: dict, *, group: str) -> dict[str, str]:
    values = {
        f"{role}_DATABASE_URL": f"postgresql://vm_{role.lower()}_login:fixture@postgres/db"
        for role in ("BACKEND", "SCORING", "EXECUTION", "FEEDBACK", "MARKET_DATA", "INDICATOR")
    }
    values.update(
        {
            "BACKEND_ADMIN_API_KEY": "backend-key",
            "SCORING_API_KEY": "scoring-key",
            "EXECUTION_API_KEY": "execution-key",
            "SCORING_ADMIN_API_KEY": "scoring-admin-key",
            "EXECUTION_ADMIN_API_KEY": "execution-admin-key",
            "FEEDBACK_API_KEY": "feedback-key",
            "MARKET_DATA_API_KEY": "market-data-key",
            "SECRETS_MASTER_KEYS": "fixture-key-ring",
            "PLATFORM_APPLICATION_GROUP": group,
            "PLATFORM_WORKERS": "fx",
        }
    )
    pattern = re.compile(r"\$\{([A-Z_]+)(?::-([^}]*))?\}")
    return {
        key: pattern.sub(lambda match: values.get(match[1], match[2] or ""), value)
        for key, value in service["environment"].items()
    }


def test_compose_environments_start_split_and_combined_process_groups() -> None:
    services = _compose()["services"]
    split = build_processes(
        "application", _resolved_runtime(services["application"], group="application")
    )
    workers = build_processes(
        "workers", _resolved_runtime(services["workers"], group="application")
    )
    combined = build_processes("all", _resolved_runtime(services["application"], group="all"))
    assert {spec.name for spec in split + workers} == {spec.name for spec in combined}
    assert {spec.name for spec in workers} == {"feedback", "fx"}
    with pytest.raises(ValueError, match="PLATFORM_APPLICATION_GROUP"):
        build_processes("workers", _resolved_runtime(services["workers"], group="all"))


def test_default_compose_feedback_starts_with_dynamic_progress_deadline(monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from feedback_loop_engine.api import create_app

    env = _resolved_runtime(_compose()["services"]["workers"], group="application")
    feedback = next(spec for spec in build_processes("workers", env) if spec.name == "feedback")
    monkeypatch.delenv("FEEDBACK_HEARTBEAT_MAX_AGE_SECONDS", raising=False)
    for key, value in feedback.environment.items():
        monkeypatch.setenv(key, value)
    app = create_app(SimpleNamespace(_engine=None), SimpleNamespace())
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/ready").status_code == 503


def test_primary_market_cadence_and_freshness_are_independent_of_equity(monkeypatch) -> None:
    from market_data_ingestor.main import _get_candle_poll_interval, _get_staleness_threshold

    env = _resolved_runtime(_compose()["services"]["workers"], group="application")
    env.update(
        {
            "PLATFORM_WORKERS": "market-data,equity",
            "INGESTOR_SYMBOLS": "BTC-USDC",
            "EQUITY_INGESTOR_SYMBOLS": "AAPL.US",
        }
    )
    specs = {spec.name: spec for spec in build_processes("workers", env)}
    for name, cadence, freshness in (("market-data", 60, None), ("equity", 3600, 350000)):
        with monkeypatch.context() as context:
            context.delenv("INGESTOR_CANDLE_POLL_INTERVAL_SEC", raising=False)
            context.delenv("INGESTOR_STALENESS_THRESHOLD_SEC", raising=False)
            for key, value in specs[name].environment.items():
                context.setenv(key, value)
            assert _get_candle_poll_interval() == cadence
            assert _get_staleness_threshold() == freshness


def test_incompatible_group_reports_configuration_field_without_other_credentials() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_platform", "workers"],
        cwd=ROOT,
        env={"PLATFORM_APPLICATION_GROUP": "all", "UNRELATED_SECRET": "must-stay-private"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "PLATFORM_APPLICATION_GROUP" in output
    assert "must-stay-private" not in output
