"""Durable scoring-outbox delivery orchestration."""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from lib_application.outbox import OutboxBacklogSnapshot, OutboxRecord, backlog_age_seconds
from lib_common.logging import get_logger
from lib_common.metrics import counter, gauge
from lib_common.retries import retry_async
from lib_data.pg_notify import PgNotifyListener

from .storage import ScoreStore

logger = get_logger(__name__)

_OUTBOX_DELIVERED = counter(
    "vm_outbox_delivered_total",
    "Outbox relay delivery outcomes",
    ("outcome",),
)
_OUTBOX_BACKLOG = gauge(
    "vm_outbox_backlog",
    "Undelivered outbox events by status",
    ("status",),
)
_OUTBOX_OLDEST_AGE = gauge(
    "vm_outbox_oldest_undelivered_age_seconds",
    "Wall-clock age of the oldest undelivered outbox event",
    ("topics",),
)
_OUTBOX_PROGRESS_READY = gauge(
    "vm_outbox_progress_ready",
    "1 when the required outbox topics have no dead letter or stale event",
    ("topics",),
)
_NOTIFY_LISTENER_UP = gauge(
    "vm_scoring_outbox_notify_listener_up",
    "1 while the outbox_events LISTEN thread is alive, else 0",
)
_BACKLOG_STATUSES = ("pending", "in_progress", "failed", "dead_letter")

_HTTP_SERVER_ERROR_STATUS = 500
_HTTP_CLIENT_ERROR_STATUS = 400
_TRANSIENT_HTTP_STATUSES = frozenset({408, 409, 425, 429})

# Exponential failure backoff capped at five minutes, with bounded jitter to
# spread retries after a downstream outage.
_OUTBOX_FAILURE_BACKOFF_BASE_SECONDS = 30
_OUTBOX_FAILURE_BACKOFF_CAP_SECONDS = 300
_OUTBOX_FAILURE_JITTER_RATIO = 0.2


