"""SignalDeliveryLoop: wake, ordered draining, backoff stop, cap, idle pass, shutdown, failure."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from indicator_runner.signal_delivery import SignalDeliveryLoop


class _Relay:
    def __init__(self, results: list[tuple[int, int]]) -> None:
        self.results = list(results)
        self.calls = 0

    def run_once(self) -> tuple[int, int]:
        self.calls += 1
        return self.results.pop(0) if self.results else (0, 0)


def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_wake_drains_until_a_pass_delivers_nothing() -> None:
    relay = _Relay([(1, 0), (1, 0), (0, 0)])
    loop = SignalDeliveryLoop(run_once=relay.run_once, worker_id="w", idle_interval_seconds=60.0)
    loop.start()
    try:
        loop.wake()
        assert _wait(lambda: relay.calls >= 3)
        time.sleep(0.1)
        assert relay.calls == 3
    finally:
        loop.stop(timeout=1.0)


def test_failing_pass_stops_the_drain_so_backoff_is_respected() -> None:
    relay = _Relay([(0, 1), (1, 0)])
    loop = SignalDeliveryLoop(run_once=relay.run_once, worker_id="w", idle_interval_seconds=60.0)
    assert loop.drain() == (0, 1)
    assert relay.calls == 1


def test_pass_cap_rearms_instead_of_spinning() -> None:
    relay = _Relay([(1, 0)] * 5)
    loop = SignalDeliveryLoop(
        run_once=relay.run_once, worker_id="w", idle_interval_seconds=60.0, max_passes_per_wake=2
    )
    assert loop.drain() == (2, 0)
    assert loop.wake_pending


def test_idle_interval_runs_a_recovery_pass_without_a_wake() -> None:
    relay = _Relay([])
    loop = SignalDeliveryLoop(run_once=relay.run_once, worker_id="w", idle_interval_seconds=0.05)
    loop.start()
    try:
        assert _wait(lambda: relay.calls >= 2)
    finally:
        loop.stop(timeout=1.0)


def test_stop_joins_the_thread_and_wakes_a_waiting_loop() -> None:
    relay = _Relay([])
    loop = SignalDeliveryLoop(run_once=relay.run_once, worker_id="w", idle_interval_seconds=60.0)
    loop.start()
    started = time.monotonic()
    loop.stop(timeout=2.0)
    assert not loop.alive
    assert time.monotonic() - started < 2.0


def test_exception_in_a_pass_ends_the_loop_and_reports_failure() -> None:
    errors: list[BaseException] = []

    def _boom() -> tuple[int, int]:
        raise RuntimeError("database gone")

    loop = SignalDeliveryLoop(run_once=_boom, worker_id="w", idle_interval_seconds=60.0)
    loop.bind_failure_handler(errors.append)
    loop.start()
    try:
        loop.wake()
        assert _wait(lambda: not loop.alive)
        assert isinstance(errors[0], RuntimeError)
    finally:
        loop.stop(timeout=1.0)


def test_wake_during_a_drain_is_not_lost() -> None:
    gate = threading.Event()
    calls: list[int] = []

    def _run_once() -> tuple[int, int]:
        calls.append(1)
        if len(calls) == 1:
            gate.wait(2.0)  # hold the first pass so a wake arrives mid-drain
        return (0, 0)

    loop = SignalDeliveryLoop(run_once=_run_once, worker_id="w", idle_interval_seconds=60.0)
    loop.start()
    try:
        loop.wake()
        assert _wait(lambda: len(calls) == 1)
        loop.wake()  # arrives while the first pass is blocked
        gate.set()
        assert _wait(lambda: len(calls) >= 2)
    finally:
        loop.stop(timeout=1.0)


def test_stop_during_an_active_drain_ends_between_passes() -> None:
    calls: list[float] = []

    def _slow_success() -> tuple[int, int]:
        calls.append(time.monotonic())
        time.sleep(0.05)
        return (1, 0)  # would drain forever without a stop check

    loop = SignalDeliveryLoop(run_once=_slow_success, worker_id="w", idle_interval_seconds=60.0)
    loop.start()
    loop.wake()
    assert _wait(lambda: len(calls) >= 2)
    started = time.monotonic()
    assert loop.stop(timeout=2.0) is True
    assert time.monotonic() - started < 1.0  # one in-flight pass, not the 200-pass cap
    assert not loop.alive
    settled = len(calls)
    time.sleep(0.2)
    assert len(calls) == settled  # nothing ran after stop() returned


def test_stop_reports_a_pass_that_outlives_its_budget() -> None:
    gate = threading.Event()
    entered = threading.Event()

    def _hung_post() -> tuple[int, int]:
        entered.set()
        gate.wait(5.0)
        return (0, 0)

    loop = SignalDeliveryLoop(run_once=_hung_post, worker_id="w", idle_interval_seconds=60.0)
    loop.start()
    try:
        loop.wake()
        assert entered.wait(2.0)
        assert loop.stop(timeout=0.2) is False
        assert loop.alive  # the caller knows a pass is still in flight
    finally:
        gate.set()
        assert _wait(lambda: not loop.alive)


def test_request_stop_never_blocks_and_ends_a_drain_before_its_first_pass() -> None:
    relay = _Relay([(1, 0), (1, 0)])
    loop = SignalDeliveryLoop(run_once=relay.run_once, worker_id="w", idle_interval_seconds=60.0)
    loop.request_stop()
    assert loop.stop_requested
    assert loop.drain() == (0, 0)
    assert relay.calls == 0
    assert loop.stop(timeout=0.1) is True  # never started: nothing to join
