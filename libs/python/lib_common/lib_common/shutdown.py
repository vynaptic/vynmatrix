"""
Graceful shutdown handling for production services.

Provides signal handlers and shutdown coordination for:
- SIGTERM (container orchestration)
- SIGINT (manual interruption)
- Completing in-flight requests
- Clean broker disconnection
- Resource cleanup
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lib_common.logging import get_logger

logger = get_logger(__name__)


def _handler_name(handler: Callable[[], Awaitable[None] | None]) -> str:
    """Return a stable diagnostic name for functions and callable objects."""
    name = getattr(handler, "__name__", None)
    return str(name) if name else type(handler).__name__


def _current_task_is_cancelling() -> bool:
    """Distinguish caller cancellation from a handler cancelling itself."""
    task = asyncio.current_task()
    return bool(task is not None and task.cancelling())


async def _cancel_and_consume(task: asyncio.Future[Any]) -> None:
    """Cancel an async handler task and retrieve its terminal outcome."""
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@dataclass
class ShutdownState:
    """Tracks shutdown state and pending operations."""

    requested: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pending_tasks: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


class GracefulShutdown:
    """
    Coordinator for graceful service shutdown.

    Handles:
    - Signal registration (SIGTERM, SIGINT)
    - Tracking in-flight operations
    - Coordinated shutdown sequence
    - Timeout enforcement

    Usage:
        shutdown = GracefulShutdown(timeout_seconds=30)
        shutdown.register_signals()

        # Register cleanup handlers
        shutdown.on_shutdown(cleanup_broker_connections)
        shutdown.on_shutdown(flush_pending_orders)

        # Track in-flight work
        async with shutdown.track_operation("process_signal"):
            await process_signal(signal)

        # Check if shutdown requested
        if shutdown.is_requested:
            return

        # In main loop
        while not shutdown.is_requested:
            await process_next()

        # Wait for completion
        await shutdown.wait_for_shutdown()
    """

    def __init__(
        self,
        timeout_seconds: float = 30,
        force_timeout_seconds: float = 45,
    ) -> None:
        """
        Initialize shutdown coordinator.

        Args:
            timeout_seconds: Graceful shutdown timeout
            force_timeout_seconds: Total pending-drain and cleanup budget
        """
        self._timeout = timeout_seconds
        self._force_timeout = force_timeout_seconds
        self._state = ShutdownState()
        self._handlers: list[Callable[[], Awaitable[None] | None]] = []
        self._lock = threading.Lock()
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._state.requested

    @property
    def pending_count(self) -> int:
        """Get count of pending operations."""
        return len(self._state.pending_tasks)

    def register_signals(self) -> None:
        """Register signal handlers for SIGTERM and SIGINT."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Register handlers
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                if self._loop:
                    self._loop.add_signal_handler(sig, self._handle_signal, sig)
                else:
                    signal.signal(sig, self._sync_handle_signal)
            except (ValueError, OSError) as e:
                # Signal handling may not be available on all platforms/threads
                logger.warning("Could not register signal handler for %s: %s", sig, e)

        logger.info(
            "Graceful shutdown handlers registered",
            extra={"timeout": self._timeout, "force_timeout": self._force_timeout},
        )

    def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal (async context)."""
        sig_name = sig.name if hasattr(sig, "name") else str(sig)
        logger.info("Received signal %s, initiating graceful shutdown", sig_name)
        self._initiate_shutdown()

    def _sync_handle_signal(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        """Handle shutdown signal (sync context)."""
        sig_name = signal.Signals(signum).name
        logger.info("Received signal %s, initiating graceful shutdown", sig_name)
        self._initiate_shutdown()

    def _initiate_shutdown(self) -> None:
        """Initiate the shutdown sequence."""
        with self._lock:
            if self._state.requested:
                logger.warning("Shutdown already in progress, forcing exit")
                sys.exit(1)

            self._state.requested = True
            self._state.started_at = datetime.now(tz=UTC)

        self._shutdown_event.set()
        logger.info(
            "Shutdown initiated",
            extra={"pending_tasks": len(self._state.pending_tasks)},
        )

    def on_shutdown(self, handler: Callable[[], Awaitable[None] | None]) -> None:
        """
        Register a shutdown handler.

        Handlers are called in reverse order of registration.

        Args:
            handler: Async or sync cleanup function
        """
        self._handlers.append(handler)

    @contextmanager
    def track_operation(self, operation_id: str) -> Iterator[None]:
        """
        Context manager to track an in-flight operation.

        Args:
            operation_id: Unique identifier for the operation

        Yields:
            None

        Example:
            with shutdown.track_operation(f"order_{order_id}"):
                await process_order(order)
        """
        with self._lock:
            self._state.pending_tasks.add(operation_id)

        try:
            yield
        finally:
            with self._lock:
                self._state.pending_tasks.discard(operation_id)

    async def track_async_operation(self, operation_id: str, coro: Awaitable[Any]) -> Any:
        """
        Track an async operation.

        Args:
            operation_id: Unique identifier for the operation
            coro: Coroutine to execute

        Returns:
            Result of the coroutine
        """
        with self.track_operation(operation_id):
            return await coro

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal and execute cleanup."""
        await self._shutdown_event.wait()
        await self.execute()

    async def execute(self) -> None:
        """Execute registered shutdown handlers within one force deadline.

        ``timeout_seconds`` remains the budget for draining tracked operations.
        ``force_timeout_seconds`` bounds that drain plus every cleanup handler.
        Async handlers receive the remaining total budget in registration-reverse
        order; once it is exhausted, later handlers are skipped and recorded
        deterministically instead of being started without time to finish.
        """
        self._ensure_shutdown_started()
        logger.info(
            "Executing shutdown sequence",
            extra={"handlers": len(self._handlers)},
        )

        loop = asyncio.get_running_loop()
        force_deadline = loop.time() + self._force_timeout
        await self._drain_pending_operations(
            loop=loop,
            force_deadline=force_deadline,
        )
        await self._execute_handlers(
            loop=loop,
            force_deadline=force_deadline,
        )

        self._state.completed_at = datetime.now(tz=UTC)

        duration = (
            (self._state.completed_at - self._state.started_at).total_seconds()
            if self._state.started_at
            else 0
        )

        logger.info(
            "Shutdown complete",
            extra={
                "duration_seconds": round(duration, 2),
                "errors": len(self._state.errors),
            },
        )

    def _ensure_shutdown_started(self) -> None:
        """Initialize lifecycle timing when an owning server handles signals."""
        with self._lock:
            self._state.requested = True
            if self._state.started_at is None:
                self._state.started_at = datetime.now(tz=UTC)
        self._shutdown_event.set()

    async def _drain_pending_operations(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        force_deadline: float,
    ) -> None:
        """Drain tracked work within both the drain and total force budgets."""
        if not self._state.pending_tasks:
            return
        pending_budget = min(
            float(self._timeout),
            max(0.0, force_deadline - loop.time()),
        )
        try:
            await asyncio.wait_for(
                self._wait_for_pending(),
                timeout=pending_budget,
            )
        except TimeoutError:
            logger.warning(
                "Timeout waiting for pending operations",
                extra={"pending": list(self._state.pending_tasks)},
            )

    async def _execute_handlers(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        force_deadline: float,
    ) -> None:
        """Run handlers in reverse registration order until the total deadline."""
        handlers = list(reversed(self._handlers))
        for index, handler in enumerate(handlers):
            if loop.time() >= force_deadline:
                self._record_skipped_handlers(handlers[index:])
                return
            await self._execute_handler(
                handler,
                loop=loop,
                force_deadline=force_deadline,
            )

    async def _execute_handler(
        self,
        handler: Callable[[], Awaitable[None] | None],
        *,
        loop: asyncio.AbstractEventLoop,
        force_deadline: float,
    ) -> None:
        """Invoke one handler and account for every terminal outcome."""
        handler_name = _handler_name(handler)
        try:
            result = handler()
        except asyncio.CancelledError:
            self._record_cancelled_handler(handler_name)
            if _current_task_is_cancelling():
                raise
            return
        except Exception as exc:
            logger.exception("Shutdown handler failed: %s", handler_name)
            self._state.errors.append(f"{handler_name}: {exc}")
            return

        if result is None:
            if loop.time() >= force_deadline:
                self._record_handler_error(f"{handler_name}: exceeded the shutdown force timeout")
            return
        await self._await_handler(
            result,
            handler_name=handler_name,
            loop=loop,
            force_deadline=force_deadline,
        )

    async def _await_handler(
        self,
        result: Awaitable[None],
        *,
        handler_name: str,
        loop: asyncio.AbstractEventLoop,
        force_deadline: float,
    ) -> None:
        """Await one cleanup result for no longer than the remaining budget."""
        try:
            task = asyncio.ensure_future(result)
        except (TypeError, ValueError) as exc:
            logger.exception("Shutdown handler returned an invalid awaitable: %s", handler_name)
            self._state.errors.append(f"{handler_name}: {exc}")
            return
        remaining = force_deadline - loop.time()
        if remaining <= 0:
            await _cancel_and_consume(task)
            self._record_handler_error(f"{handler_name}: timed out at the shutdown force deadline")
            return

        try:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            if not done:
                await _cancel_and_consume(task)
                self._record_handler_error(
                    f"{handler_name}: timed out at the shutdown force deadline"
                )
                return
            await task
        except asyncio.CancelledError:
            await _cancel_and_consume(task)
            self._record_cancelled_handler(handler_name)
            if _current_task_is_cancelling():
                raise
        except TimeoutError:
            logger.exception("Shutdown handler failed: %s", handler_name)
            self._state.errors.append(f"{handler_name}: timeout")
        except Exception as exc:
            logger.exception("Shutdown handler failed: %s", handler_name)
            self._state.errors.append(f"{handler_name}: {exc}")

    def _record_cancelled_handler(self, handler_name: str) -> None:
        self._state.errors.append(f"{handler_name}: cancelled")
        logger.warning("Shutdown handler cancelled: %s", handler_name)

    def _record_handler_error(self, error: str) -> None:
        self._state.errors.append(error)
        logger.warning(error)

    def _record_skipped_handlers(
        self,
        handlers: list[Callable[[], Awaitable[None] | None]],
    ) -> None:
        for handler in handlers:
            self._record_handler_error(
                f"{_handler_name(handler)}: skipped because the shutdown "
                "force timeout was exhausted"
            )

    async def _wait_for_pending(self) -> None:
        """Wait for all pending operations to complete."""
        # pending_tasks is mutated under a threading.Lock from arbitrary
        # threads (sync track_operation), so an asyncio.Event cannot be set
        # safely without per-loop plumbing. This 0.1s poll runs only during
        # shutdown and is bounded by the caller's wait_for budget.
        while self._state.pending_tasks:  # noqa: ASYNC110 - cross-thread drain poll
            await asyncio.sleep(0.1)

    def get_status(self) -> dict[str, Any]:
        """Get current shutdown status."""
        return {
            "requested": self._state.requested,
            "started_at": (self._state.started_at.isoformat() if self._state.started_at else None),
            "completed_at": (
                self._state.completed_at.isoformat() if self._state.completed_at else None
            ),
            "pending_tasks": list(self._state.pending_tasks),
            "pending_count": len(self._state.pending_tasks),
            "errors": self._state.errors,
        }


