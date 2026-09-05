"""Relay notification flag and worker delivery-loop interval configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lib_common.config_validation import (
    IndicatorWorkerConfig,
    ScoringRelayConfig,
    load_indicator_worker_config,
)


def test_relay_config_defaults_cover_only_execution_topics_and_enable_notify() -> None:
    config = ScoringRelayConfig()
    assert config.topics == ("execution.commands", "execution.rebalance.commands")
    assert config.notify_enabled is True
    assert not hasattr(config, "publish_topics")


def test_worker_idle_interval_is_bounded() -> None:
    default = IndicatorWorkerConfig(database_url="postgresql://vm_indicator_login:x@db/vm")
    assert default.signal_relay_idle_interval_seconds == 5
    for bad in (0, 61):
        with pytest.raises(ValidationError):
            IndicatorWorkerConfig(
                database_url="postgresql://vm_indicator_login:x@db/vm",
                signal_relay_idle_interval_seconds=bad,
            )


def test_worker_idle_interval_parses_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://vm_indicator_login:secret@localhost:5432/vm")
    monkeypatch.setenv("SIGNAL_RELAY_IDLE_INTERVAL_SEC", "9")
    assert load_indicator_worker_config().signal_relay_idle_interval_seconds == 9
    monkeypatch.setenv("SIGNAL_RELAY_IDLE_INTERVAL_SEC", "0")
    with pytest.raises(ValueError, match="SIGNAL_RELAY_IDLE_INTERVAL_SEC=0 is below minimum 1"):
        load_indicator_worker_config()
