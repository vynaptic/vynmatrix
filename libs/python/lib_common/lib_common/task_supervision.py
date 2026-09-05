"""Canonical supervision for long-running asyncio background tasks.

A background task that dies silently leaves ``/health`` green while its work
(outbox delivery, order lifecycle) halts. Every supervised task gets the same
contract: a liveness gauge flipped on attach and off on completion, and a
done-callback that triages cancelled / crashed / unexpected-exit terminations
into the canonical log + optional failure hook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from lib_common.logging import get_logger

logger = get_logger(__name__)


def supervise_background_task(
    task: asyncio.Task[Any],
    *,
    name: str,
    up_gauge: Any | None = None,
    is_expected_stop: Callable[[], bool] | None = None,
    cancelled_is_error: bool = False,
    on_failure: Callable[[str], None] | None = None,
) -> None:
    """Attach the canonical liveness done-callback to ``task``.

    Args:
        task: The just-created background task.
        name: Human-readable task name used in every log line.
        up_gauge: Optional Prometheus gauge set to 1 now and 0 on completion.
        is_expected_stop: Optional predicate consulted first — when it returns
            True the termination is an orderly shutdown (logged at info).
        cancelled_is_error: Treat cancellation as a failure (for tasks that are
            never cancelled by design) instead of an orderly shutdown.
        on_failure: Optional hook invoked with the error description for every
            failure-classified termination (e.g. mark engine health unhealthy).
    """
    if up_gauge is not None:
        up_gauge.set(1)

    def _on_done(done: asyncio.Task[Any]) -> None:
        if up_gauge is not None:
            up_gauge.set(0)
        if is_expected_stop is not None and is_expected_stop():
            logger.info("%s stopped during shutdown", name)
            return
        if done.cancelled():
            if not cancelled_is_error:
                logger.info("%s cancelled (shutdown)", name)
                return
            error = f"{name} was cancelled unexpectedly"
            logger.error(error)
        else:
            exc = done.exception()
            if exc is not None:
                error = f"{name} died: {type(exc).__name__}: {exc}"
                logger.error(error, exc_info=exc)
            else:
                error = f"{name} exited unexpectedly"
                logger.error(error)
        if on_failure is not None:
            on_failure(error)

    task.add_done_callback(_on_done)
