# Event-Driven Delivery and Bounded Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement [the approved spec](../specs/2026-09-05-event-driven-delivery-bounded-pools-design.md): strategy subprocesses get a fixed 2/0 pool, the scoring relay wakes on the existing `outbox_events` notification, the signal worker delivers through a dedicated loop outside its processing lock, and the four consumer-less outbox topics are retired with their producers.

**Architecture:** Four independent changes inside existing processes, no topology or grant change. The launcher injects pool values on the indicator spec; `OutboxRelayWorker` gains a wired LISTEN plus a liveness gauge; a new `SignalDeliveryLoop` thread owns every `DurableSignalRelay.run_once` call and is woken after each committed transition; producers of `signals.ingested`, `signals.scored`, `execution.results` and `feedback.ready` are deleted and a data migration retires their rows.

**Tech Stack:** Python 3.11, SQLAlchemy 2, Alembic, pydantic, psycopg2 LISTEN/NOTIFY, threading, asyncio, httpx, pytest; repository tooling `vmdev` (`vmdev audit --strict`, `vmdev test all`, `vmdev build docker --from-config`).

---

## Ground rules for every task

- Work on branch `feat/event-driven-delivery-bounded-pools` (Task 0). `main` is protected by a required `ci-gate` check, so the work lands through a pull request.
- Commits are authored as the repository-local identity `vynaptic <325089101+vynaptic@users.noreply.github.com>` (already configured); end every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run tests from the prepared tooling environment: `source .venv-dev/bin/activate` and `export EXECUTION_MODE=paper EXECUTION_ENGINE_ALLOW_LIVE=false` first. Use `python -m pytest -q -p no:cacheprovider <paths>`.
- After every code edit run `ruff check <files>` and `ruff format <files>` on the touched files.
- Never rewrite or revert changes you did not make; another agent commits to this tree. Run `git status --short` before each commit and stage only the listed files.
- Keep `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false`; nothing in this plan touches authority, freshness or risk logic.

## File structure

| Path | Responsibility after this plan |
| --- | --- |
| `scripts/platform_processes.py` | Fixed pool values on the indicator spec; new env names in the scoring and indicator allowlists |
| `libs/python/lib_common/lib_common/config_validation.py` | `ScoringRelayConfig.notify_enabled`, reduced topic defaults, `IndicatorWorkerConfig.signal_relay_idle_interval_seconds` |
| `apps/scoring_engine/scoring_engine/outbox_relay.py` | Relay with wired LISTEN, listener gauge, execution-topic-only delivery |
| `apps/scoring_engine/scoring_engine/main.py` | Passes `notify_dsn` into both relay constructors; publisher plumbing removed |
| `apps/indicator_runner/indicator_runner/signal_delivery.py` | **New.** `SignalDeliveryLoop` thread |
| `apps/indicator_runner/indicator_runner/signal_worker.py` | Injects the loop, wakes it after commits, ordered shutdown, idle-interval main loop |
| `apps/indicator_runner/indicator_runner/panel_runtime.py` | Takes a `wake_delivery` callback instead of `relay_once` |
| `apps/scoring_engine/scoring_engine/{api.py,services/rebalance_service.py}` | No longer enqueue `signals.ingested` / `signals.scored`; `pipeline_events.py` deleted |
| `apps/execution_engine/execution_engine/{execution_persistence.py,execution_log_store.py,paper_order_lifecycle.py,engine.py,_services.py}`, `apps/execution_engine/main.py` | No `execution.results` event; `causation_event_id` in `execution_logs.execution_details` |
| `apps/feedback_loop_engine/feedback_loop_engine/{engine.py,main.py}` | No `feedback.ready` event |
| `libs/python/lib_common/lib_common/{internal_events.py,__init__.py}` | Four event classes removed; `event_bus.py` deleted |
| `scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py` | **New.** Marks undelivered rows of the retired topics published |
| `docker/docker-compose.stack.yml`, `.env.example`, `docs/CONFIGURATION.md`, `docs/E2E_VERIFICATION_GUIDE.md`, `CHANGELOG.md`, the spec | Configuration surface and documentation |
| `apps/indicator_runner/tests/test_multi_worker_delivery_postgres_integration.py` | **New.** Twenty-worker PostgreSQL acceptance module |

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1: Confirm identity and start point**

Run:
```bash
cd /Users/virendrayadav/workspace/VisionMaverick/vynmatrix
git status --short
git log --oneline -1
git config user.name && git config user.email
```
Expected: clean tree, HEAD is `551663f docs: add event-driven delivery and bounded pools design spec` (or a later commit that contains it), identity `vynaptic` / `325089101+vynaptic@users.noreply.github.com`.

- [ ] **Step 2: Create the branch**

```bash
git checkout -b feat/event-driven-delivery-bounded-pools
```

---

### Task 1: Launcher pool values and env allowlists

**Files:**
- Modify: `scripts/platform_processes.py` (`_SCORING` ~line 140, `_INDICATOR` ~line 105, indicator `child.update` ~line 442)
- Test: `tests/test_platform_process_supervision.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_platform_process_supervision.py`:

```python
def test_indicator_child_uses_bounded_strategy_pool_and_forwards_idle_interval(
    runtime_env: dict[str, str],
) -> None:
    runtime_env["STRATEGY_LIST"] = "SwingHighLowPMO"
    runtime_env["DB_POOL_SIZE"] = "3"
    runtime_env["DB_MAX_OVERFLOW"] = "2"
    runtime_env["DB_POOL_CONNECTION_BUDGET"] = "5"
    runtime_env["SIGNAL_RELAY_IDLE_INTERVAL_SEC"] = "7"
    specs = {spec.name: spec for spec in _module().build_processes("workers", runtime_env)}

    indicator = specs["indicator"].environment
    # Strategy grandchildren inherit this environment, so the fixed 2/0 pool
    # bounds every worker to two pooled connections plus its LISTEN connection.
    assert indicator["DB_POOL_SIZE"] == "2"
    assert indicator["DB_MAX_OVERFLOW"] == "0"
    assert indicator["DB_POOL_CONNECTION_BUDGET"] == "2"
    assert indicator["SIGNAL_RELAY_IDLE_INTERVAL_SEC"] == "7"

    feedback = specs["feedback"].environment
    assert feedback["DB_POOL_SIZE"] == "3"
    assert feedback["DB_MAX_OVERFLOW"] == "2"
    assert "SIGNAL_RELAY_IDLE_INTERVAL_SEC" not in feedback


def test_scoring_child_forwards_outbox_notify_flag(runtime_env: dict[str, str]) -> None:
    runtime_env["SCORING_OUTBOX_NOTIFY_ENABLED"] = "false"
    specs = {spec.name: spec for spec in _module().build_processes("application", runtime_env)}
    assert specs["scoring"].environment["SCORING_OUTBOX_NOTIFY_ENABLED"] == "false"
    assert "SCORING_OUTBOX_NOTIFY_ENABLED" not in specs["execution"].environment
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q -p no:cacheprovider tests/test_platform_process_supervision.py -k "bounded_strategy_pool or notify_flag"`
Expected: 2 failed (`KeyError: 'SIGNAL_RELAY_IDLE_INTERVAL_SEC'` and `KeyError: 'SCORING_OUTBOX_NOTIFY_ENABLED'`).

- [ ] **Step 3: Implement**

In `scripts/platform_processes.py`:

Add to the `_SCORING` frozenset:
```python
        "SCORING_OUTBOX_NOTIFY_ENABLED",
```

Add to the `_INDICATOR` frozenset:
```python
        "SIGNAL_RELAY_IDLE_INTERVAL_SEC",
```

