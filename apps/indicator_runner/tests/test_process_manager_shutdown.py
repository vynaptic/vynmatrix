"""Fleet shutdown terminates every worker first and shares one grace deadline."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from indicator_runner import process_manager, signal_worker
from indicator_runner.process_manager import IndicatorProcess, IndicatorRunner


class _Process:
    def __init__(self, name: str, events: list[tuple[str, Any]], *, hangs: bool = False) -> None:
        self.name = name
        self.pid = 4000 + len(events)
        self._events = events
        self._hangs = hangs
        self._exited = False

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def terminate(self) -> None:
        self._events.append(("terminate", self.name))

    def wait(self, timeout: float | None = None) -> int:
        self._events.append(("wait", self.name, timeout))
        if self._hangs:
            raise subprocess.TimeoutExpired(cmd=self.name, timeout=timeout or 0.0)
        self._exited = True
        return 0

    def kill(self) -> None:
        self._events.append(("kill", self.name))
        self._exited = True


def _runner_with(processes: list[_Process]) -> IndicatorRunner:
    runner = IndicatorRunner(category="indicator", deployment_config={}, secrets={})
    runner.strategies = [
        IndicatorProcess(name=p.name, path=Path(), config={}, process=p, status="running")  # type: ignore[arg-type]
        for p in processes
    ]
    return runner


def test_grace_covers_the_worker_stop_budget() -> None:
    assert process_manager._STRATEGY_STOP_GRACE_SECONDS > signal_worker.WORKER_STOP_BUDGET_SECONDS


def test_shutdown_terminates_every_worker_before_waiting_on_any() -> None:
    events: list[tuple[str, Any]] = []
    processes = [_Process(f"s{i}", events) for i in range(3)]
    _runner_with(processes).shutdown()

    kinds = [event[0] for event in events]
    assert kinds[:3] == ["terminate"] * 3
    assert kinds[3:] == ["wait"] * 3
    timeouts = [event[2] for event in events if event[0] == "wait"]
    assert all(0.0 < t <= process_manager._STRATEGY_STOP_GRACE_SECONDS for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)  # one shared deadline, not per worker


def test_shutdown_kills_only_the_worker_that_outlives_the_deadline() -> None:
    events: list[tuple[str, Any]] = []
    processes = [_Process("fast", events), _Process("stuck", events, hangs=True)]
    _runner_with(processes).shutdown()

    assert ("kill", "stuck") in events
    assert ("kill", "fast") not in events
    assert all(p.poll() is not None for p in processes)
