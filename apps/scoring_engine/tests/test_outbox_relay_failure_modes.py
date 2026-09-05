"""Failure-mode tests for ``OutboxRelayWorker``.

Complements ``test_outbox_dispatch.py`` (happy-path delivery) by covering
the recovery semantics when the downstream execution engine returns
errors:

* HTTP 5xx → caught and retried by ``retry_async`` inside
  ``_deliver_execution_command``; if all retries fail, the row is
  marked ``failed`` with status flipping to ``dead_letter`` once
  ``attempts >= max_attempts``,
* HTTP 4xx → not retried (the wrapper raises ``RuntimeError`` which is
  not in the ``retry_on`` whitelist), marked ``failed`` immediately,
* a row already in ``dead_letter`` is invisible to the next ``drain_once``.
"""

from __future__ import annotations

import asyncio

import httpx

from scoring_engine.outbox_relay import OutboxRelayWorker
from scoring_engine.storage import InMemoryScoreStore


def _seed_pending_command(
    store: InMemoryScoreStore,
    *,
    signal_id: str,
    max_attempts: int = 1,
) -> str:
    """Push a pending ``execution.commands`` row into the outbox.

    Uses ``enqueue_event`` directly (rather than dispatcher.dispatch) so the
    test owns ``max_attempts``; the dispatcher uses a longer production budget.
    """
    return store.enqueue_event(
        topic="execution.commands",
        event_type="ExecutionCommand",
        payload={
            "signal_id": signal_id,
            "user_id": "user-relay-fail",
            "binding_id": 17,
            "execution_mode": "spot",
            "run_id": f"run-{signal_id}",
        },
        schema_version="v1",
        aggregate_type="execution_command",
        aggregate_id=f"{signal_id}:user-relay-fail",
        event_key=f"execution-command:{signal_id}:user-relay-fail",
        ordering_key="user-relay-fail",
        headers={"run_id": f"run-{signal_id}"},
        max_attempts=max_attempts,
    )


def _row_state(store: InMemoryScoreStore, event_id: str) -> tuple[str, int]:
    """Return ``(status, attempts)`` for the given outbox row."""
    record = store._outbox.get(event_id)  # type: ignore[attr-defined]
    assert record is not None, f"outbox row {event_id!r} disappeared"
    return str(record.status), int(record.attempts)


def _drain(store: InMemoryScoreStore, *, transport: httpx.MockTransport) -> int:
    async def _go() -> int:
        worker = OutboxRelayWorker(
            store=store,
            exec_engine_url="http://execution.test",
            http_client=httpx.AsyncClient(transport=transport),
        )
        try:
            return await worker.drain_once(topics=["execution.commands"])
        finally:
            await worker.aclose()

    return asyncio.run(_go())


def test_relay_marks_failed_on_http_5xx_and_dead_letters_at_max_attempts() -> None:
    """A 5xx response retries, then DLQs once ``attempts >= max_attempts``."""
    store = InMemoryScoreStore()
    event_id = _seed_pending_command(store, signal_id="sig-5xx", max_attempts=1)

    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "upstream"}, request=request)

    processed = _drain(store, transport=httpx.MockTransport(_handler))

    # Delivery never succeeds; ``processed`` reflects published-only count.
    assert processed == 0
    # ``retry_async`` retries 2 + 1 initial = 3 calls per drain attempt.
    assert len(calls) == 3
    status, attempts = _row_state(store, event_id)
    # max_attempts=1 → first failure flips to dead_letter immediately.
    assert status == "dead_letter"
    assert attempts == 1


def test_relay_marks_failed_on_http_4xx_without_retrying() -> None:
    """A 4xx response is treated as a permanent rejection — no retry storm."""
    store = InMemoryScoreStore()
    event_id = _seed_pending_command(store, signal_id="sig-4xx", max_attempts=3)

    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(422, json={"error": "validation"}, request=request)

    processed = _drain(store, transport=httpx.MockTransport(_handler))
    assert processed == 0
    # ``RuntimeError`` falls outside ``retry_on``, so a single attempt is made.
    assert len(calls) == 1
    status, attempts = _row_state(store, event_id)
    # Schema/contract failures are permanent and enter the DLQ immediately;
    # retrying the same invalid economic command cannot make it valid.
    assert status == "dead_letter"
    assert attempts == 1


def test_relay_recovers_when_5xx_clears_within_max_attempts() -> None:
    """Two transient 5xxs followed by a 200 publishes successfully."""
    store = InMemoryScoreStore()
    event_id = _seed_pending_command(store, signal_id="sig-recover", max_attempts=5)

    state: dict[str, int] = {"calls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(502, json={"error": "bad gateway"}, request=request)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    processed = _drain(store, transport=httpx.MockTransport(_handler))
    assert processed == 1
    assert state["calls"] == 3  # 2 retries within retry_async + 1 success
    status, _ = _row_state(store, event_id)
    assert status == "published"


def test_dead_letter_row_is_skipped_by_subsequent_drain() -> None:
    """A row already in DLQ stays there; ``drain_once`` does not re-claim it."""
    store = InMemoryScoreStore()
    event_id = _seed_pending_command(store, signal_id="sig-dlq-skip", max_attempts=1)

    def _handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"}, request=request)

    # First drain: DLQs the row.
    _drain(store, transport=httpx.MockTransport(_handler_500))
    status, _ = _row_state(store, event_id)
    assert status == "dead_letter"

    # Second drain on a clean handler — should not even hit the handler.
    calls: list[int] = []

    def _handler_200(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    processed = _drain(store, transport=httpx.MockTransport(_handler_200))
    assert processed == 0
    assert calls == []  # Dead-letter row is filtered out of the claim batch.