In `build_processes`, extend the indicator `child.update({...})` so it reads:
```python
        child.update(
            {
                "API_KEY": _required(env, "SCORING_API_KEY"),
                "SIGNAL_API_URL": f"http://{app_host}:8001",
                "INDICATOR_ALLOW_DEV_DISCOVERY": "false",
                "HEALTH_CHECK_PORT": "8080",
                "PROMETHEUS_MULTIPROC_DIR": "/tmp/vynmatrix-prometheus/indicator",
                # Every strategy grandchild inherits this environment. A worker
                # has exactly two pool users (processing thread and delivery
                # loop) and one raw LISTEN connection, so bound it explicitly
                # instead of inheriting the 3+2 default of the API children.
                "DB_POOL_SIZE": "2",
                "DB_MAX_OVERFLOW": "0",
                "DB_POOL_CONNECTION_BUDGET": "2",
            }
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest -q -p no:cacheprovider tests/test_platform_process_supervision.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/platform_processes.py tests/test_platform_process_supervision.py
git commit -m "feat(launcher): bound strategy worker pools and forward relay settings

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Configuration models

**Files:**
- Modify: `libs/python/lib_common/lib_common/config_validation.py` (`ScoringRelayConfig` ~line 615, `IndicatorWorkerConfig` ~line 425, relay parse ~line 1239, `load_indicator_worker_config` ~line 750)
- Create: `libs/python/lib_common/tests/test_relay_and_worker_config.py`

- [ ] **Step 1: Write the failing tests**

Create `libs/python/lib_common/tests/test_relay_and_worker_config.py`:

```python
"""Relay notification flag and worker delivery-loop interval configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lib_common.config_validation import (
    IndicatorWorkerConfig,
    ScoringRelayConfig,
    load_indicator_worker_config,
)


def test_relay_config_defaults_cover_only_execution_topics_and_enable_notify() -> None:
    config = ScoringRelayConfig()
    assert config.topics == ("execution.commands", "execution.rebalance.commands")
    assert config.notify_enabled is True
    assert not hasattr(config, "publish_topics")


def test_worker_idle_interval_is_bounded() -> None:
    default = IndicatorWorkerConfig(database_url="postgresql://vm_indicator_login:x@db/vm")
    assert default.signal_relay_idle_interval_seconds == 5
    for bad in (0, 61):
        with pytest.raises(ValidationError):
            IndicatorWorkerConfig(
                database_url="postgresql://vm_indicator_login:x@db/vm",
                signal_relay_idle_interval_seconds=bad,
            )


def test_worker_idle_interval_parses_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://vm_indicator_login:secret@localhost:5432/vm")
    monkeypatch.setenv("SIGNAL_RELAY_IDLE_INTERVAL_SEC", "9")
    assert load_indicator_worker_config().signal_relay_idle_interval_seconds == 9
    monkeypatch.setenv("SIGNAL_RELAY_IDLE_INTERVAL_SEC", "0")
    with pytest.raises(ValueError):
        load_indicator_worker_config()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q -p no:cacheprovider libs/python/lib_common/tests/test_relay_and_worker_config.py`
Expected: 3 failed (`publish_topics` still present, unknown field `signal_relay_idle_interval_seconds`).

- [ ] **Step 3: Implement**

Replace the `ScoringRelayConfig` class body:
```python
class ScoringRelayConfig(_FrozenConfig):
    """Transactional-outbox relay polling and topic controls."""

    inline: bool = False
    # Wake the claim loop on the existing outbox_events notification; polling
    # on poll_interval_seconds remains the recovery path either way.
    notify_enabled: bool = True
    topics: tuple[str, ...] = (
        "execution.commands",
        "execution.rebalance.commands",
    )
    poll_interval_seconds: float = Field(default=2.0, gt=0.0)
    batch_size: int = Field(default=100, ge=1)
    lease_seconds: int = Field(default=60, ge=1)
    max_backlog_age_seconds: int = Field(default=300, ge=1)
```

In the relay parse block (inside the `ScoringRelayConfig(...)` construction near line 1239), replace the `topics=` default list and delete the whole `publish_topics=` argument, so the block reads:
```python
            relay=ScoringRelayConfig(
                inline=parse_bool_env(
                    "SCORING_OUTBOX_RELAY_INLINE",
                    default=False,
                    logger=logger,
                    strict=True,
                ),
                notify_enabled=parse_bool_env(
                    "SCORING_OUTBOX_NOTIFY_ENABLED",
                    default=True,
                    logger=logger,
                    strict=True,
                ),
                topics=tuple(
                    parse_list_env(
                        "SCORING_OUTBOX_TOPICS",
                        default=[
                            "execution.commands",
                            "execution.rebalance.commands",
                        ],
                    )
                ),
                poll_interval_seconds=parse_float_env(
```
(the remaining `poll_interval_seconds`, `batch_size`, `lease_seconds`, `max_backlog_age_seconds` arguments are unchanged).

In `IndicatorWorkerConfig`, after `catchup_floor_seconds`, add:
```python
    # Idle recovery pass cadence of the signal delivery loop; wakes after
    # commits make ordinary delivery independent of this value.
    signal_relay_idle_interval_seconds: int = Field(default=5, ge=1, le=60)
```

In `load_indicator_worker_config`, after the `catchup_floor_seconds=parse_int_env(...)` argument, add:
```python
        signal_relay_idle_interval_seconds=parse_int_env(
            "SIGNAL_RELAY_IDLE_INTERVAL_SEC",
            default=5,
            min_value=1,
            max_value=60,
            logger=logger,
            strict=True,
        ),
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest -q -p no:cacheprovider libs/python/lib_common/tests/test_relay_and_worker_config.py libs/python/lib_common/tests/test_config.py`
Expected: pass. If `test_config.py` asserts the old six-topic default, update that assertion to the two execution topics.

- [ ] **Step 5: Commit**

```bash
git add libs/python/lib_common/lib_common/config_validation.py libs/python/lib_common/tests/test_relay_and_worker_config.py libs/python/lib_common/tests/test_config.py
git commit -m "feat(config): relay notify flag, worker idle interval, execution-only relay topics

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Scoring relay wake-up, listener gauge, publisher removal

**Files:**
- Modify: `apps/scoring_engine/scoring_engine/outbox_relay.py`
- Modify: `apps/scoring_engine/scoring_engine/main.py`
- Create: `apps/scoring_engine/tests/test_outbox_relay_notify.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/scoring_engine/tests/test_outbox_relay_notify.py`:

```python
"""The relay wakes on an outbox notification instead of waiting for the poll interval."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

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

    instances: list[_FakeListener] = []

    def __init__(self, *, dsn: str, channel: str, callback: Any, **_: Any) -> None:
        self.dsn = dsn
        self.channel = channel
        self._callback = callback
        self.fire = threading.Event()
        self._stop = threading.Event()
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

    def _handler(request: httpx.Request) -> httpx.Response:
        delivered_at.append(time.monotonic())
        return _ok(request)

    async def _go() -> float:
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
            deadline = time.monotonic() + 5.0
            while not delivered_at and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            return (delivered_at[0] - seeded_at) if delivered_at else float("inf")
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q -p no:cacheprovider apps/scoring_engine/tests/test_outbox_relay_notify.py`
Expected: failures. `test_notification_wakes_idle_relay_before_poll_interval` passes only if the relay wires the listener (it does when `notify_dsn` is given, so this one may already pass); `test_topic_without_consumer_is_dead_lettered_as_permanent` fails because the no-op publisher "delivers" it; `test_inline_relay_receives_notify_dsn` fails with `TypeError: unexpected keyword argument 'notify_dsn'`.

- [ ] **Step 3: Implement the relay changes**

In `apps/scoring_engine/scoring_engine/outbox_relay.py`:

1. Delete the import line `from lib_common.event_bus import EventPublisher, NoOpEventPublisher`.

2. After `_OUTBOX_PROGRESS_READY = gauge(...)`, add:
```python
_NOTIFY_LISTENER_UP = gauge(
    "vm_scoring_outbox_notify_listener_up",
    "1 while the outbox_events LISTEN thread is alive, else 0",
)
```

3. Replace the constructor signature and body so publisher plumbing is gone:
```python
    def __init__(
        self,
        *,
        store: ScoreStore,
        exec_engine_url: str,
        consumer_name: str = "scoring-outbox-relay",
        http_client: httpx.AsyncClient | None = None,
        notify_dsn: str | None = None,
        notify_channel: str = "outbox_events",
        api_key: str | None = None,
    ) -> None:
        self._store = store
        self._exec_engine_url = exec_engine_url.rstrip("/")
        self._consumer_name = consumer_name
        self._client = http_client or httpx.AsyncClient(timeout=10)
        self._notify_dsn = notify_dsn
        self._notify_channel = notify_channel
        self._api_key = api_key
        self._notify_listener: PgNotifyListener | None = None
        self._notify_thread: threading.Thread | None = None
```

4. Replace `_start_notify_listener` and `_stop_notify_listener` and add the gauge helpers:
```python
    @staticmethod
    def _set_notify_gauge(value: int) -> None:
        if _NOTIFY_LISTENER_UP is not None:
            _NOTIFY_LISTENER_UP.set(value)

    def _refresh_notify_gauge(self) -> None:
        alive = self._notify_thread is not None and self._notify_thread.is_alive()
        self._set_notify_gauge(1 if alive else 0)

    def _start_notify_listener(
        self,
        notify_event: asyncio.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Start the PostgreSQL LISTEN thread when a DSN is configured."""
        if not self._notify_dsn:
            self._set_notify_gauge(0)
            return

        def _on_notify(_channel: str, _payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(notify_event.set)

        self._notify_listener = PgNotifyListener(
            dsn=self._notify_dsn,
            channel=self._notify_channel,
            callback=_on_notify,
        )
        self._notify_thread = threading.Thread(
            target=self._notify_listener.run_forever,
            daemon=True,
            name=f"{self._consumer_name}-pg-listen",
        )
        self._notify_thread.start()
        self._set_notify_gauge(1)
        logger.info(
            "outbox.listen_started",
            extra={"channel": self._notify_channel, "consumer": self._consumer_name},
        )

    def _stop_notify_listener(self) -> None:
        if self._notify_listener is not None:
            self._notify_listener.stop()
        if self._notify_thread is not None:
            self._notify_thread.join(timeout=2.0)
        self._notify_listener = None
        self._notify_thread = None
        self._set_notify_gauge(0)
```

5. In `run_forever`, immediately after `self._update_backlog_gauge(topic_list)` inside the `while` loop, add:
```python
                self._refresh_notify_gauge()
```

6. Replace the whole `_deliver` method:
```python
    async def _deliver(self, record: OutboxRecord) -> dict[str, Any]:
        if record.topic in {"execution.commands", "execution.rebalance.commands"}:
            return await self._deliver_execution_command(record)
        # The observational topics were retired with their producers; a stray
        # row must not loop through retries, so classify it permanent.
        msg = f"Outbox topic {record.topic!r} has no consumer"
        raise OutboxDeliveryError(msg, failure_class="permanent")
```

7. Update the module docstring sentence about publishing if it mentions an event bus, and run `ruff check` to find any now-unused import (`Iterable` may still be used by `drain_once`).

- [ ] **Step 4: Implement the main.py wiring**

In `apps/scoring_engine/scoring_engine/main.py`:

1. Delete `from lib_common.event_bus import EventPublisher, NoOpEventPublisher` and the whole `_build_event_publisher` function.

2. Replace `_run_relay_worker`:
```python
async def _run_relay_worker(
    store: ScoreStore,
    exec_url: str,
    *,
    relay_config: ScoringRelayConfig,
    exec_engine_api_key: str | None,
    notify_dsn: str | None,
) -> None:
    worker = OutboxRelayWorker(
        store=store,
        exec_engine_url=exec_url,
        api_key=exec_engine_api_key,
        notify_dsn=notify_dsn,
    )
    try:
        await worker.run_forever(
            topics=relay_config.topics,
            poll_interval_seconds=relay_config.poll_interval_seconds,
            limit=relay_config.batch_size,
            lease_seconds=relay_config.lease_seconds,
        )
    finally:
        await worker.aclose()
```

3. Replace `_start_inline_relay`:
```python
def _start_inline_relay(
    store: ScoreStore,
    exec_url: str,
    *,
    relay_config: ScoringRelayConfig,
    exec_engine_api_key: str | None,
    notify_dsn: str | None,
) -> OutboxRelayWorker:
    _runtime.relay_worker = OutboxRelayWorker(
        store=store,
        exec_engine_url=exec_url,
        api_key=exec_engine_api_key,
        notify_dsn=notify_dsn,
    )
    _runtime.relay_task = asyncio.create_task(
        _runtime.relay_worker.run_forever(
            topics=relay_config.topics,
            poll_interval_seconds=relay_config.poll_interval_seconds,
            limit=relay_config.batch_size,
            lease_seconds=relay_config.lease_seconds,
        ),
        name="scoring-outbox-relay",
    )
    # A dead inline relay stops ALL execution-command delivery while /health
    # stays green, so alerting must see it (OB-1).
    supervise_background_task(
        _runtime.relay_task,
        name="Scoring outbox relay task",
        up_gauge=_RELAY_UP,
    )
    return _runtime.relay_worker
```

4. `_make_lifespan` gains `notify_dsn: str | None` as a keyword-only parameter and passes `notify_dsn=notify_dsn` into `_start_inline_relay(...)`.

5. In `main()`, after `exec_engine_api_key = ...`, add:
```python
    # The relay listens as the scoring login on the existing outbox_events
    # trigger; LISTEN needs no extra privilege. Polling stays as recovery.
    notify_dsn = db_url if config.runtime.relay.notify_enabled else None
```
and pass `notify_dsn=notify_dsn` to both `_run_relay_worker(...)` and `_make_lifespan(...)`.

- [ ] **Step 5: Run to verify they pass**

Run: `python -m pytest -q -p no:cacheprovider apps/scoring_engine/tests/test_outbox_relay_notify.py apps/scoring_engine/tests/test_outbox_dispatch.py apps/scoring_engine/tests/test_outbox_relay_failure_modes.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/scoring_engine/scoring_engine/outbox_relay.py apps/scoring_engine/scoring_engine/main.py apps/scoring_engine/tests/test_outbox_relay_notify.py
git commit -m "feat(scoring): wake the outbox relay on outbox_events notifications

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Signal delivery loop

**Files:**
- Create: `apps/indicator_runner/indicator_runner/signal_delivery.py`
- Modify: `apps/indicator_runner/indicator_runner/signal_worker.py` (imports ~line 14, `__init__` ~line 140, `start` ~line 262, `stop` ~line 276, `relay_once` ~line 305, bar path ~line 859, `_build_worker` ~line 1290, `main` ~line 1370)
- Modify: `apps/indicator_runner/indicator_runner/panel_runtime.py` (lines 58, 68, 281)
- Create: `apps/indicator_runner/tests/test_signal_delivery.py`
- Modify: `apps/indicator_runner/tests/test_signal_worker.py`, `apps/indicator_runner/tests/test_synchronized_panel_runtime.py`

- [ ] **Step 1: Write the failing loop tests**

Create `apps/indicator_runner/tests/test_signal_delivery.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest -q -p no:cacheprovider apps/indicator_runner/tests/test_signal_delivery.py`
Expected: `ModuleNotFoundError: No module named 'indicator_runner.signal_delivery'`.

- [ ] **Step 3: Create the loop module**

Create `apps/indicator_runner/indicator_runner/signal_delivery.py`:

```python
"""Serialized signal delivery on a thread outside the strategy processing lock."""

from __future__ import annotations

import threading
from collections.abc import Callable

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

    def stop(self, *, timeout: float) -> None:
        """Stop the thread and wait up to ``timeout`` seconds for the current pass."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Signal delivery loop did not stop within %.1fs",
                    timeout,
                    worker_id=self._worker_id,
                )

    def drain(self) -> tuple[int, int]:
        """Run passes until one delivers nothing; return (delivered, failed) totals.

        A pass that only fails ends the drain so the relay's per-record backoff
        is respected. Hitting the pass cap re-arms the wake instead of spinning.
        """
        delivered_total = 0
        failed_total = 0
        for _ in range(self._max_passes_per_wake):
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
            except Exception as exc:  # boundary: one failed pass ends the loop and the process
                logger.exception("Signal delivery loop failed", worker_id=self._worker_id)
                if self._failure_handler is not None:
                    self._failure_handler(exc)
                return