class OutboxDeliveryError(RuntimeError):
    """Delivery failure with an explicit retry classification."""

    def __init__(self, message: str, *, failure_class: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def _failure_class(exc: Exception) -> str:
    if isinstance(exc, OutboxDeliveryError):
        return exc.failure_class
    if isinstance(exc, (httpx.RequestError, httpx.HTTPStatusError, OSError)):
        return "transient"
    if isinstance(exc, (TypeError, ValueError)):
        return "permanent"
    return "transient"


def _failure_backoff_seconds(attempts: int) -> int:
    """Return the bounded, jittered delay for a failed delivery."""
    base = min(
        _OUTBOX_FAILURE_BACKOFF_CAP_SECONDS,
        _OUTBOX_FAILURE_BACKOFF_BASE_SECONDS * max(1, attempts),
    )
    jitter = random.uniform(
        1 - _OUTBOX_FAILURE_JITTER_RATIO,
        1 + _OUTBOX_FAILURE_JITTER_RATIO,
    )
    return max(1, int(base * jitter))


def outbox_progress_ready(
    store: ScoreStore,
    *,
    topics: Iterable[str],
    max_backlog_age_seconds: int,
) -> bool:
    """Fail closed when a required outbox partition has stopped advancing."""
    topic_list = sorted({str(topic).strip() for topic in topics if str(topic).strip()})
    if not topic_list:
        return False
    topic_label = ",".join(topic_list)
    try:
        counts = store.outbox_backlog_counts(topics=topic_list)
        oldest_created_at = store.oldest_outbox_undelivered_created_at(
            topics=topic_list,
        )
    except (OSError, SQLAlchemyError):
        if _OUTBOX_PROGRESS_READY is not None:
            _OUTBOX_PROGRESS_READY.labels(topics=topic_label).set(0)
        return False

    snapshot = OutboxBacklogSnapshot(
        counts=counts,
        oldest_age_seconds=backlog_age_seconds(oldest_created_at, now=datetime.now(tz=UTC)),
    )
    ready = snapshot.is_current(max_backlog_age_seconds)
    if _OUTBOX_OLDEST_AGE is not None:
        _OUTBOX_OLDEST_AGE.labels(topics=topic_label).set(snapshot.oldest_age_seconds)
    if _OUTBOX_PROGRESS_READY is not None:
        _OUTBOX_PROGRESS_READY.labels(topics=topic_label).set(1 if ready else 0)
    return ready


class OutboxRelayWorker:
    """Claim scoring outbox messages and deliver them to downstream consumers.

    PostgreSQL notifications shorten delivery latency when configured. Polling
    remains the recovery mechanism for reconnects and missed notifications.
    """

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

    async def drain_once(
        self,
        *,
        topics: Iterable[str],
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> int:
        # The sync store I/O below (claim_outbox_batch / mark_outbox_failed /
        # mark_outbox_published) is offloaded per call so it never blocks the
        # event loop. Per-call ``to_thread`` is safe HERE (unlike the ingest
        # unit of work): each method delegates to ``OutboxStore``, whose
        # session factory (``_StoreInfra._session``) consults the
        # ``_ACTIVE_SESSION`` ContextVar only to JOIN an in-flight
        # ``unit_of_work``. The relay never runs inside a unit of work, so the
        # context copied into each worker thread carries no session and every
        # call opens, commits, and closes its own Session entirely within that
        # thread.
        claimed = await asyncio.to_thread(
            self._store.claim_outbox_batch,
            topics=topics,
            consumer=self._consumer_name,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        processed = 0
        failed = 0
        dead_lettered = 0
        for record in claimed:
            try:
                delivery_metadata = await self._deliver(record)
            except Exception as exc:
                # The claim operation has already incremented attempts.
                # Completion remains fenced by owner and attempt generation.
                current_attempts = max(1, record.attempts)
                failure_class = _failure_class(exc)
                will_dead_letter = failure_class == "permanent" or current_attempts >= max(
                    1, record.max_attempts
                )
                logger.exception(
                    "outbox.delivery_failed",
                    extra={
                        "event_id": record.event_id,
                        "topic": record.topic,
                        "attempts": current_attempts,
                        "max_attempts": record.max_attempts,
                        "will_dead_letter": will_dead_letter,
                        "failure_class": failure_class,
                    },
                )
                marked_failed = await asyncio.to_thread(
                    self._store.mark_outbox_failed,
                    record.event_id,
                    expected_claim_owner=record.claim_owner or self._consumer_name,
                    expected_attempts=record.attempts,
                    error_message=str(exc),
                    retry_delay_seconds=_failure_backoff_seconds(record.attempts),
                    failure_class=failure_class,
                    terminal=failure_class == "permanent",
                )
                if not marked_failed:
                    logger.warning(
                        "outbox.claim_lost_before_failure",
                        extra={
                            "event_id": record.event_id,
                            "claim_owner": record.claim_owner,
                            "attempts": record.attempts,
                        },
                    )
                    continue
                failed += 1
                if _OUTBOX_DELIVERED is not None:
                    _OUTBOX_DELIVERED.labels("dead_letter" if will_dead_letter else "failed").inc()
                if will_dead_letter:
                    dead_lettered += 1
                continue

            marked_published = await asyncio.to_thread(
                self._store.mark_outbox_published,
                record.event_id,
                expected_claim_owner=record.claim_owner or self._consumer_name,
                expected_attempts=record.attempts,
                delivery_metadata=delivery_metadata,
            )
            if not marked_published:
                logger.warning(
                    "outbox.claim_lost_before_publish",
                    extra={
                        "event_id": record.event_id,
                        "claim_owner": record.claim_owner,
                        "attempts": record.attempts,
                    },
                )
                continue
            processed += 1
            if _OUTBOX_DELIVERED is not None:
                _OUTBOX_DELIVERED.labels("published").inc()

        if claimed:
            logger.info(
                "outbox.drain_summary",
                extra={
                    "claimed": len(claimed),
                    "published": processed,
                    "failed": failed,
                    "dead_lettered": dead_lettered,
                    "consumer": self._consumer_name,
                },
            )
        return processed

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

    def _update_backlog_gauge(self, topics: list[str]) -> None:
        """Refresh backlog metrics without risking relay availability."""
        if _OUTBOX_BACKLOG is None:
            return
        try:
            counts = self._store.outbox_backlog_counts(topics=topics)
        except (SQLAlchemyError, OSError):
            logger.warning("Outbox backlog gauge query failed", exc_info=True)
            return
        for status in _BACKLOG_STATUSES:
            _OUTBOX_BACKLOG.labels(status=status).set(counts.get(status, 0))

    async def run_forever(
        self,
        *,
        topics: Iterable[str],
        poll_interval_seconds: float = 2.0,
        limit: int = 100,
        lease_seconds: int = 60,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        notify_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._start_notify_listener(notify_event, loop)
        topic_list = list(topics)

        try:
            while not stop_event.is_set():
                self._update_backlog_gauge(topic_list)
                self._refresh_notify_gauge()
                try:
                    processed = await self.drain_once(
                        topics=topic_list,
                        limit=limit,
                        lease_seconds=lease_seconds,
                    )
                except (SQLAlchemyError, OSError):
                    logger.exception("Outbox drain cycle failed; retrying after poll interval")
                    await asyncio.sleep(poll_interval_seconds)
                    continue
                if processed > 0:
                    notify_event.clear()
                    continue

                stop_task = asyncio.create_task(stop_event.wait())
                notify_task = asyncio.create_task(notify_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {stop_task, notify_task},
                        timeout=poll_interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    # Await the cancelled waiters so no task is abandoned with
                    # an unretrieved CancelledError.
                    for task in (stop_task, notify_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(stop_task, notify_task, return_exceptions=True)
                if notify_task in done:
                    notify_event.clear()
        finally:
            self._stop_notify_listener()

    async def _deliver(self, record: OutboxRecord) -> dict[str, Any]:
        if record.topic in {"execution.commands", "execution.rebalance.commands"}:
            return await self._deliver_execution_command(record)
        # The observational topics were retired with their producers; a stray
        # row must not loop through retries, so classify it permanent.
        msg = f"Outbox topic {record.topic!r} has no consumer"
        raise OutboxDeliveryError(msg, failure_class="permanent")

    async def _deliver_execution_command(self, record: OutboxRecord) -> dict[str, Any]:
        async def _post_command() -> httpx.Response:
            headers: dict[str, str] = {}
            run_id = str(record.headers.get("run_id") or record.payload.get("run_id") or "")
            if run_id:
                headers["X-Run-ID"] = run_id
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            response = await self._client.post(
                (
                    f"{self._exec_engine_url}/execute-rebalance-command"
                    if record.topic == "execution.rebalance.commands"
                    else f"{self._exec_engine_url}/execute-command"
                ),
                json=record.payload,
                headers=headers,
            )
            if response.status_code >= _HTTP_SERVER_ERROR_STATUS:
                msg = f"Server error {response.status_code}"
                raise httpx.HTTPStatusError(
                    msg,
                    request=response.request,
                    response=response,
                )
            if response.status_code >= _HTTP_CLIENT_ERROR_STATUS:
                msg = f"Execution command rejected ({response.status_code}): {response.text}"
                if response.status_code in _TRANSIENT_HTTP_STATUSES:
                    raise httpx.HTTPStatusError(
                        msg,
                        request=response.request,
                        response=response,
                    )
                raise OutboxDeliveryError(msg, failure_class="permanent")
            return response

        response = await retry_async(
            _post_command,
            retries=2,
            base_delay=0.5,
            max_delay=5.0,
            retry_on=(httpx.RequestError, httpx.HTTPStatusError),
            operation_name="execution_command_delivery",
            log=logger,
        )
        return {
            "delivery": "http",
            "status_code": response.status_code,
            "response": response.json(),
        }

    async def aclose(self) -> None:
        await self._client.aclose()
