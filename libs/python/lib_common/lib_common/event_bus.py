"""Event publisher abstractions for internal platform events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from lib_common.logging import get_logger

logger = get_logger(__name__)


class EventPublisher(ABC):
    """Abstract event publisher."""

    @abstractmethod
    def publish(
        self,
        *,
        topic: str,
        event_key: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish an event payload and return delivery metadata."""


class NoOpEventPublisher(EventPublisher):
    """Publisher that records nothing and performs no network I/O."""

    def publish(
        self,
        *,
        topic: str,
        event_key: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        logger.debug(
            "No-op publish",
            topic=topic,
            event_key=event_key,
            payload_keys=sorted(payload.keys()),
        )
        return {"publisher": "noop", "topic": topic, "event_key": event_key}


__all__ = ["EventPublisher", "NoOpEventPublisher"]
