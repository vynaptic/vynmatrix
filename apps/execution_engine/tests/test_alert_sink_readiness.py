"""O1: fail-closed readiness gate — a live execution engine must be able to page
a human on circuit-breaker / reconciliation alerts. In the soak a ~16h breaker
outage reached nobody because no alert sink was configured."""

from __future__ import annotations

import asyncio
from typing import Any

import main
import pytest
from fastapi import FastAPI

from execution_engine.paper_order_lifecycle import PaperOrderLifecycleWorker
from lib_common.alerting import build_sinks_from_env
from lib_common.config_validation import (
    ExecutionEngineConfig,
    ExecutionPaperConfig,
    ExecutionRuntimeConfig,
    load_execution_engine_config,
)
from lib_common.task_supervision import supervise_background_task

_ALERT_ENV = (
    "EXECUTION_ALERTS_ENABLED",
    "EXECUTION_MODE",
    "EXECUTION_ENGINE_ALLOW_LIVE",
    "EXECUTION_REQUIRE_ALERT_SINK",
    "ALERT_WEBHOOK_URL",
    "ALERT_TELEGRAM_BOT_TOKEN",
    "ALERT_TELEGRAM_CHAT_ID",
    "ALERT_EMAIL_SMTP_HOST",
    "ALERT_EMAIL_TO",
)


@pytest.fixture(autouse=True)
def _clean_alert_env(monkeypatch):  # type: ignore[no-untyped-def]
    for key in _ALERT_ENV:
        monkeypatch.delenv(key, raising=False)


def _load_config():
    return load_execution_engine_config(require_database=False)


def _has_sink() -> bool:
    sinks = build_sinks_from_env()
    try:
        return bool(sinks)
    finally:
        for sink in sinks:
            sink.close()


def test_paper_mode_without_alert_sink_warns_but_starts(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    # No raise in paper mode — degrade to a warning.
    main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_service_without_database_refuses_to_start() -> None:
    engine = type("EngineWithoutDatabase", (), {})()

    with pytest.raises(RuntimeError, match="without DATABASE_URL"):
        main._verify_database_configured(engine)


def test_service_with_database_configuration_can_start() -> None:
    engine = type("EngineWithDatabase", (), {"_session_factory": object()})()

    main._verify_database_configured(engine)


def test_live_mode_without_alert_sink_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")
    with pytest.raises(RuntimeError, match="without an alert sink"):
        main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_allow_live_without_alert_sink_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")
    with pytest.raises(RuntimeError, match="without an alert sink"):
        main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_live_mode_with_enabled_webhook_starts(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")
    monkeypatch.setenv("EXECUTION_ALERTS_ENABLED", "true")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/alert")
    main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_webhook_set_but_alerts_disabled_still_refuses_in_live(monkeypatch) -> None:
    # A sink alone is not enough — the publisher is gated by EXECUTION_ALERTS_ENABLED.
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/alert")
    with pytest.raises(RuntimeError, match="without an alert sink"):
        main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_require_alert_sink_override_relaxes_live_gate(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")
    monkeypatch.setenv("EXECUTION_REQUIRE_ALERT_SINK", "false")
    # Explicit override: warn instead of raise even in live mode.
    main._verify_alert_sink_readiness(_load_config(), has_sink=_has_sink())


def test_alert_readiness_uses_startup_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    config = _load_config()
    has_sink = _has_sink()

    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("EXECUTION_ENGINE_ALLOW_LIVE", "true")

    main._verify_alert_sink_readiness(config, has_sink=has_sink)


def test_unexpected_paper_lifecycle_task_death_fails_engine_health() -> None:
    class _Engine:
        def __init__(self) -> None:
            self.health: dict[str, Any] = {"healthy": True}

        def mark_paper_order_lifecycle_health(
            self,
            *,
            healthy: bool,
            error: str | None,
            failed_orders: int = 0,
        ) -> None:
            self.health = {
                "healthy": healthy,
                "error": error,
                "failed_orders": failed_orders,
            }

    async def _task_death() -> _Engine:
        engine = _Engine()
        worker = PaperOrderLifecycleWorker(
            engine=engine,
            session_factory=lambda: None,
        )

        async def _die() -> None:
            raise RuntimeError("unexpected lifecycle defect")

        task = asyncio.create_task(_die())
        supervise_background_task(
            task,
            name="Durable paper-order lifecycle task",
            is_expected_stop=lambda: worker.is_stopping,
            cancelled_is_error=True,
            on_failure=lambda error: engine.mark_paper_order_lifecycle_health(
                healthy=False,
                error=error,
            ),
        )
        await asyncio.gather(task, return_exceptions=True)
        # Done-callbacks run via call_soon; yield once so they fire.
        await asyncio.sleep(0)
        return engine

    engine = asyncio.run(_task_death())

    assert engine.health["healthy"] is False
    assert "died" in str(engine.health["error"])
    assert "unexpected lifecycle defect" in str(engine.health["error"])


def test_lifespan_stops_paper_worker_before_first_shutdown_yield(monkeypatch) -> None:
    """Teardown must close the worker race before yielding the event loop."""

    async def _exercise() -> list[str]:
        trace: list[str] = []

        class _PaperWorker:
            def __init__(
                self,
                *,
                engine: Any,
                session_factory: Any,
                account_serializer: Any,
            ) -> None:
                del engine, session_factory, account_serializer
                self._stop_event = asyncio.Event()

            async def process_once(self) -> int:
                return 0

            async def run(self) -> None:
                await self._stop_event.wait()

            async def stop(self) -> None:
                trace.append("paper_worker_stopped")
                self._stop_event.set()

            @property
            def is_stopping(self) -> bool:
                return self._stop_event.is_set()

        class _Engine:
            _execution_position_store = None
            _session_factory = object()
            account_execution_serializer = object()

            def reconciliation_status(self) -> dict[str, object]:
                return {}

            def paper_order_lifecycle_status(self) -> dict[str, object]:
                return {}

            def mark_paper_order_lifecycle_health(self, **kwargs: object) -> None:
                del kwargs

            async def close(self) -> None:
                trace.append("engine_closed")

        class _Shutdown:
            def __init__(self) -> None:
                self.handler: Any = None

            def on_shutdown(self, handler: Any) -> None:
                self.handler = handler

            async def execute(self) -> None:
                async def _observe_first_yield() -> None:
                    await asyncio.sleep(0)
                    trace.append("event_loop_yielded")

                observer = asyncio.create_task(_observe_first_yield())
                await self.handler()
                await observer

        shutdown = _Shutdown()
        monkeypatch.setattr(main, "PaperOrderLifecycleWorker", _PaperWorker)
        monkeypatch.setattr(main, "get_shutdown_coordinator", lambda **kwargs: shutdown)
        monkeypatch.setattr(main, "dispose_engine", lambda engine: None)
        monkeypatch.setattr(main, "_paper_order_task", None)
        monkeypatch.setattr(main, "_reconciliation_task", None)

        config = ExecutionEngineConfig(
            runtime=ExecutionRuntimeConfig(
                paper=ExecutionPaperConfig(use_local_broker=True),
            )
        )
        engine = _Engine()
        lifespan = main._make_lifespan(
            engine,  # type: ignore[arg-type]
            None,
            config,
            has_alert_sink=False,
        )

        async with lifespan(FastAPI()):
            trace.append("service_running")
        return trace

    trace = asyncio.run(_exercise())

    assert trace.index("paper_worker_stopped") < trace.index("event_loop_yielded")
    assert trace.index("event_loop_yielded") < trace.index("engine_closed")
