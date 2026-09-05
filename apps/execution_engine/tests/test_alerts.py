"""AlertPublisher environment-scoped paging (paper is log-only by default)."""

from __future__ import annotations

from typing import Any

import pytest

from execution_engine.alerts import AlertPublisher, ExecutionAlert


class _FakePublisher:
    """Captures what would be delivered to sinks."""

    def __init__(self) -> None:
        self.published: list[Any] = []
        self.enabled = True

    def publish(self, alert: Any) -> None:
        self.published.append(alert)

    def close(self) -> None:
        pass


def _alert(*, env: str | None, event_type: str = "reconciliation_position_drift") -> ExecutionAlert:
    payload: dict[str, Any] = {"symbol": "BTCUSD"}
    if env is not None:
        payload["environment"] = env
    return ExecutionAlert(
        event_type=event_type, severity="warning", message="drift", payload=payload
    )


def test_paper_alert_not_paged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXECUTION_ALERT_ENVIRONMENTS", raising=False)  # default -> {"live"}
    fake = _FakePublisher()
    pub = AlertPublisher(enabled=True, webhook_url=None, publisher=fake)

    pub.publish(_alert(env="paper"))

    assert fake.published == []  # suppressed (log-only), not delivered to sinks


def test_live_alert_is_paged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXECUTION_ALERT_ENVIRONMENTS", raising=False)
    fake = _FakePublisher()
    pub = AlertPublisher(enabled=True, webhook_url=None, publisher=fake)

    pub.publish(_alert(env="live"))

    assert len(fake.published) == 1
    assert fake.published[0].event_type == "reconciliation_position_drift"


def test_alert_without_environment_always_paged() -> None:
    fake = _FakePublisher()
    pub = AlertPublisher(
        enabled=True, webhook_url=None, publisher=fake, alert_environments={"live"}
    )

    pub.publish(_alert(env=None, event_type="alert_sink_readiness"))

    assert len(fake.published) == 1  # no environment tag -> always pages


def test_paper_paging_opt_in_via_config() -> None:
    fake = _FakePublisher()
    pub = AlertPublisher(
        enabled=True, webhook_url=None, publisher=fake, alert_environments={"live", "paper"}
    )

    pub.publish(_alert(env="paper"))

    assert len(fake.published) == 1  # opted in -> paper pages