__all__ = ["SignalDeliveryLoop"]
```

- [ ] **Step 4: Run the loop tests**

Run: `python -m pytest -q -p no:cacheprovider apps/indicator_runner/tests/test_signal_delivery.py`
Expected: 7 passed.

- [ ] **Step 5: Write the failing worker wiring tests**

In `apps/indicator_runner/tests/test_signal_worker.py`, extend the `_build_worker` helper with a `delivery_loop` parameter: add `delivery_loop: Any | None = None,` to its signature (after `delivery_emitter`) and pass `delivery_loop=delivery_loop,` into the `SignalWorker(...)` call. Then append:

```python
class _ObservingStrategy(PureSignalStrategy):
    def __init__(self) -> None:
        super().__init__(
            strategy_id="delivery_wiring_probe",
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        return None


class _FakeDeliveryLoop:
    idle_interval_seconds = 5.0

    def __init__(self) -> None:
        self.wakes = 0
        self.started = False
        self.stopped_with: float | None = None
        self.alive = False
        self.failure_handler: Any = None

    def bind_failure_handler(self, handler: Any) -> None:
        self.failure_handler = handler

    def start(self) -> None:
        self.started = True
        self.alive = True

    def wake(self) -> None:
        self.wakes += 1

    def stop(self, *, timeout: float) -> None:
        self.stopped_with = timeout
        self.alive = False


def test_worker_wakes_an_injected_delivery_loop_instead_of_delivering_inline(
    session_factory: sessionmaker,
) -> None:
    loop = _FakeDeliveryLoop()
    worker = _build_worker(session_factory, _ObservingStrategy(), delivery_loop=loop)

    worker.start()
    assert loop.started is True
    assert loop.wakes == 1  # start() hands the committed backlog to the loop
    assert loop.failure_handler is not None
    assert worker.delivery_alive is True
    assert worker.delivery_idle_interval_seconds == 5.0

    worker.deliver_after_commit()
    assert loop.wakes == 2

    worker.stop()
    assert loop.stopped_with is not None and loop.stopped_with >= 10.0
    assert worker.delivery_alive is False


def test_worker_without_delivery_loop_delivers_inline(session_factory: sessionmaker) -> None:
    worker = _build_worker(session_factory, _ObservingStrategy())
    assert worker.delivery_alive is True
    assert worker.delivery_idle_interval_seconds == 1.0
    assert worker.deliver_after_commit() is None  # runs relay_once synchronously


def test_delivery_failure_marks_the_worker_failed(session_factory: sessionmaker) -> None:
    loop = _FakeDeliveryLoop()
    worker = _build_worker(session_factory, _ObservingStrategy(), delivery_loop=loop)
    worker.start()
    loop.failure_handler(RuntimeError("relay database gone"))
    assert worker.failed is True
    assert isinstance(worker.terminal_error, RuntimeError)
    worker.stop()
```

Also update the panel runtime test at `apps/indicator_runner/tests/test_synchronized_panel_runtime.py` lines 593 to 604: rename the stub and keyword:
```python
    def wake_delivery() -> None:
        relay_calls.append(None)

    runtime = SynchronizedPanelRuntime(
        ...
        processing_lock=threading.RLock(),
        wake_delivery=wake_delivery,
        mark_transition_failure=lambda exc, key: failures.append((exc, key)),
        ...
    )
```

And in the fake worker used by `test_main_returns_nonzero_for_listener_delivery_failure` (class `_FailedWorker`, ~line 1272) add two class attributes:
```python
        delivery_alive = True
        delivery_idle_interval_seconds = 1.0
```
Apply the same two attributes to any other fake worker class in that file that `signal_worker.main` drives (search `catchup_floor_seconds = 30`).

- [ ] **Step 6: Run to verify they fail**

Run: `python -m pytest -q -p no:cacheprovider apps/indicator_runner/tests/test_signal_worker.py -k "delivery" apps/indicator_runner/tests/test_synchronized_panel_runtime.py`
Expected: failures (`unexpected keyword argument 'delivery_loop'`, `unexpected keyword argument 'wake_delivery'`).

- [ ] **Step 7: Implement the worker wiring**

In `apps/indicator_runner/indicator_runner/panel_runtime.py`:
- line 58: `relay_once: Callable[[], tuple[int, int]],` becomes `wake_delivery: Callable[[], None],`
- line 68: `self._relay_once = relay_once` becomes `self._wake_delivery = wake_delivery`
- line 281: `self._relay_once()` becomes `self._wake_delivery()`

In `apps/indicator_runner/indicator_runner/signal_worker.py`:

1. Add the import next to the other `indicator_runner` imports:
```python
from indicator_runner.signal_delivery import SignalDeliveryLoop
```
and the constant after `HISTORICAL_REBUILD_EXIT_CODE = 75`:
```python
# One HTTP attempt budget (10s timeout) plus retry slack; the loop is joined
# for at most this long before the engine is disposed.
_DELIVERY_STOP_TIMEOUT_SECONDS = 15.0
```

2. Add the constructor parameter after `signal_relay: DurableSignalRelay,`:
```python
        delivery_loop: SignalDeliveryLoop | None = None,
```
and after `self._signal_relay = signal_relay`:
```python
        # A supervised process injects the loop; unit tests and offline tooling
        # leave it None and deliver inline through relay_once().
        self._delivery = delivery_loop
        if self._delivery is not None:
            self._delivery.bind_failure_handler(self._mark_delivery_failure)
```

3. In the `SynchronizedPanelRuntime(...)` construction replace `relay_once=self.relay_once,` with `wake_delivery=self.deliver_after_commit,`.

4. In `start()`, replace the block
```python
        # Recover every committed, undelivered signal before accepting a newer
        # market bar. The outbox claim commits before HTTP, so no strategy or
        # watermark lock is held across the network call.
        self.relay_once()
```
with
```python
        # Hand every committed, undelivered signal to delivery before accepting
        # a newer market bar. With an injected loop this only wakes the thread;
        # the outbox claim commits before HTTP either way, so no strategy or
        # watermark lock is ever held across the network call.
        if self._delivery is not None:
            self._delivery.start()
        self.deliver_after_commit()
```

5. Replace `stop()`:
```python
    def stop(self) -> None:
        """Stop the worker gracefully: listener first, then delivery, before engine disposal."""
        self._stop_event.set()
        if self._listener is not None:
            self._listener.stop()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=5.0)
        if self._delivery is not None:
            self._delivery.stop(timeout=_DELIVERY_STOP_TIMEOUT_SECONDS)
        logger.info("Signal worker stopped", worker_id=self._worker_id)
```

6. After `relay_once`, add:
```python
    def deliver_after_commit(self) -> None:
        """Hand committed signals to delivery without holding the processing lock.

        Outside a supervised process no loop is injected and delivery runs
        inline, exactly as before this method existed.
        """
        if self._delivery is not None:
            self._delivery.wake()
            return
        self.relay_once()

    @property
    def delivery_alive(self) -> bool:
        """False once an injected delivery loop has stopped or died."""
        return self._delivery is None or self._delivery.alive

    @property
    def delivery_idle_interval_seconds(self) -> float:
        """Main-loop wait between recovery checks."""
        if self._delivery is None:
            return 1.0
        return self._delivery.idle_interval_seconds

    def _mark_delivery_failure(self, exc: BaseException) -> None:
        if self._terminal_error is None:
            self._terminal_error = exc
        self._failure_event.set()
