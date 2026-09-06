"""The relay wakes on an outbox notification instead of waiting for the poll interval."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, ClassVar

import httpx
import pytest

from scoring_engine import main as scoring_main
from scoring_engine import outbox_relay
from scoring_engine.outbox_relay import OutboxRelayWorker
from scoring_engine.storage_memory import InMemoryScoreStore


def _seed_command(store: InMemoryScoreStore, signal_id: str) -> str:
    return store.enqueue_event(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={"signal_id": signal_id},
        aggregate_type="execution_command",
        aggregate_id=f"{signal_id}:u1",
        event_key=f"execution-command:{signal_id}:u1",
        ordering_key="u1",
    )


class _FakeListener:
    """Stand-in for PgNotifyListener that fires one notification when told to."""

    instances: ClassVar[list[_FakeListener]] = []

    def __init__(self, *, dsn: str, channel: str, callback: Any, **_: Any) -> None:
        self.dsn = dsn
        self.channel = channel
        self._callback = callback
        self.fire = threading.Event()
        self._stop = threading.Event()
        self.listening = True  # PostgreSQL acknowledged LISTEN
        _FakeListener.instances.append(self)

    def run_forever(self) -> None:
        if self.fire.wait(5.0):
            self._callback(self.channel, {"topic": "execution.commands"})
        self._stop.wait(5.0)

    def stop(self) -> None:
        self._stop.set()


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok"}, request=request)


def test_notification_wakes_idle_relay_before_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outbox_relay, "PgNotifyListener", _FakeListener)
    _FakeListener.instances.clear()
    store = InMemoryScoreStore()
    delivered_at: list[float] = []

    async def _go() -> float:
        delivered = asyncio.Event()

        def _handler(request: httpx.Request) -> httpx.Response:
            delivered_at.append(time.monotonic())
            delivered.set()
            return _ok(request)

        worker = OutboxRelayWorker(
            store=store,
            exec_engine_url="http://execution.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
            notify_dsn="postgresql://listener",
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            worker.run_forever(
                topics=["execution.commands"],
                poll_interval_seconds=30.0,
                stop_event=stop,
            )
        )
        try:
            await asyncio.sleep(0.3)  # first pass found nothing; the relay is idle-waiting
            _seed_command(store, "sig-notify")
            seeded_at = time.monotonic()
            _FakeListener.instances[0].fire.set()
            try:
                await asyncio.wait_for(delivered.wait(), timeout=5.0)
            except TimeoutError:
                return float("inf")
            return delivered_at[0] - seeded_at
        finally:
            stop.set()
            await task
            await worker.aclose()

    latency = asyncio.run(_go())

    assert latency < 5.0, "notification did not wake the idle relay"
    assert _FakeListener.instances[0].dsn == "postgresql://listener"
    assert _FakeListener.instances[0].channel == "outbox_events"


def test_relay_without_notify_dsn_starts_no_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_relay, "PgNotifyListener", _FakeListener)
    _FakeListener.instances.clear()
    store = InMemoryScoreStore()

    async def _go() -> None:
        worker = OutboxRelayWorker(
            store=store,
            exec_engine_url="http://execution.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_ok)),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            worker.run_forever(
                topics=["execution.commands"], poll_interval_seconds=0.05, stop_event=stop
            )
        )
        await asyncio.sleep(0.2)
        stop.set()
        await task
        await worker.aclose()

    asyncio.run(_go())
    assert _FakeListener.instances == []


def test_topic_without_consumer_is_dead_lettered_as_permanent() -> None:
    store = InMemoryScoreStore()
    event_id = store.enqueue_event(
        topic="signals.scored",
        event_type="ScoredSignal",
        payload={},
        event_key="scored-signal:legacy",
        max_attempts=5,
    )

    async def _go() -> int:
        worker = OutboxRelayWorker(
            store=store,
            exec_engine_url="http://execution.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(_ok)),
        )
        try:
            return await worker.drain_once(topics=["signals.scored"])
        finally:
            await worker.aclose()

    asyncio.run(_go())
    record = store._outbox[event_id]  # type: ignore[attr-defined]
    assert record.status == "dead_letter"
    assert record.attempts == 1


def test_inline_relay_receives_notify_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run_forever(self, **_: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(scoring_main, "OutboxRelayWorker", _Recorder)

    async def _go() -> None:
        scoring_main._start_inline_relay(
            object(),  # type: ignore[arg-type]
            "http://127.0.0.1:8000",
            relay_config=scoring_main.ScoringRelayConfig(),
            exec_engine_api_key="execution-key",
            notify_dsn="postgresql://scoring",
        )
        await asyncio.gather(scoring_main._runtime.relay_task, return_exceptions=True)
        scoring_main._runtime.relay_task = None
        scoring_main._runtime.relay_worker = None

    asyncio.run(_go())

    assert captured["notify_dsn"] == "postgresql://scoring"
    assert captured["api_key"] == "execution-key"
    assert "publisher" not in captured
    assert "publish_topics" not in captured


def test_listener_gauge_reports_acknowledged_listen_not_thread_liveness() -> None:
    gauge = outbox_relay._NOTIFY_LISTENER_UP
    if gauge is None:
        pytest.skip("prometheus_client not installed")

    class _Reconnecting:
        """Alive thread, no registered LISTEN — the reconnect loop."""

        def __init__(self) -> None:
            self.listening = False

    class _AliveThread:
        def is_alive(self) -> bool:
            return True

    worker = OutboxRelayWorker(
        store=InMemoryScoreStore(),
        exec_engine_url="http://execution.test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_ok)),
        notify_dsn="postgresql://listener",
    )
    listener = _Reconnecting()
    worker._notify_listener = listener  # type: ignore[assignment]
    worker._notify_thread = _AliveThread()  # type: ignore[assignment]

    worker._refresh_notify_gauge()
    assert gauge._value.get() == 0  # alive but reconnecting is not "up"

    listener.listening = True
    worker._refresh_notify_gauge()
    assert gauge._value.get() == 1

    worker._notify_listener = None
    worker._notify_thread = None
    worker._refresh_notify_gauge()
    assert gauge._value.get() == 0
    asyncio.run(worker.aclose())
