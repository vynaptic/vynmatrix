"""PostgreSQL LISTEN/NOTIFY client with reconnect and periodic catch-up polling.

Used by the signal worker to react to new market data committed by the
market_data_ingestor without polling the prices table directly.
"""

from __future__ import annotations

import json
import select as _select
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from lib_common.logging import get_logger
from lib_common.time_utils import now_utc

logger = get_logger(__name__)

# Type for the notification callback
NotifyCallback = Callable[[str, dict[str, Any]], None]


class PgNotifyListener:
    """Listen for PostgreSQL NOTIFY events on a channel with auto-reconnect.

    Usage::

        def on_market_data(channel: str, payload: dict) -> None:
            print(f"New data: {payload}")

        listener = PgNotifyListener(
            dsn="postgresql://...",
            channel="new_market_data",
            callback=on_market_data,
        )
        listener.run_forever()  # blocks, call stop() from another thread

    Features:
        - Automatic reconnect on connection loss
        - JSON payload parsing
        - Periodic catch-up poll interval to handle missed notifications
    """

    def __init__(
        self,
        *,
        dsn: str,
        channel: str = "new_market_data",
        callback: NotifyCallback | None = None,
        reconnect_delay_sec: float = 5.0,
        catchup_interval_sec: float = 60.0,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._callback = callback
        self._reconnect_delay = reconnect_delay_sec
        self._catchup_interval = catchup_interval_sec
        self._stop_event = threading.Event()
        self._listening_event = threading.Event()
        self._conn: Any = None  # psycopg2 connection (lazy import)

    def stop(self) -> None:
        self._stop_event.set()

    def wait_until_listening(self, timeout: float | None = None) -> bool:
        """Wait until PostgreSQL has acknowledged this connection's LISTEN.

        Callers that must publish immediately after startup can use this
        readiness boundary instead of racing the background connection setup.
        Reconnects clear the event until the replacement connection has
        registered the channel again.
        """
        return self._listening_event.wait(timeout)

    def run_forever(self) -> None:
        """Block and listen for notifications until stop() is called."""
        while not self._stop_event.is_set():
            try:
                self._connect()
                self._listen_loop()
            except Exception:
                logger.warning(
                    "LISTEN connection lost, reconnecting in %.1fs",
                    self._reconnect_delay,
                    exc_info=True,
                )
                self._close()
                if not self._stop_event.wait(self._reconnect_delay):
                    continue

        self._close()
        logger.info("PgNotifyListener stopped")

    def _connect(self) -> None:
        import psycopg2  # noqa: PLC0415

        self._listening_event.clear()
        self._conn = psycopg2.connect(self._dsn)
        self._conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        with self._conn.cursor() as cur:
            cur.execute(f"LISTEN {self._channel};")
        self._listening_event.set()
        logger.info("Listening on channel '%s'", self._channel)

    def _close(self) -> None:
        self._listening_event.clear()
        if self._conn is not None:
            with suppress(Exception):
                self._conn.close()
            self._conn = None

    def _listen_loop(self) -> None:
        """Poll the connection for notifications with periodic catch-up."""
        last_catchup = time.monotonic()

        while not self._stop_event.is_set():
            # select() with timeout so we can check stop_event periodically
            if _select.select([self._conn], [], [], 1.0) == ([], [], []):
                # Timeout — check if catch-up poll is due
                now = time.monotonic()
                if now - last_catchup >= self._catchup_interval:
                    last_catchup = now
                    if self._callback:
                        self._callback(
                            self._channel,
                            {"type": "catchup", "ts": now_utc().isoformat()},
                        )
                continue

            self._conn.poll()
            while self._conn.notifies:
                notify = self._conn.notifies.pop(0)
                payload = self._parse_payload(notify.payload)
                if self._callback:
                    try:
                        self._callback(notify.channel, payload)
                    except Exception:
                        logger.exception(
                            "Error in NOTIFY callback for channel '%s'",
                            notify.channel,
                        )

    @staticmethod
    def _parse_payload(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw}
        if isinstance(payload, dict):
            return payload
        return {"raw": payload}