```

7. In the bar path (the line `self.relay_once()` that follows `self._watermark.remember(advanced)` at ~line 859), replace with `self.deliver_after_commit()`.

8. In `_build_worker`, after `signal_relay = DurableSignalRelay(...)`, add:
```python
    delivery_loop = SignalDeliveryLoop(
        run_once=signal_relay.run_once,
        worker_id=resolved_worker_id,
        idle_interval_seconds=float(startup.signal_relay_idle_interval_seconds),
    )
```
and pass `delivery_loop=delivery_loop,` into `SignalWorker(...)`.

9. In `main()`, replace the `while` loop with:
```python
        idle_seconds = worker.delivery_idle_interval_seconds
        last_catchup = time.monotonic()
        while not stop_event.is_set():
            if worker.failed:
                if worker.rebuild_required:
                    logger.warning(
                        "Signal worker exiting for supervised historical rebuild",
                        worker_id=args.worker_id,
                        error=str(worker.terminal_error),
                    )
                    exit_code = HISTORICAL_REBUILD_EXIT_CODE
                else:
                    logger.error(
                        "Signal worker exiting after terminal delivery failure",
                        worker_id=args.worker_id,
                        error=str(worker.terminal_error),
                    )
                    exit_code = 1
                break
            if (
                not stop_event.is_set()
                and worker._listener_thread
                and not worker._listener_thread.is_alive()
                and worker._dsn
            ):
                logger.error("Signal worker listener thread stopped unexpectedly")
                exit_code = 1
                break
            if not stop_event.is_set() and not worker.delivery_alive:
                logger.error("Signal worker delivery loop stopped unexpectedly")
                exit_code = 1
                break
            if stop_event.wait(idle_seconds):
                break
            if time.monotonic() - last_catchup >= catchup_floor_sec:
                worker.catchup_all()
                last_catchup = time.monotonic()
```
(the per-second `worker.relay_once()` call is gone; the loop's own idle pass replaces it).

- [ ] **Step 8: Run to verify they pass**

Run: `python -m pytest -q -p no:cacheprovider apps/indicator_runner/tests`
Expected: all pass. If `vmdev audit --strict` later reports `signal_worker.py` over the 1,800-line production cap, move `deliver_after_commit`, `delivery_alive`, `delivery_idle_interval_seconds` and `_mark_delivery_failure` unchanged into a mixin class in `signal_delivery.py` and inherit it; the file is 1,400 lines today so this is not expected.

- [ ] **Step 9: Commit**

```bash
git add apps/indicator_runner/indicator_runner/signal_delivery.py apps/indicator_runner/indicator_runner/signal_worker.py apps/indicator_runner/indicator_runner/panel_runtime.py apps/indicator_runner/tests/test_signal_delivery.py apps/indicator_runner/tests/test_signal_worker.py apps/indicator_runner/tests/test_synchronized_panel_runtime.py
git commit -m "feat(indicator): deliver signals from a dedicated loop outside the processing lock

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Retire the scoring observational topics

**Files:**
- Delete: `apps/scoring_engine/scoring_engine/pipeline_events.py`
- Modify: `apps/scoring_engine/scoring_engine/api.py` (import line 52, `_emit_pipeline_events` lines 207-219, call line 519)
- Modify: `apps/scoring_engine/scoring_engine/services/rebalance_service.py` (import line 38, loop lines 231-237)
- Modify: `apps/scoring_engine/tests/test_rebalance_ingestion.py` (lines ~466 and ~513)

- [ ] **Step 1: Update the tests first**

In `apps/scoring_engine/tests/test_rebalance_ingestion.py`, replace the block that builds `scored = {...}` from `record.topic == "signals.scored"` and asserts `scored == {"IBM": [...], "MSFT": [...]}` with:
```python
    assert not [record for record in store._outbox.values() if record.topic == "signals.scored"]
    assert not [record for record in store._outbox.values() if record.topic == "signals.ingested"]
```
Apply the same replacement at the second occurrence (~line 513). Keep every other assertion in those tests.

Run: `python -m pytest -q -p no:cacheprovider apps/scoring_engine/tests/test_rebalance_ingestion.py`
Expected: the edited tests fail because the producers still enqueue.

- [ ] **Step 2: Remove the producers**

1. `git rm apps/scoring_engine/scoring_engine/pipeline_events.py`
2. In `api.py`: delete `from .pipeline_events import enqueue_scoring_pipeline_events`; delete the `_emit_pipeline_events` function; change the two lines
```python
                queued_users = _dispatch_if_configured(sig, score, sector, provider_contexts)
                _emit_pipeline_events(signal=sig, score=score, queued_users=queued_users)
```
to
```python
                _dispatch_if_configured(sig, score, sector, provider_contexts)
```
3. In `rebalance_service.py`: delete `from scoring_engine.pipeline_events import enqueue_scoring_pipeline_events`; delete the `for signal, score in zip(signals, scores, strict=True): enqueue_scoring_pipeline_events(...)` loop; then run `rg -n queued_users_by_signal apps/scoring_engine/scoring_engine/services/rebalance_service.py` and, if the only remaining references are its construction and the `.append(...)` inside the leg loop, delete both (the dict existed only to label the retired event).
4. Run `ruff check apps/scoring_engine` and remove imports it reports unused (`ScoreRecord` in api.py stays if still used).

- [ ] **Step 3: Run**

Run: `python -m pytest -q -p no:cacheprovider apps/scoring_engine/tests`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add -A apps/scoring_engine/scoring_engine apps/scoring_engine/tests/test_rebalance_ingestion.py
git commit -m "refactor(scoring): stop enqueuing signals.ingested and signals.scored

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Retire `execution.results`, keep the causation link

**Files:**
- Modify: `apps/execution_engine/execution_engine/execution_persistence.py` (ctor line 58, `persist` lines 181-262, delete `_build_result_event_message` ~679-783 and `_emit_result_message_standalone` ~784-800)
- Modify: `apps/execution_engine/execution_engine/execution_log_store.py` (`log` lines 40-88)
- Modify: `apps/execution_engine/execution_engine/_services.py` (protocol lines 80-90, `OutboxStorePort` ~167 if unused)
- Modify: `apps/execution_engine/execution_engine/engine.py` (lines 46, 124, 189, 230)
- Modify: `apps/execution_engine/main.py` (lines 71, 76, 114)
- Modify: `apps/execution_engine/execution_engine/paper_order_lifecycle.py` (imports 27-28, ctor line 217, block 622-632, `_result_event` 757+)
- Modify: `apps/execution_engine/execution_engine/api.py` comment line 328
- Rewrite: `apps/execution_engine/tests/test_event_atomicity.py`
- Modify: `apps/execution_engine/tests/test_execution_persistence.py`, `apps/execution_engine/tests/test_paper_order_lifecycle.py`

- [ ] **Step 1: Rewrite the atomicity test as a causation test**

Replace the whole of `apps/execution_engine/tests/test_event_atomicity.py` with:

```python
"""Command-to-result tracing survives without the retired execution.results event."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine.execution_log_store import ExecutionLogStore
from lib_application.db.models import Base, ExecutionLog, OutboxEvent


def _session_local():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _log_kwargs(**details: Any) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "account_id": 1,
        "strategy_id": "swing_high_low_pmo_v1",
        "signal_type": "long",
        "execution_mode": "paper",
        "status": "executed",
        "details": {"orders_submitted": 1, **details},
    }


def test_log_persists_causation_event_id_and_enqueues_nothing() -> None:
    session_local = _session_local()
    store = ExecutionLogStore(session_factory=session_local)

    store.log(**_log_kwargs(causation_event_id="evt-command-1"))

    with session_local() as s:
        row = s.query(ExecutionLog).one()
        assert row.execution_details["causation_event_id"] == "evt-command-1"
        assert s.query(OutboxEvent).count() == 0


def test_log_no_longer_accepts_an_outbox_event_message() -> None:
    store = ExecutionLogStore(session_factory=_session_local())
    with pytest.raises(TypeError):
        store.log(**_log_kwargs(), outbox_store=object(), event_message={})
```

In `apps/execution_engine/tests/test_execution_persistence.py`, find the test that constructs `ExecutionPersistence(execution_log_store=log_store)` (~line 268) and calls `persistence.persist(...)`. In its `profile=` argument add the key `"_execution_user_strategy_config": {"_causation_event_id": "evt-cause-1"}` (merge into the existing profile dict), and after the existing assertions add:
```python
    with session_local() as s:
        row = s.query(ExecutionLog).one()
        assert row.execution_details["causation_event_id"] == "evt-cause-1"
```
(import `ExecutionLog` from `lib_application.db.models` if the module does not already).

Run: `python -m pytest -q -p no:cacheprovider apps/execution_engine/tests/test_event_atomicity.py apps/execution_engine/tests/test_execution_persistence.py`
Expected: the new assertions fail (`KeyError: 'causation_event_id'`; the `TypeError` test fails because the old kwargs are still accepted).

- [ ] **Step 2: Implement**

`execution_log_store.py`, `ExecutionLogStore.log`: remove the `outbox_store` and `event_message` parameters, the docstring paragraph about co-committing, and the `with self._session_factory() as session:` branch, so the tail reads:
```python
        result = self._repo.log_execution(log)
        return str(result) if result else ""
```

`_services.py`: delete the two protocol parameters `outbox_store: Any | None = None,` and `event_message: dict[str, Any] | None = None,` from the `log(...)` protocol method. Then run `rg -n OutboxStorePort apps/execution_engine`; if the only hits are its definition and `engine.py`, delete the `OutboxStorePort` protocol class and the `engine.py` import.

`execution_persistence.py`:
- Delete `outbox_store: Any | None = None,` from `__init__` and `self._outbox_store = outbox_store`.
- In `persist`, after `details["risk_baseline"] = profile.get("_risk_baseline_audit")` add:
```python
        # The inbound execution command's event_id, threaded by the API as
        # user_strategy_config["_causation_event_id"], is the trace link from
        # command to result now that no result event is published.
        details["causation_event_id"] = user_strategy_config.get("_causation_event_id")
```
- Delete the `event_message = self._build_result_event_message(...)` statement, the `event_committed_with_log` variable, the `outbox_store=`/`event_message=` arguments to `self._execution_log_store.log(...)`, the `event_committed_with_log = ...` line, and the whole "If the event was not co-committed ..." block.
- Delete `_build_result_event_message` and `_emit_result_message_standalone`.
- Run `ruff check` and remove now-unused imports (`ExecutionResultEvent`, and `ScoreContextSnapshot`, `ExecutionPolicySnapshot`, `BrokerRouteSnapshot` if nothing else in the file uses them).