# Global singleton for simple usage
_shutdown_coordinator: GracefulShutdown | None = None


def get_shutdown_coordinator(
    timeout_seconds: float = 30,
) -> GracefulShutdown:
    """
    Get or create the global shutdown coordinator.

    Args:
        timeout_seconds: Shutdown timeout (only used on first call)

    Returns:
        GracefulShutdown instance
    """
    global _shutdown_coordinator  # noqa: PLW0603

    if _shutdown_coordinator is None:
        _shutdown_coordinator = GracefulShutdown(timeout_seconds=timeout_seconds)

    return _shutdown_coordinator


def request_shutdown() -> None:
    """Request graceful shutdown from anywhere in the application."""
    coordinator = get_shutdown_coordinator()
    coordinator._initiate_shutdown()


def is_shutdown_requested() -> bool:
    """Check if shutdown has been requested."""
    if _shutdown_coordinator is None:
        return False
    return _shutdown_coordinator.is_requested


# Convenience decorators
def shutdown_safe(func: Callable) -> Callable:
    """
    Decorator to skip function execution if shutdown is requested.

    Example:
        @shutdown_safe
        async def process_signal(signal):
            # This won't execute if shutdown requested
            ...
    """
    if asyncio.iscoroutinefunction(func):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if is_shutdown_requested():
                logger.debug("Skipping %s due to shutdown", func.__name__)
                return None
            return await func(*args, **kwargs)

        return async_wrapper

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if is_shutdown_requested():
            logger.debug("Skipping %s due to shutdown", func.__name__)
            return None
        return func(*args, **kwargs)

    return sync_wrapper
