"""Deployment contract tests for graceful application shutdown."""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE_PATH = _ROOT / "docker" / "docker-compose.stack.yml"
_EXPECTED_STOP_GRACE = "${APP_STOP_GRACE_PERIOD:-60s}"
_SUPERVISED_APPLICATION_SERVICES = {
    "scoring-engine",
    "scoring-outbox-relay",
    "execution-engine",
    "feedback-loop-engine",
    "market-data-ingestor",
    "fx-rate-ingestor",
    "market-calendar-ibkr",
    "market-calendar-saxo",
    "market-calendar-zerodha",
    "indicator-runner",
    "backend",
}


def test_supervised_application_services_have_explicit_stop_grace() -> None:
    """Docker must not apply its shorter implicit stop timeout to app cleanup."""
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose["services"]

    assert services.keys() >= _SUPERVISED_APPLICATION_SERVICES
    assert {
        name: services[name].get("stop_grace_period")
        for name in sorted(_SUPERVISED_APPLICATION_SERVICES)
    } == dict.fromkeys(
        sorted(_SUPERVISED_APPLICATION_SERVICES),
        _EXPECTED_STOP_GRACE,
    )