`engine.py`: delete the `outbox_store: OutboxStorePort | None = None,` parameter, `self._outbox_store = outbox_store`, and `outbox_store=outbox_store,` in the `ExecutionPersistence(...)` call.

`apps/execution_engine/main.py`: delete `outbox_store = None`, `outbox_store = OutboxStore(session_factory)`, the `outbox_store=outbox_store,` argument (~line 114), and the `OutboxStore` import if nothing else in the file uses it.

`paper_order_lifecycle.py`: delete `from lib_application.outbox import OutboxStore` and `from lib_common.internal_events import ExecutionResultEvent`; delete `self._outbox_store = OutboxStore(session_factory)`; replace the block
```python
            event = self._result_event(session, pending, plan, candle)
            self._outbox_store.enqueue_on_session(
                session,
                topic=event.topic,
                ...
                ordering_key=f"broker-account:{pending.broker_account_id}",
            )
            session.commit()
```
with
```python
            # Lineage for a lifecycle fill is the canonical ledger itself:
            # orders -> order_intents.canonical_signal_id -> canonical_signals.
            session.commit()
```
and delete the `_result_event` static method. Run `ruff check` for unused imports (`CanonicalSignal`, `CanonicalOrderIntent` may become unused).

`api.py` line 328: change the comment to `# Carry the inbound command's event_id so execution_logs.execution_details records it as causation_event_id (command -> result tracing link).`

In `apps/execution_engine/tests/test_paper_order_lifecycle.py`, the partial-fill test asserts `session.query(OutboxEvent).one()` / `.count() == 2` for the lifecycle's result events; change those to `assert session.query(OutboxEvent).count() == 0` and drop the `outbox.payload["orders_filled"] == 1` line.

In `tests/test_public_strategy_pipeline_postgres_integration.py`, any `ExecutionEngine(..., outbox_store=...)` or `ExecutionPersistence(..., outbox_store=...)` keyword is removed (search `outbox_store=`).

- [ ] **Step 3: Run**

Run: `python -m pytest -q -p no:cacheprovider apps/execution_engine/tests`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add apps/execution_engine tests/test_public_strategy_pipeline_postgres_integration.py
git commit -m "refactor(execution): retire execution.results; keep causation in execution_logs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Retire `feedback.ready`

**Files:**
- Modify: `apps/feedback_loop_engine/feedback_loop_engine/engine.py` (ctor line 72/91, closure lines ~276-294, `_build_feedback_event_message` ~449-495, import)
- Modify: `apps/feedback_loop_engine/feedback_loop_engine/main.py` (lines 113, 123, import)
- Modify: `apps/feedback_loop_engine/tests/test_feedback_outbox.py`
- Modify: `tests/test_public_strategy_pipeline_postgres_integration.py` (lines ~817-827)

- [ ] **Step 1: Update the tests first**

In `apps/feedback_loop_engine/tests/test_feedback_outbox.py`:
- delete every `outbox_store=OutboxStore(session_local),` / `outbox_store=outbox_store,` constructor argument and the `outbox_store = OutboxStore(session_local)` assignment;
- replace lines 188-191 (`records = feedback_engine._outbox_store.list_pending(...)` through `assert records[0].payload["is_correct"] is True`) with:
```python
    with session_local() as s:
        assert s.query(OutboxEvent).count() == 0
```
- replace the `events = {...}` block at lines ~428-436 with:
```python
    with session_local() as s:
        assert s.query(OutboxEvent).filter(OutboxEvent.topic == "feedback.ready").count() == 0
```
- import `OutboxEvent` from `lib_application.db.models`; remove the `OutboxStore` import if unused; update the module docstring's first line to "Feedback persistence: performance rows, trackers and exact price observations."

In `tests/test_public_strategy_pipeline_postgres_integration.py`, replace the `feedback_events = session.scalars(...)` block and its two assertions with:
```python
        performance_rows = session.scalars(
            select(app_models.SignalPerformance).where(
                app_models.SignalPerformance.strategy_id.in_(strategy_ids),
            )
        ).all()
        assert len(performance_rows) == len(_STRATEGIES) * 2
        assert sum(bool(row.needs_optimization) for row in performance_rows) == len(_STRATEGIES)
        retired_rows = session.scalars(
            select(app_models.OutboxEvent).where(
                app_models.OutboxEvent.topic.in_(
                    ["feedback.ready", "execution.results", "signals.ingested", "signals.scored"]
                ),
            )
        ).all()
        assert retired_rows == []
        execution_rows = session.scalars(
            select(app_models.ExecutionLog).where(
                app_models.ExecutionLog.strategy_id.in_(strategy_ids),
            )
        ).all()
        assert execution_rows
        assert all("causation_event_id" in row.execution_details for row in execution_rows)
```

Run: `python -m pytest -q -p no:cacheprovider apps/feedback_loop_engine/tests/test_feedback_outbox.py`
Expected: fails on the zero-row assertions.

- [ ] **Step 2: Implement**

`engine.py`:
- delete `outbox_store: Any | None = None,` from `__init__` and `self._outbox_store = outbox_store`;
- delete the inner `_on_persisted(...)` closure (the one that calls `_build_feedback_event_message` and `enqueue_on_session`) and the `on_persisted=...` argument passed to `self.evaluator.persist_evaluation(...)`;
- delete `_build_feedback_event_message`;
- delete the `FeedbackEvaluationEvent` import; run `ruff check`.

`main.py` (feedback): delete `outbox_store = OutboxStore(session_local)` and `outbox_store=outbox_store,`; delete the `OutboxStore` import.

- [ ] **Step 3: Run**

Run: `python -m pytest -q -p no:cacheprovider apps/feedback_loop_engine/tests`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add apps/feedback_loop_engine tests/test_public_strategy_pipeline_postgres_integration.py
git commit -m "refactor(feedback): retire the consumer-less feedback.ready event

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Shared contracts, Compose, env example, stale-reference sweep

**Files:**
- Modify: `libs/python/lib_common/lib_common/internal_events.py`, `libs/python/lib_common/lib_common/__init__.py`
- Delete: `libs/python/lib_common/lib_common/event_bus.py`
- Modify: `docker/docker-compose.stack.yml` (lines 37, 76, 111), `.env.example` (lines 118, 148-149)
- Modify: `tests/test_outbox_store.py`, `tests/test_outbox_failure_modes.py` (neutral topic strings)

- [ ] **Step 1: Remove the event classes and the bus**

In `internal_events.py` delete the classes `CanonicalSignalEvent`, `ScoredSignalEvent`, `ExecutionResultEvent`, `FeedbackEvaluationEvent`. Keep `CanonicalSignalSnapshot`, `ScoreContextSnapshot`, `ExecutionPolicySnapshot`, `BrokerRouteSnapshot` and `ExecutionCommandEvent`.

In `lib_common/__init__.py` delete the `from lib_common.event_bus import (EventPublisher, NoOpEventPublisher,)` block, remove those two names plus `"CanonicalSignalEvent"`, `"ScoredSignalEvent"`, `"ExecutionResultEvent"`, `"FeedbackEvaluationEvent"` from the `internal_events` import list and from `__all__`.

`git rm libs/python/lib_common/lib_common/event_bus.py`.

- [ ] **Step 2: Compose and env example**

`docker/docker-compose.stack.yml`:
- delete line 37 `EVENT_BUS_PUBLISH_TOPICS: ${EVENT_BUS_PUBLISH_TOPICS:-...}`;
- line 76 becomes `SCORING_OUTBOX_TOPICS: ${SCORING_OUTBOX_TOPICS:-execution.commands,execution.rebalance.commands}` and directly below it add `SCORING_OUTBOX_NOTIFY_ENABLED: ${SCORING_OUTBOX_NOTIFY_ENABLED:-true}`;
- below line 111 `INDICATOR_MAX_STRATEGY_LAG_SECONDS: ...` add `SIGNAL_RELAY_IDLE_INTERVAL_SEC: ${SIGNAL_RELAY_IDLE_INTERVAL_SEC:-5}`.

`.env.example`:
- after `INDICATOR_MAX_STRATEGY_LAG_SECONDS=300` add:
```dotenv
# Idle recovery cadence of each strategy worker's delivery loop; ordinary
# delivery is woken by committed bars and does not wait for this.
SIGNAL_RELAY_IDLE_INTERVAL_SEC=5
```
- replace lines 148-149 with:
```dotenv
SCORING_OUTBOX_TOPICS=execution.commands,execution.rebalance.commands
# Wake the relay on the outbox_events notification; polling remains recovery.
SCORING_OUTBOX_NOTIFY_ENABLED=true
```

Validate: `docker compose --env-file .env.example -f docker/docker-compose.stack.yml config --quiet` (with a copy of `.env.example` that has `DB_PASSWORD` set if the command insists; the goal is only YAML validity).

- [ ] **Step 3: Neutralise stale topic strings in generic store tests**

In `tests/test_outbox_store.py` and `tests/test_outbox_failure_modes.py` replace every `"signals.scored"` with `"execution.commands"` and every `"execution.results"` with `"execution.rebalance.commands"`; these tests exercise the generic store and only need two distinct topic strings.

- [ ] **Step 4: Stale-reference sweep**

Run:
```bash
rg -n "signals\.ingested|signals\.scored|feedback\.ready|execution\.results|EVENT_BUS_PUBLISH_TOPICS|NoOpEventPublisher|event_bus|pipeline_events|CanonicalSignalEvent|ScoredSignalEvent|ExecutionResultEvent|FeedbackEvaluationEvent" --glob '!**/build/**' --glob '!graphify-out/**' .
```
Expected remaining hits only in: `CHANGELOG.md`, `docs/MIGRATION.md` (history), the spec and this plan, the new migration `0105_...` (Task 9), and the relay test's `signals.scored` dead-letter case. Fix anything else (docs prose in `docs/*.md` that describes the retired topics is rewritten to say they were retired).

- [ ] **Step 5: Run the broad suites**

Run: `python -m pytest -q -p no:cacheprovider libs/python/lib_common/tests tests/test_outbox_store.py tests/test_outbox_failure_modes.py apps/scoring_engine/tests apps/execution_engine/tests apps/feedback_loop_engine/tests apps/indicator_runner/tests`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A libs/python/lib_common docker/docker-compose.stack.yml .env.example tests/test_outbox_store.py tests/test_outbox_failure_modes.py
git commit -m "refactor: remove retired outbox event contracts and the no-op event bus

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Data migration for retired topic rows

