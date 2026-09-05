"""Execution-engine alert publishing.

Thin adapter over the shared ``lib_common.alerting`` multi-sink publisher: a
caller-supplied webhook plus any ``ALERT_*`` sinks (webhook / Telegram / email)
declared in the environment. Delivery is best-effort and off-thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from lib_common.alerting import (
    Alert,
    AlertSink,
    MultiSinkAlertPublisher,
    WebhookSink,
    build_sinks_from_env,
)
from lib_common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ExecutionAlert:
    event_type: str
    severity: str
    message: str
    payload: dict[str, Any]


class AlertPublisher:
    """Publish execution operational alerts to every configured sink."""

    def __init__(
        self,
        *,
        enabled: bool,
        webhook_url: str | None,
        client: Any | None = None,
        dispatch_async: bool = True,
        publisher: MultiSinkAlertPublisher | None = None,
        alert_environments: set[str] | None = None,
        sinks: Sequence[AlertSink] | None = None,
    ) -> None:
        self._alert_environments = (
            alert_environments if alert_environments is not None else {"live"}
        )
        if publisher is not None:
            if sinks is not None:
                msg = "AlertPublisher accepts either publisher or sinks, not both"
                raise ValueError(msg)
            self._publisher = publisher
            return
        extra_sinks = [WebhookSink(webhook_url, client=client)] if webhook_url else []
        configured_sinks = build_sinks_from_env() if sinks is None else list(sinks)
        self._publisher = MultiSinkAlertPublisher(
            [*extra_sinks, *configured_sinks],
            enabled=enabled,
            dispatch_async=dispatch_async,
        )

    @property
    def enabled(self) -> bool:
        return self._publisher.enabled

    def publish(self, alert: ExecutionAlert) -> None:
        # Suppress alerts tagged with a non-paging run-environment (default: only
        # "live" pages). Paper drift/breaker churn is expected test-noise and would
        # otherwise bury real live-money alerts. Alerts with no environment tag
        # always page. The finding is still logged here for visibility.
        env = alert.payload.get("environment")
        if env is not None and str(env).strip().lower() not in self._alert_environments:
            logger.info(
                "Alert not paged (non-paging environment)",
                event_type=alert.event_type,
                severity=alert.severity,
                environment=str(env),
            )
            return
        self._publisher.publish(
            Alert(
                event_type=alert.event_type,
                severity=alert.severity,
                message=alert.message,
                payload=alert.payload,
                source="execution_engine",
            )
        )

    def close(self) -> None:
        self._publisher.close()
