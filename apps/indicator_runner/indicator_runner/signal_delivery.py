"""Serialized signal delivery on a thread outside the strategy processing lock."""

from __future__ import annotations

import threading
from collections.abc import Callable

from sqlalchemy.exc import SQLAlchemyError

from lib_common.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_PASSES_PER_WAKE = 200


class SignalDeliveryLoop:
    """Own every ``DurableSignalRelay.run_once`` call for one worker.

    Processing paths call :meth:`wake` after committing a bar or panel
    transition. The thread then drains ordered outbox claims until a pass
    delivers nothing, so a partition holding several undelivered signals empties
    in one wake even though ordered claiming yields one row per pass. Between
    wakes an idle pass every ``idle_interval_seconds`` picks up records whose
    retry backoff expired and any wake lost to a race. The loop never takes the
    worker's processing lock, so scoring latency or an outage cannot stall bar
    processing.
    """

    def __init__(
        self,
        *,
        run_once: Callable[[], tuple[int, int]],
        worker_id: str,
        idle_interval_seconds: float,
        max_passes_per_wake: int = _DEFAULT_MAX_PASSES_PER_WAKE,
    ) -> None:
        if idle_interval_seconds <= 0:
            msg = "idle_interval_seconds must be positive"
            raise ValueError(msg)
        if max_passes_per_wake < 1:
            msg = "max_passes_per_wake must be positive"
            raise ValueError(msg)
        self._run_once = run_once
        self._worker_id = worker_id
        self._idle_interval_seconds = float(idle_interval_seconds)
        self._max_passes_per_wake = max_passes_per_wake
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure_handler: Callable[[BaseException], None] | None = None

    @property
    def idle_interval_seconds(self) -> float:
        """Recovery pass cadence when no wake arrives."""
        return self._idle_interval_seconds

    @property
    def alive(self) -> bool:
        """True while the delivery thread is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def wake_pending(self) -> bool:
        """True when a wake is armed and not yet consumed by a drain."""
        return self._wake_event.is_set()

    def bind_failure_handler(self, handler: Callable[[BaseException], None]) -> None:
        """Report an unrecoverable pass failure to the owning worker."""
        self._failure_handler = handler

    def start(self) -> None:
        """Start the daemon thread once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"signal-delivery-{self._worker_id}",
        )
        self._thread.start()

    def wake(self) -> None:
        """Ask the thread to drain; safe from any thread, never blocks."""
        self._wake_event.set()

    def request_stop(self) -> None:
        """Ask the thread to stop after its current pass; never blocks.

        Safe from a signal handler. :meth:`stop` joins the thread afterwards.
        """
        self._stop_event.set()
        self._wake_event.set()

    @property
    def stop_requested(self) -> bool:
        """True once a stop was requested; ``drain`` ends between passes."""
        return self._stop_event.is_set()

    def stop(self, *, timeout: float) -> bool:
        """Stop the thread and wait up to ``timeout`` seconds for the current pass.

        Returns True once the thread has exited (or never ran). False means a
        pass is still in flight, typically one HTTP attempt against an
        unresponsive scoring endpoint that the emitter's own timeout bounds.
        The row that pass claimed stays leased in the outbox and is reclaimed
        after the lease expires, so the caller may dispose the engine and exit.
        """
        self.request_stop()
        if self._thread is None or not self._thread.is_alive():
            return True
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.error(
                "Signal delivery loop still in flight %.1fs after stop; its claimed "
                "outbox row is reclaimed when the lease expires",
                timeout,
                worker_id=self._worker_id,
            )
            return False
        return True

    def drain(self) -> tuple[int, int]:
        """Run passes until one delivers nothing; return (delivered, failed) totals.

        A pass that only fails ends the drain so the relay's per-record backoff
        is respected. Hitting the pass cap re-arms the wake instead of spinning.
        A stop request ends the drain between passes, so shutdown waits for at
        most one pass.
        """
        delivered_total = 0
        failed_total = 0
        for _ in range(self._max_passes_per_wake):
            if self._stop_event.is_set():
                return delivered_total, failed_total
            delivered, failed = self._run_once()
            delivered_total += delivered
            failed_total += failed
            if delivered == 0:
                return delivered_total, failed_total
        self._wake_event.set()
        return delivered_total, failed_total

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(self._idle_interval_seconds)
            if self._stop_event.is_set():
                return
            # Clear before draining so a wake that lands mid-drain survives.
            self._wake_event.clear()
            try:
                self.drain()
            except (SQLAlchemyError, OSError, RuntimeError, ValueError, TypeError) as exc:
                # One failed pass ends the loop; the main loop sees the dead
                # thread through delivery_alive and exits non-zero.
                logger.exception("Signal delivery loop failed", worker_id=self._worker_id)
                if self._failure_handler is not None:
                    self._failure_handler(exc)
                return


__all__ = ["SignalDeliveryLoop"]