**Files:**
- Create: `scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py`
- Create: `tests/test_retired_outbox_topics_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_retired_outbox_topics_migration.py`:

```python
"""Migration 0105 retires undelivered rows of the consumer-less topics idempotently."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, OutboxEvent

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0105", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(event_key: str, topic: str, status: str) -> OutboxEvent:
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    return OutboxEvent(
        topic=topic,
        event_type="Legacy",
        schema_version="v1",
        payload={},
        headers={},
        event_key=event_key,
        status=status,
        attempts=1,
        max_attempts=10,
        available_at=now,
        created_at=now,
        updated_at=now,
    )


def test_upgrade_marks_only_undelivered_retired_rows_published_and_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                _row("k-pending", "signals.scored", "pending"),
                _row("k-failed", "feedback.ready", "failed"),
                _row("k-inflight", "execution.results", "in_progress"),
                _row("k-done", "signals.ingested", "published"),
                _row("k-live", "execution.commands", "pending"),
            ]
        )
        session.commit()

    module = _load_migration()
    for _ in range(2):  # second run must change nothing further
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                module.upgrade()

    with Session(engine) as session:
        rows = {row.event_key: row for row in session.scalars(select(OutboxEvent)).all()}
    for key in ("k-pending", "k-failed", "k-inflight"):
        assert rows[key].status == "published"
        assert rows[key].claim_owner is None
        assert rows[key].delivery_metadata["publisher"] == "retired"
    assert rows["k-done"].status == "published"
    assert rows["k-done"].delivery_metadata is None
    assert rows["k-live"].status == "pending"
```

Run: `python -m pytest -q -p no:cacheprovider tests/test_retired_outbox_topics_migration.py`
Expected: fails, the migration file does not exist.

- [ ] **Step 2: Create the migration**

First check the JSON column type: `rg -n "^JSONType" libs/python/lib_application/lib_application/db/models/*.py`. If it is a `JSON().with_variant(JSONB, "postgresql")` variant, the PostgreSQL cast below is `jsonb`; if plain `JSON`, use `json`. Then create `scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py`:

```python
"""Retire the four consumer-less outbox topics.

Revision ID: 0105_retire_observational_outbox_topics
Revises: 0104_saxo_capability_flags

``signals.ingested``, ``signals.scored``, ``execution.results`` and
``feedback.ready`` were durably enqueued and handed to a no-op publisher; nothing
consumed them. Their producers are removed in the same change. Any row still
undelivered is marked published with a ``retired`` marker so no backlog is
stranded and no relay ever claims it again. Published rows stay as history.

Downgrade is a deliberate no-op: recreating producers for topics nobody consumes
has no value, and the marked rows remain readable.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0105_retire_observational_outbox_topics"
down_revision = "0104_saxo_capability_flags"
branch_labels = None
depends_on = None

_RETIRED_TOPICS = ("signals.ingested", "signals.scored", "execution.results", "feedback.ready")
_UNDELIVERED = ("pending", "failed", "in_progress")
_MARKER = json.dumps({"publisher": "retired", "revision": revision})


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Match the installed column type (json or jsonb) exactly.
        metadata_expr = "CAST(:marker AS json)"
        now_expr = "now()"
    else:
        metadata_expr = ":marker"
        now_expr = "CURRENT_TIMESTAMP"
    statement = sa.text(
        "UPDATE outbox_events "
        "SET status = 'published', "
        f"    published_at = COALESCE(published_at, {now_expr}), "
        "    claimed_at = NULL, "
        "    claim_owner = NULL, "
        f"    delivery_metadata = {metadata_expr}, "
        f"    updated_at = {now_expr} "
        "WHERE topic IN :topics AND status IN :statuses"
    ).bindparams(
        sa.bindparam("topics", expanding=True),
        sa.bindparam("statuses", expanding=True),
    )
    bind.execute(
        statement,
        {"marker": _MARKER, "topics": list(_RETIRED_TOPICS), "statuses": list(_UNDELIVERED)},
    )


def downgrade() -> None:
    """Intentional no-op: retired rows stay published and producers are not recreated."""
```

Replace `json` in `CAST(:marker AS json)` with `jsonb` if the column check above showed a JSONB variant.

- [ ] **Step 3: Run**

Run: `python -m pytest -q -p no:cacheprovider tests/test_retired_outbox_topics_migration.py`
Expected: pass. Also run any existing migration-chain test that checks a single head: `python -m pytest -q -p no:cacheprovider tests -k "alembic or migration_head or drift" --co -q` to list candidates, then run them.

- [ ] **Step 4: Commit**

```bash
git add scripts/db/alembic/versions/0105_retire_observational_outbox_topics.py tests/test_retired_outbox_topics_migration.py
git commit -m "feat(db): retire undelivered rows of the consumer-less outbox topics

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Documentation, changelog and spec amendments

**Files:**
- Modify: `docs/CONFIGURATION.md` (section "Snapshots, parsing, and readiness"), `docs/E2E_VERIFICATION_GUIDE.md` (metric list ~line 150), `docs/DATABASE.md` (migration head mention if it names `0104`), `CHANGELOG.md`, `docs/superpowers/specs/2026-09-05-event-driven-delivery-bounded-pools-design.md`

- [ ] **Step 1: CONFIGURATION.md**

In "Snapshots, parsing, and readiness", after the paragraph beginning "Use lib_common.env_utils", add:

```markdown
Connection budget. Every strategy worker inherits a fixed pool of two
connections with no overflow plus one raw LISTEN connection; the indicator
supervisor uses the same pool. Other children keep DB_POOL_SIZE plus
DB_MAX_OVERFLOW (five by default). The configured allowance is therefore
3 x strategies + 2 + 5 x other children + 1 for the scoring notification
listener: twenty strategies with the default six other children reach 93 of
PostgreSQL's default max_connections of 100 (three are reserved for the
superuser). Beyond that, raise max_connections through the postgres service
command in docker/docker-compose.stack.yml; each extra strategy adds three and
each optional equity or calendar worker adds five.

Delivery cadence. SCORING_OUTBOX_NOTIFY_ENABLED (default true) wakes the
scoring relay on the outbox_events notification while SCORING_OUTBOX_POLL_SEC
remains the recovery poll. SIGNAL_RELAY_IDLE_INTERVAL_SEC (default 5) is the
recovery cadence of each strategy worker's delivery loop; committed bars wake
the loop directly. The relay publishes only execution.commands and
execution.rebalance.commands; the former signals.ingested, signals.scored,
execution.results and feedback.ready topics were retired with their producers.
```

- [ ] **Step 2: E2E guide and DATABASE.md**

In `docs/E2E_VERIFICATION_GUIDE.md`, after `- vm_outbox_oldest_undelivered_age_seconds` add `- vm_scoring_outbox_notify_listener_up`.

Run `grep -n "0104" docs/DATABASE.md`; if it names `0104_saxo_capability_flags` as the head, change it to `0105_retire_observational_outbox_topics`.

- [ ] **Step 3: CHANGELOG**

Under `## [Unreleased]`, add as the first bullets:
```markdown
- Strategy signal delivery runs on a dedicated loop outside the bar-processing
  lock, woken by each committed transition, and the scoring outbox relay wakes on
  the existing `outbox_events` notification (`SCORING_OUTBOX_NOTIFY_ENABLED`).
- Strategy worker subprocesses use a fixed two-connection pool with no overflow;
  `SIGNAL_RELAY_IDLE_INTERVAL_SEC` sets the delivery loop's recovery cadence.
- Retired the consumer-less outbox topics `signals.ingested`, `signals.scored`,
  `execution.results` and `feedback.ready` with their producers; migration `0105`
  marks any undelivered rows published. `execution_logs.execution_details` now
  carries `causation_event_id`. `EVENT_BUS_PUBLISH_TOPICS` is removed.
```

- [ ] **Step 4: Spec amendments (record what implementation found)**

In the spec:
- Section 6, replace "and the paper lifecycle writes the same key with the canonical signal's `external_signal_id`" with "; the paper lifecycle writes no `execution_logs` row, so its trace remains the canonical ledger lineage from `orders` through `order_intents.canonical_signal_id`".
- Section 8, the unit-test bullet ending "on both fill paths" becomes "on the immediate path; lifecycle fills are traced through the ledger".
- Section 8, integration paragraph: replace "spawns twenty `signal_worker` processes from copies of the `PipelineExerciser` fixture" with "runs twenty `SignalWorker` instances in one test process, each with its own engine, delivery loop and LISTEN connection, against a probe strategy"; and in item 5 replace "`SIGTERM` during a drain exits zero" with "stopping every worker during a drain returns within the stop timeout".

- [ ] **Step 5: Commit**

