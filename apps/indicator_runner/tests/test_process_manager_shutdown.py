"""Fleet shutdown terminates every worker first and shares one grace deadline."""

from __future__ import annotations

import importlib
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


def test_grace_fits_inside_the_platform_supervisor_stage_share() -> None:
    """scripts/run_platform.py splits its 55 s stop cap evenly across stop orders.

    In the combined group every stage gets 55 s / <stop orders>, and the runner
    is SIGKILLed at the end of its stage, so the fleet grace plus the runner's
    own cleanup must fit inside that share.
    """
    platform_processes = importlib.import_module("scripts.platform_processes")
    env = {
        **{
            f"{role}_DATABASE_URL": f"postgresql://vm_{login}_login:secret@postgres/db"
            for role, login in (
                ("BACKEND", "backend"),
                ("SCORING", "scoring"),
                ("EXECUTION", "execution"),
                ("FEEDBACK", "feedback"),
                ("MARKET_DATA", "market_data"),
                ("INDICATOR", "indicator"),
            )
        },
        "BACKEND_ADMIN_API_KEY": "backend-secret",
        "SCORING_API_KEY": "scoring-secret",
        "EXECUTION_API_KEY": "execution-secret",
        "SCORING_ADMIN_API_KEY": "scoring-admin-secret",
        "EXECUTION_ADMIN_API_KEY": "execution-admin-secret",
        "FEEDBACK_API_KEY": "feedback-secret",
        "MARKET_DATA_API_KEY": "market-data-secret",
        "SECRETS_MASTER_KEYS": "encrypted-key-ring",
    }
    stages = len({spec.stop_order for spec in platform_processes.build_processes("all", env)})
    supervisor_stop_cap_seconds = 55.0  # run_platform.PlatformSupervisor.stop(timeout=55.0)
    assert (
        supervisor_stop_cap_seconds / stages
    ) >= process_manager._STRATEGY_STOP_GRACE_SECONDS + 1.0


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
