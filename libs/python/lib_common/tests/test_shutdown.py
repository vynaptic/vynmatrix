"""Tests for bounded, deterministic graceful shutdown coordination."""

from __future__ import annotations

import asyncio
import gc
import time
import warnings
from collections.abc import Awaitable
from datetime import datetime

import pytest

from lib_common.shutdown import GracefulShutdown


def test_execute_initializes_real_shutdown_timing_without_signal_registration() -> None:
    async def exercise() -> tuple[dict[str, object], dict[str, object]]:
        shutdown = GracefulShutdown()
        before = shutdown.get_status()
        await shutdown.execute()
        return before, shutdown.get_status()

    before, after = asyncio.run(exercise())

    assert before["requested"] is False
    assert before["started_at"] is None
    assert after["requested"] is True
    assert after["started_at"] is not None
    assert after["completed_at"] is not None
    assert datetime.fromisoformat(str(after["completed_at"])) >= datetime.fromisoformat(
        str(after["started_at"])
    )


def test_async_handler_may_exceed_five_seconds_within_force_budget() -> None:
    async def exercise() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        shutdown = GracefulShutdown(
            timeout_seconds=0.1,
            force_timeout_seconds=6,
        )

        async def deliberate_drain() -> None:
            trace.append("started")
            await asyncio.sleep(5.05)
            trace.append("completed")

        shutdown.on_shutdown(deliberate_drain)
        await shutdown.execute()
        return trace, shutdown.get_status()["errors"]

    trace, errors = asyncio.run(exercise())

    assert trace == ["started", "completed"]
    assert errors == []


def test_force_timeout_bounds_pending_drain_and_async_handlers() -> None:
    async def exercise() -> tuple[float, list[str], list[str]]:
        trace: list[str] = []
        shutdown = GracefulShutdown(
            timeout_seconds=0.04,
            force_timeout_seconds=0.08,
        )

        async def later_handler() -> None:
            trace.append("later")

        async def blocked_handler() -> None:
            trace.append("blocked_started")
            try:
                await asyncio.Event().wait()
            finally:
                trace.append("blocked_cancelled")

        shutdown.on_shutdown(later_handler)
        shutdown.on_shutdown(blocked_handler)

        started = time.monotonic()
        with shutdown.track_operation("in-flight-order"):
            await shutdown.execute()
        return time.monotonic() - started, trace, shutdown.get_status()["errors"]

    elapsed, trace, errors = asyncio.run(exercise())

    assert elapsed < 0.3
    assert trace == ["blocked_started", "blocked_cancelled"]
    assert errors == [
        "blocked_handler: timed out at the shutdown force deadline",
        "later_handler: skipped because the shutdown force timeout was exhausted",
    ]


def test_pending_drain_timeout_does_not_consume_available_handler_budget() -> None:
    async def exercise() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        shutdown = GracefulShutdown(
            timeout_seconds=0.01,
            force_timeout_seconds=0.2,
        )

        async def cleanup_handler() -> None:
            trace.append("cleanup")

        shutdown.on_shutdown(cleanup_handler)
        with shutdown.track_operation("still-draining"):
            await shutdown.execute()
        return trace, shutdown.get_status()["errors"]

    trace, errors = asyncio.run(exercise())

    assert trace == ["cleanup"]
    assert errors == []


def test_self_cancelled_handler_is_recorded_and_later_handler_runs() -> None:
    async def exercise() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        shutdown = GracefulShutdown(
            timeout_seconds=0.1,
            force_timeout_seconds=0.5,
        )

        async def later_handler() -> None:
            trace.append("later")

        async def cancelled_handler() -> None:
            trace.append("cancelled")
            raise asyncio.CancelledError

        shutdown.on_shutdown(later_handler)
        shutdown.on_shutdown(cancelled_handler)
        await shutdown.execute()
        return trace, shutdown.get_status()["errors"]

    trace, errors = asyncio.run(exercise())

    assert trace == ["cancelled", "later"]
    assert errors == ["cancelled_handler: cancelled"]


def test_cancelling_execute_cancels_active_handler_without_running_later_handlers() -> None:
    async def exercise() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        handler_started = asyncio.Event()
        shutdown = GracefulShutdown(
            timeout_seconds=0.1,
            force_timeout_seconds=1,
        )

        async def later_handler() -> None:
            trace.append("later")

        async def active_handler() -> None:
            trace.append("active_started")
            handler_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                trace.append("active_cancelled")

        shutdown.on_shutdown(later_handler)
        shutdown.on_shutdown(active_handler)
        execute_task = asyncio.create_task(shutdown.execute())
        await handler_started.wait()
        execute_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execute_task
        return trace, shutdown.get_status()["errors"]

    trace, errors = asyncio.run(exercise())

    assert trace == ["active_started", "active_cancelled"]
    assert errors == ["active_handler: cancelled"]


def test_coroutine_returned_after_deadline_is_closed_without_warning() -> None:
    async def exercise() -> list[str]:
        shutdown = GracefulShutdown(
            timeout_seconds=0.01,
            force_timeout_seconds=0.01,
        )

        async def never_started() -> None:
            await asyncio.sleep(1)

        def slow_factory() -> Awaitable[None]:
            time.sleep(0.02)
            return never_started()

        shutdown.on_shutdown(slow_factory)
        await shutdown.execute()
        return shutdown.get_status()["errors"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        errors = asyncio.run(exercise())
        gc.collect()

    assert errors == ["slow_factory: timed out at the shutdown force deadline"]
    assert not any("was never awaited" in str(item.message) for item in caught)


def test_sync_handler_overrun_and_later_skip_are_visible_in_state() -> None:
    async def exercise() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        shutdown = GracefulShutdown(
            timeout_seconds=0.01,
            force_timeout_seconds=0.01,
        )

        def later_handler() -> None:
            trace.append("later")

        def slow_sync_handler() -> None:
            trace.append("slow")
            time.sleep(0.02)

        shutdown.on_shutdown(later_handler)
        shutdown.on_shutdown(slow_sync_handler)
        await shutdown.execute()
        return trace, shutdown.get_status()["errors"]

    trace, errors = asyncio.run(exercise())

    assert trace == ["slow"]
    assert errors == [
        "slow_sync_handler: exceeded the shutdown force timeout",
        "later_handler: skipped because the shutdown force timeout was exhausted",
    ]