```bash
git add docs/CONFIGURATION.md docs/E2E_VERIFICATION_GUIDE.md docs/DATABASE.md CHANGELOG.md docs/superpowers/specs/2026-09-05-event-driven-delivery-bounded-pools-design.md
git commit -m "docs: connection budget, delivery cadence, retired topics

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Twenty-worker PostgreSQL acceptance module

**Files:**
- Create: `apps/indicator_runner/tests/test_multi_worker_delivery_postgres_integration.py`

This module is opt-in like the other PostgreSQL integration tests (skips without `DATABASE_URL`). Run it against the isolated test database used earlier in this repository's verification, never against `vm_trading`.

- [ ] **Step 1: Write the module**

```python
"""Twenty strategy workers waking on one committed bar batch (PostgreSQL, opt-in).

Proves, on a real database and a real LISTEN/NOTIFY channel, that the delivery
loop keeps connections bounded, delivers every committed signal to a stub
scoring endpoint, preserves per-worker order, does not duplicate under a repeated
notification or a reclaimed lease, recovers after a scoring outage, and stops
cleanly mid-drain. Latency from row commit to stub receipt is recorded, not
promised.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text

from indicator_runner.runtime_journal import (
    BufferedSignalEmitter,
    DurableSignalRelay,
    StrategyRuntimeIdentity,
    StrategyRuntimeStore,
)
from indicator_runner.signal_delivery import SignalDeliveryLoop
from indicator_runner.signal_worker import SignalWorker
from lib_application.db.models import (
    ConsumerWatermark,
    Instrument,
    InstrumentAlias,
    OutboxEvent,
    Strategy,
    StrategyDecision,
    StrategyRuntimeState,
    User,
)
from lib_application.db.session import create_engine_for_env, dispose_engine, get_session_factory
from lib_application.outbox import OutboxStore
from lib_application.services.price_ingestion_service import PriceIngestionService
from lib_data.market_data import CandleRow
from lib_infrastructure.market_data.coinbase_client import CoinbaseCandleClient
from lib_strategy.signals.emitter import HttpSignalEmitter
from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy
from lib_strategy.signals.signal import SignalAction
from lib_strategy.signals.utils import compute_external_signal_id, extract_price_provenance

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "tests/fixtures/market_data/coinbase_btcusdc_1m_integration_2026-06-10.json"
_SOURCE = CoinbaseCandleClient.source
_SYMBOL = "BTCUSDC"
_PRODUCT_ID = "BTC-USDC"
_WORKERS = 20
_WORKER_PREFIX = "multiworker-probe"
_STRATEGY_PREFIX = "multiworker_probe"
_OWNER_ID = "multiworker-probe-owner"
_WAIT = 20.0


# ---------------------------------------------------------------------------
# Stub scoring endpoint
# ---------------------------------------------------------------------------


class _StubScoring:
    """Accepts /api/v1/signals, records arrivals, can fail or slow down on demand."""

    def __init__(self) -> None:
        self.received: list[tuple[float, str, str]] = []  # (monotonic, worker_id, external id)
        self.fail = threading.Event()
        self.delay_seconds = 0.0
        self._lock = threading.Lock()
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if stub.delay_seconds:
                    time.sleep(stub.delay_seconds)
                if stub.fail.is_set():
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{"status":"unavailable"}')
                    return
                context = body.get("context") or {}
                with stub._lock:
                    stub.received.append(
                        (
                            time.monotonic(),
                            str(body.get("strategy_id")),
                            str(context.get("external_signal_id")),
                        )
                    )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, _format: str, *_args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def ids_for(self, strategy_id: str) -> list[str]:
        with self._lock:
            return [ext for _, sid, ext in self.received if sid == strategy_id]


# ---------------------------------------------------------------------------
# Probe strategy: one LONG per scheduled bar close
# ---------------------------------------------------------------------------


class _ProbeStrategy(PureSignalStrategy):
    def __init__(self, strategy_id: str, entry_timestamps: set[datetime]) -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_type="indicator",
            config={"strategy_version": "1.0.0"},
        )
        self._entries = entry_timestamps

    @property
    def warmup_bars_needed(self) -> int:
        return 1

    def initialize(self) -> None:
        return None

    def on_data(self, state: MarketState) -> None:
        if state.timestamp not in self._entries:
            return
        ext_id = compute_external_signal_id(
            strategy_id=self.strategy_id,
            symbol=state.symbol,
            action=SignalAction.LONG,
            bar_close_ts=state.timestamp,
            strategy_version="1.0.0",
            reason="probe_entry",
        )
        self.emit_long(
            symbol=state.symbol,
            entry_price=state.close,
            confidence=0.7,
            timestamp=state.timestamp,
            horizon="1H",
            stop_loss=state.close * 0.994,
            take_profit=state.close * 1.004,
            external_signal_id=ext_id,
            expires_at=state.timestamp + timedelta(seconds=60),
            strategy_version="1.0.0",
            metadata={"external_signal_id": ext_id, **extract_price_provenance(state.metadata)},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_rows(instr_id: int) -> list[CandleRow]:
    fixture: dict[str, Any] = json.loads(_FIXTURE.read_text())
    rows = CoinbaseCandleClient.normalize_candles(
        instr_id=instr_id, product_id=_PRODUCT_ID, candles=fixture["candles"], timeframe="1m"
    )
    assert len(rows) == 11
    return sorted(rows, key=lambda row: row.ts)


def _wait_until(predicate: Any, *, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    msg = f"Timed out waiting for {what}"
    raise AssertionError(msg)


def _connections(session_factory: Any) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
            ).scalar_one()
        )


def _notify(session_factory: Any, latest_ts: datetime) -> None:
    payload = json.dumps(
        {"symbol": _SYMBOL, "timeframe": "1m", "source": _SOURCE, "latest_ts": latest_ts.isoformat()}
    )
    with session_factory() as session:
        session.execute(text("NOTIFY new_market_data, :payload"), {"payload": payload})
        session.commit()


def _provision(session_factory: Any) -> int:
    with session_factory() as session:
        if session.get(User, _OWNER_ID) is None:
            session.add(
                User(
                    user_id=_OWNER_ID,
                    email="multiworker@example.invalid",
                    base_ccy="USD",
                    is_deployment_owner=True,
                )
            )
        instrument = session.scalars(
            select(Instrument).where(Instrument.canonical == _SYMBOL)
        ).one_or_none()
        if instrument is not None:
            pytest.skip("Refusing to reuse an existing BTCUSDC catalogue row; use an isolated database")
        instrument = Instrument(
            asset_class="crypto",
            canonical=_SYMBOL,
            settlement_currency="USD",
            is_tradable=True,
            market_session_policy="continuous",
        )
        session.add(instrument)
        session.flush()
        session.add(InstrumentAlias(instr_id=instrument.instr_id, alias=_SYMBOL, source="canonical"))
        for index in range(_WORKERS):
            session.add(
                Strategy(strategy_id=f"{_STRATEGY_PREFIX}_{index:02d}", strategy_name=f"Probe {index:02d}")
            )
        session.commit()
        return int(instrument.instr_id)


def _cleanup(session_factory: Any, instr_id: int | None) -> None:
    with session_factory() as session:
        for index in range(_WORKERS):
            worker_id = f"{_WORKER_PREFIX}-{index:02d}"
            session.execute(delete(StrategyDecision).where(StrategyDecision.worker_id == worker_id))
            session.execute(
                delete(StrategyRuntimeState).where(StrategyRuntimeState.worker_id == worker_id)
            )
            session.execute(delete(OutboxEvent).where(OutboxEvent.ordering_key == worker_id))
            session.execute(
                delete(ConsumerWatermark).where(ConsumerWatermark.worker_id == worker_id)
            )
        if instr_id is not None:
            session.execute(text("DELETE FROM prices WHERE instr_id = :i"), {"i": instr_id})
            session.execute(delete(InstrumentAlias).where(InstrumentAlias.instr_id == instr_id))
            session.execute(delete(Instrument).where(Instrument.instr_id == instr_id))
        session.execute(delete(Strategy).where(Strategy.strategy_id.like(f"{_STRATEGY_PREFIX}_%")))
        session.execute(delete(User).where(User.user_id == _OWNER_ID))
        session.commit()


class _Fleet:
    """Twenty workers, each with its own engine, relay, delivery loop and LISTEN connection."""

    def __init__(self, dsn: str, stub_url: str, entries: set[datetime]) -> None:
        self.engines: list[Any] = []
        self.workers: list[SignalWorker] = []
        self.loops: list[SignalDeliveryLoop] = []
        for index in range(_WORKERS):
            strategy_id = f"{_STRATEGY_PREFIX}_{index:02d}"
            worker_id = f"{_WORKER_PREFIX}-{index:02d}"
            engine = create_engine_for_env(db_url=dsn)
            factory = get_session_factory(engine=engine)
            buffer = BufferedSignalEmitter()
            strategy = _ProbeStrategy(strategy_id, entries)
            strategy._emitter = buffer
            identity = StrategyRuntimeIdentity.from_strategy(
                worker_id=worker_id,
                strategy=strategy,
                symbols=[_SYMBOL],
                source=_SOURCE,
                timeframe="1m",
                consolidation_minutes=0,
            )
            relay = DurableSignalRelay(
                outbox=OutboxStore(factory),
                emitter=HttpSignalEmitter(base_url=stub_url, max_retries=0),
                worker_id=worker_id,
                strategy_id=strategy_id,
                ordering_key=identity.ordering_key,
                retry_base_seconds=1,
            )
            loop = SignalDeliveryLoop(
                run_once=relay.run_once, worker_id=worker_id, idle_interval_seconds=1.0
            )
            worker = SignalWorker(
                strategy=strategy,
                session_factory=factory,
                worker_id=worker_id,
                symbols=[_SYMBOL],
                consolidation_minutes=0,
                dsn=dsn,
                bootstrap_bars=3,
                source=_SOURCE,
                timeframe="1m",
                signal_buffer=buffer,
                runtime_store=StrategyRuntimeStore(factory, identity),
                signal_relay=relay,
                delivery_loop=loop,
            )
            self.engines.append(engine)
            self.workers.append(worker)
            self.loops.append(loop)

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    def stop(self) -> float:
        started = time.monotonic()
        for worker in self.workers:
            worker.stop()
        elapsed = time.monotonic() - started
        for engine in self.engines:
            dispose_engine(engine)
        return elapsed


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_twenty_workers_wake_together_with_bounded_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set; skipping PostgreSQL integration")
    # The launcher gives strategy workers this pool; mirror it for every engine below.
    monkeypatch.setenv("DB_POOL_SIZE", "2")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_CONNECTION_BUDGET", "2")

    control_engine = create_engine_for_env(db_url=dsn)
    control = get_session_factory(engine=control_engine)
    ingestion = PriceIngestionService(control)
    stub = _StubScoring()
    stub.start()
    instr_id: int | None = None
    fleet: _Fleet | None = None
    try:
        instr_id = _provision(control)
        rows = _fixture_rows(instr_id)
        # Bars 0-2 warm the workers; 3, 5 and 6 carry probe entries; 7 is the outage bar.
        entries = {rows[i].ts.replace(tzinfo=UTC) for i in (3, 5, 6, 7)}
        ingestion.upsert_candles(rows[:3])

        baseline = _connections(control)
        fleet = _Fleet(dsn, stub.url, entries)
        fleet.start()
        _wait_until(lambda: all(w._listener_thread and w._listener_thread.is_alive() for w in fleet.workers), what="LISTEN threads")

        # 1. Connections stay within 3 per worker (2 pooled + 1 LISTEN).
        _wait_until(lambda: _connections(control) - baseline <= _WORKERS * 3, what="bounded connections", timeout=5.0)
        assert _connections(control) - baseline <= _WORKERS * 3

        # 2. One committed bar + one NOTIFY -> twenty deliveries; latency recorded.
        committed_at = time.monotonic()
        ingestion.upsert_candles([rows[3]])
        _notify(control, rows[3].ts.replace(tzinfo=UTC))
        _wait_until(lambda: len(stub.received) >= _WORKERS, what="first wave delivery")
        first_wave = [t - committed_at for t, _, _ in stub.received[:_WORKERS]]
        assert len({ext for _, _, ext in stub.received}) == _WORKERS
        print(
            f"delivery latency s: p50={statistics.median(first_wave):.3f} "
            f"max={max(first_wave):.3f} n={len(first_wave)}"
        )
        with control() as session:
            statuses = session.execute(
                text(
                    "SELECT status, count(*) FROM outbox_events "
                    "WHERE topic = 'signals.submit' AND ordering_key LIKE :prefix GROUP BY status"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%"},
            ).all()
        assert dict(statuses) == {"published": _WORKERS}

        # 3. A repeated NOTIFY without a new bar delivers nothing new.
        _notify(control, rows[3].ts.replace(tzinfo=UTC))
        time.sleep(2.5)
        assert len(stub.received) == _WORKERS

        # 4. A reclaimed lease redelivers exactly that row and it ends published again.
        with control() as session:
            target = session.execute(
                text(
                    "UPDATE outbox_events SET status = 'in_progress', claim_owner = 'dead-worker', "
                    "claimed_at = now() - interval '10 minutes', published_at = NULL "
                    "WHERE topic = 'signals.submit' AND ordering_key = :key RETURNING event_id, attempts"
                ),
                {"key": f"{_WORKER_PREFIX}-00"},
            ).one()
            session.commit()
        _wait_until(lambda: len(stub.received) == _WORKERS + 1, what="lease reclaim redelivery")
        with control() as session:
            reclaimed = session.execute(
                text("SELECT status, attempts FROM outbox_events WHERE event_id = :id"),
                {"id": target.event_id},
            ).one()
        assert reclaimed.status == "published"
        assert reclaimed.attempts == target.attempts + 1

        # 5. Two more bars: per-worker order follows bar order.
        ingestion.upsert_candles([rows[4], rows[5], rows[6]])
        _notify(control, rows[6].ts.replace(tzinfo=UTC))
        _wait_until(lambda: len(stub.received) >= _WORKERS * 3 + 1, what="second and third wave")
        for index in range(_WORKERS):
            strategy_id = f"{_STRATEGY_PREFIX}_{index:02d}"
            ids = stub.ids_for(strategy_id)
            expected = [
                compute_external_signal_id(
                    strategy_id=strategy_id,
                    symbol=_SYMBOL,
                    action=SignalAction.LONG,
                    bar_close_ts=rows[i].ts.replace(tzinfo=UTC),
                    strategy_version="1.0.0",
                    reason="probe_entry",
                )
                for i in (3, 5, 6)
            ]
            # The reclaimed row of worker 00 appears twice; order of first appearances holds.
            first_seen = list(dict.fromkeys(ids))
            assert first_seen == expected, strategy_id

        # 6. Outage: deliveries fail, back off, then recover through the idle pass.
        stub.fail.set()
        ingestion.upsert_candles([rows[7]])
        _notify(control, rows[7].ts.replace(tzinfo=UTC))
        _wait_until(
            lambda: _count(control, "failed") == _WORKERS, what="failed rows during outage"
        )
        stub.fail.clear()
        _wait_until(lambda: _count(control, "published") == _WORKERS * 4, what="recovery after outage")

        # 7. Stop every worker while a slow drain is in flight.
        stub.delay_seconds = 0.5
        ingestion.upsert_candles(rows[8:])
        _notify(control, rows[-1].ts.replace(tzinfo=UTC))
        time.sleep(0.2)
        elapsed = fleet.stop()
        fleet = None
        assert elapsed < _WORKERS * 15.0
        with control() as session:
            stale = session.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE topic = 'signals.submit' "
                    "AND ordering_key LIKE :prefix AND status = 'in_progress' "
                    "AND claimed_at < now() - interval '60 seconds'"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%"},
            ).scalar_one()
        assert stale == 0
    finally:
        if fleet is not None:
            fleet.stop()
        stub.stop()
        _cleanup(control, instr_id)
        dispose_engine(control_engine)


def _count(session_factory: Any, status: str) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE topic = 'signals.submit' "
                    "AND ordering_key LIKE :prefix AND status = :status"
                ),
                {"prefix": f"{_WORKER_PREFIX}-%", "status": status},
            ).scalar_one()
        )
```

- [ ] **Step 2: Run it against the isolated test database**

The repository's earlier verification created database `vm_pipeline_test` on the isolated PostgreSQL published at `127.0.0.1:55432`; reuse it (never `vm_trading`). Derive the URL from the private `.env` without printing secrets:

```bash
export DATABASE_URL="$(grep '^MIGRATION_DATABASE_URL=' .env | cut -d= -f2- | sed 's#@postgres:5432/vm_trading#@127.0.0.1:55432/vm_pipeline_test#')"
export PYTHONPATH=libs/python/lib_common:libs/python/lib_strategy:libs/python/lib_indicators:libs/python/lib_data:libs/python/lib_application:libs/python/lib_infrastructure:tools/dev_cli:apps/execution_engine:apps/scoring_engine:apps/feedback_loop_engine:apps/indicator_runner:apps/market_data_ingestor
alembic -c scripts/db/alembic.ini upgrade head
python -m pytest -q -p no:cacheprovider -m integration -s apps/indicator_runner/tests/test_multi_worker_delivery_postgres_integration.py
```
Expected: 1 passed, with a printed `delivery latency s: p50=... max=... n=20` line. If `_provision` skips because a `BTCUSDC` instrument already exists in that database, delete it only if it was created by an earlier run of this exact test (check `instrument_aliases.source = 'canonical'` and no `prices` rows outside this fixture's day); otherwise create a fresh database with `CREATE DATABASE vm_multiworker_test OWNER trader` and repeat.

- [ ] **Step 3: Commit**

```bash
git add apps/indicator_runner/tests/test_multi_worker_delivery_postgres_integration.py
git commit -m "test(indicator): twenty-worker delivery acceptance on PostgreSQL

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Full validation and paper-stack acceptance

**Files:** none new

- [ ] **Step 1: Repository gates**

```bash
ruff check . && ruff format --check .
vmdev audit --strict
vmdev test all
```
Expected: audit passes; `vmdev test all` reports all passed with the usual opt-in skips. Fix anything that fails before continuing; do not widen a gate.

- [ ] **Step 2: Rebuild the image and redeploy the isolated paper stack**

```bash
vmdev build libs && vmdev build strategies && vmdev build venvs
vmdev build docker --from-config --tag latest
docker compose --env-file .env -f docker/docker-compose.stack.yml config --quiet
```
Then apply migration `0105` and recreate both runtime groups through the supported lifecycle:
```bash
vmdev db bootstrap --owner-config owner.local.yaml
vmdev db status
```
Expected: bootstrap completes (it applies `0105` and restarts `application` and `workers`); three containers healthy.

- [ ] **Step 3: Measure**

```bash
S=/private/tmp/claude-501/-Users-virendrayadav-workspace-VisionMaverick-vynmatrix/3034a263-084a-42a7-a4a0-50a7c5a8f5cc/scratchpad
$S/status.sh application /metrics/scoring | grep -E '^vm_scoring_outbox_(relay|notify_listener)_up|^vm_outbox_oldest_undelivered_age_seconds'
$S/status.sh workers /metrics/indicator | grep -E '^vm_indicator_(signal_backlog_oldest_seconds|strategy_lag_seconds)'
$S/psql.sh "SELECT usename, count(*) FROM pg_stat_activity WHERE datname='vm_trading' GROUP BY 1 ORDER BY 1"
$S/psql.sh "SELECT topic, status, count(*) FROM outbox_events GROUP BY 1,2 ORDER BY 1,2"
docker compose --env-file .env -f docker/docker-compose.stack.yml logs --no-log-prefix --since 10m application workers | grep -cE '"level": ?"(ERROR|CRITICAL)"'
```
Expected: `vm_scoring_outbox_notify_listener_up 1.0`; `vm_indicator_login` connection count at most 3 per running strategy plus 2; the four retired topics show only `published` rows; zero errors. Record these numbers for the pull request body.

- [ ] **Step 4: Update the verification record**

Append one paragraph to the "Local paper end-to-end run and defect fixes (2026-09-05)" section of `docs/MIGRATION.md` stating what was measured in Step 3 (listener gauge, connection counts per login, error count), then commit:
```bash
git add docs/MIGRATION.md
git commit -m "docs: record event-driven delivery acceptance on the paper stack

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Push and open the pull request

**Files:** none

- [ ] **Step 1: Confirm identity and push the branch**

```bash
git log --format='%an <%ae>' origin/main..HEAD | sort -u
gh auth status 2>&1 | grep -E 'Active account|Logged in'
git push -u origin feat/event-driven-delivery-bounded-pools
```
Expected: every commit authored as `vynaptic <325089101+vynaptic@users.noreply.github.com>`; the active `gh` account is `vynaptic`. Direct pushes to `main` are refused by branch protection (required `ci-gate` check, admin enforcement), which is why the branch is pushed instead.

- [ ] **Step 2: Open the pull request into `main`**

```bash
gh pr create --base main --head feat/event-driven-delivery-bounded-pools \
  --title "feat: event-driven signal delivery and bounded strategy pools" \
  --body-file - <<'EOF'
Implements docs/superpowers/specs/2026-09-05-event-driven-delivery-bounded-pools-design.md.

- Strategy worker subprocesses run with a fixed 2/0 connection pool set on the indicator spec.
- The scoring outbox relay wakes on the existing `outbox_events` notification (`SCORING_OUTBOX_NOTIFY_ENABLED`, default true) with a `vm_scoring_outbox_notify_listener_up` gauge; polling remains the recovery path.
- Signal workers deliver through a dedicated `SignalDeliveryLoop` thread woken after each committed bar or panel transition; no HTTP runs under the processing lock. `SIGNAL_RELAY_IDLE_INTERVAL_SEC` (default 5) sets the recovery cadence.
- `signals.ingested`, `signals.scored`, `execution.results` and `feedback.ready` are retired with their producers; migration `0105` marks undelivered rows published; `execution_logs.execution_details.causation_event_id` keeps the command-to-result trace.

Verification: `ruff`, `vmdev audit --strict`, `vmdev test all`, the new twenty-worker PostgreSQL acceptance module, and a redeploy of the isolated paper stack (numbers in docs/MIGRATION.md).

No topology, grant, authority, freshness or risk logic changed. `EXECUTION_MODE=paper` and `EXECUTION_ENGINE_ALLOW_LIVE=false` throughout.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

- [ ] **Step 3: Watch the required check**

```bash
gh pr checks --watch
```
Expected: `ci-gate` succeeds. If it fails, read the failing job with `gh run view --log-failed`, fix on the branch, commit, push, and re-watch. Merging is the owner's decision; report the PR URL and check state.
