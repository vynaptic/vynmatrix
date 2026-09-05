"""Retry helpers with exponential backoff.

This module provides sync and async retry utilities with exponential backoff.

Consolidated from:
- libs/python/lib_common/lib_common/retries.py (original async)
- libs/python/lib_infrastructure/lib_infrastructure/brokers/base.py (custom loop)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from lib_common.logging import get_logger

# ``logging`` import is retained because ``logging.Logger`` is used as a type
# annotation on the ``log`` parameter of :func:`retry_async` / :func:`retry_sync`.
logger = get_logger(__name__)
_T = TypeVar("_T")


async def retry_async(
    operation: Callable[[], Awaitable[_T]],
    *,
    retries: int = 2,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
    log: logging.Logger | None = None,
) -> _T:
    """
    Run an async operation with exponential backoff.

    Args:
        operation: Async callable to execute.
        retries: Number of retries after the first attempt.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exponential_base: Multiplier applied for each subsequent retry.
        jitter: Jitter factor applied to delay (0 disables jitter).
        retry_on: Exception types to retry on.
        operation_name: Name for logging context.
        log: Optional logger override.
    """
    log = log or logger
    last_err: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await operation()
        except retry_on as err:
            last_err = err
            if attempt >= retries:
                break
            delay = min(base_delay * (exponential_base**attempt), max_delay)
            if jitter:
                delay *= 1 + random.uniform(-jitter, jitter)
            log.warning(
                "Retrying %s after error (%s/%s): %s",
                operation_name,
                attempt + 1,
                retries,
                err,
            )
            await asyncio.sleep(delay)
    if last_err:
        raise last_err
    msg = f"Retry failed for {operation_name} with no captured error"
    raise RuntimeError(msg)


def retry_sync(
    operation: Callable[[], _T],
    *,
    retries: int = 2,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
    log: logging.Logger | None = None,
) -> _T:
    """
    Run a synchronous operation with exponential backoff.

    Args:
        operation: Callable to execute.
        retries: Number of retries after the first attempt.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between retries.
        exponential_base: Multiplier applied for each subsequent retry.
        jitter: Jitter factor applied to delay (0 disables jitter).
        retry_on: Exception types to retry on.
        operation_name: Name for logging context.
        log: Optional logger override.

    Returns:
        Result from the operation.

    Raises:
        Last exception encountered if all retries fail.
    """
    log = log or logger
    last_err: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return operation()
        except retry_on as err:
            last_err = err
            if attempt >= retries:
                break
            delay = min(base_delay * (exponential_base**attempt), max_delay)
            if jitter:
                delay *= 1 + random.uniform(-jitter, jitter)
            log.warning(
                "Retrying %s after error (%s/%s): %s",
                operation_name,
                attempt + 1,
                retries,
                err,
            )
            time.sleep(delay)
    if last_err:
        raise last_err
    msg = f"Retry failed for {operation_name} with no captured error"
    raise RuntimeError(msg)
