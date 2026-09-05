"""Contract tests for the shared retry helpers."""

from __future__ import annotations

import asyncio

import pytest

from lib_common.retries import retry_async


def test_retry_async_honors_configured_exponential_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        return "ok"

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    result = asyncio.run(
        retry_async(
            operation,
            retries=2,
            base_delay=0.25,
            max_delay=10,
            exponential_base=3,
            jitter=0,
            retry_on=(ConnectionError,),
        )
    )

    assert result == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.75]
