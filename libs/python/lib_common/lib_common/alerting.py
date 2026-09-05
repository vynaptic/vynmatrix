"""Pluggable operational alert publishing — webhook, Telegram, and email sinks.

Shared across services so any of them can notify a human on operational failures
(circuit breakers, reconciliation breaks, feed staleness, ...). Sinks are
best-effort: a delivery failure is logged, never raised, and never blocks the
caller. Delivery is dispatched off the caller's thread.

Configure via env (any subset; a sink is added only when its vars are present):
- ``ALERT_WEBHOOK_URL``                         -> WebhookSink (generic JSON POST)
- ``ALERT_TELEGRAM_BOT_TOKEN`` + ``ALERT_TELEGRAM_CHAT_ID``  -> TelegramSink
- ``ALERT_EMAIL_SMTP_HOST`` + ``ALERT_EMAIL_TO`` (+ PORT/FROM/USERNAME/PASSWORD/TLS) -> EmailSink

SMS / WhatsApp need a paid provider (e.g. Twilio) and are intentionally not
implemented here yet — add a sink class + an env branch when that's wanted.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from email.message import EmailMessage
from threading import Lock
from typing import Any, Protocol, runtime_checkable

import httpx

from lib_common.env_utils import parse_bool_value, parse_int_value
from lib_common.logging import get_logger

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"
_DEFAULT_SMTP_PORT = 587


@dataclass
class Alert:
    """An operational alert to deliver to humans."""

    event_type: str
    severity: str  # "info" | "warning" | "critical"
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # emitting service, e.g. "execution_engine"

    def title(self) -> str:
        parts = [f"[{self.severity.upper()}]"]
        if self.source:
            parts.append(self.source)
        parts.append(self.event_type)
        return " ".join(parts)

    def as_text(self) -> str:
        lines = [self.title(), self.message]
        if self.payload:
            lines.extend(f"{k}: {v}" for k, v in self.payload.items())
        return "\n".join(lines)


@runtime_checkable
class AlertSink(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...

    def close(self) -> None: ...


class WebhookSink:
    """POST the alert as JSON to a generic webhook (Slack-compatible shape)."""

    name = "webhook"

    def __init__(self, url: str, *, client: httpx.Client | None = None) -> None:
        self._url = url
        self._client = client or httpx.Client(timeout=5.0)
        self._owns_client = client is None

    def send(self, alert: Alert) -> None:
        self._client.post(
            self._url,
            json={
                "event_type": alert.event_type,
                "severity": alert.severity,
                "message": alert.message,
                "payload": alert.payload,
                "source": alert.source,
                "text": alert.as_text(),
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class TelegramSink:
    """Send the alert via the Telegram Bot API (free; just a bot token)."""

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.Client | None = None,
        api_base: str = _TELEGRAM_API_BASE,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=5.0)
        self._owns_client = client is None

    def send(self, alert: Alert) -> None:
        self._client.post(
            f"{self._api_base}/bot{self._bot_token}/sendMessage",
            json={"chat_id": self._chat_id, "text": alert.as_text()},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class EmailSink:
    """Send the alert as a plaintext email over SMTP."""

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = _DEFAULT_SMTP_PORT,
        sender: str,
        recipients: Sequence[str],
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        smtp_factory: Any | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._recipients = list(recipients)
        self._username = username
        self._password = password
        self._use_tls = use_tls
        # Injectable for tests; defaults to a real SMTP connection.
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP(host, port, timeout=10))

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = alert.title()
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        msg.set_content(alert.as_text())
        smtp = self._smtp_factory()
        try:
            if self._use_tls:
                smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)
        finally:
            smtp.quit()

    def close(self) -> None:
        return None


class MultiSinkAlertPublisher:
    """Fan an alert out to every configured sink, best-effort and off-thread."""

    def __init__(
        self,
        sinks: Sequence[AlertSink],
        *,
        enabled: bool = True,
        dispatch_async: bool = True,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._sinks = list(sinks)
        # Disabled with no sinks → publish() is a cheap no-op.
        self._enabled = enabled and bool(self._sinks)
        self._dispatch_async = dispatch_async
        self._owns_executor = dispatch_async and executor is None
        self._executor = executor
        if self._dispatch_async and self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alerts")
        self._pending: list[Future[Any]] = []
        self._pending_lock = Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def sink_names(self) -> list[str]:
        return [s.name for s in self._sinks]

    def publish(self, alert: Alert) -> None:
        if not self._enabled:
            return
        if self._dispatch_async and self._executor is not None:
            future = self._executor.submit(self._fan_out, alert)
            with self._pending_lock:
                self._pending = [f for f in self._pending if not f.done()]
                self._pending.append(future)
            return
        self._fan_out(alert)

    def _fan_out(self, alert: Alert) -> None:
        for sink in self._sinks:
            try:
                sink.send(alert)
            except Exception:
                # Best-effort: one failing channel must not block the others or
                # the caller. (lib_common is outside the apps/ bare-except gate.)
                logger.exception(
                    "Alert sink delivery failed",
                    sink=sink.name,
                    event_type=alert.event_type,
                    severity=alert.severity,
                )

    def close(self) -> None:
        with self._pending_lock:
            pending = [f for f in self._pending if not f.done()]
            self._pending = []
        if pending:
            wait(pending, timeout=5.0)
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=True)
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                logger.exception("Alert sink close failed", sink=sink.name)


def build_sinks_from_env(env: Mapping[str, str] | None = None) -> list[AlertSink]:
    """Construct the alert sinks declared by ``ALERT_*`` environment variables."""
    e = env if env is not None else os.environ
    sinks: list[AlertSink] = []

    webhook_url = e.get("ALERT_WEBHOOK_URL")
    if webhook_url:
        sinks.append(WebhookSink(webhook_url))

    token = e.get("ALERT_TELEGRAM_BOT_TOKEN")
    chat_id = e.get("ALERT_TELEGRAM_CHAT_ID")
    if token and chat_id:
        sinks.append(TelegramSink(token, chat_id))

    smtp_host = e.get("ALERT_EMAIL_SMTP_HOST")
    recipients = e.get("ALERT_EMAIL_TO")
    if smtp_host and recipients:
        username = e.get("ALERT_EMAIL_USERNAME")
        sinks.append(
            EmailSink(
                host=smtp_host,
                port=parse_int_value(
                    e.get("ALERT_EMAIL_SMTP_PORT"),
                    name="ALERT_EMAIL_SMTP_PORT",
                    default=_DEFAULT_SMTP_PORT,
                    min_value=1,
                    max_value=65535,
                    logger=logger,
                    strict=True,
                ),
                sender=e.get("ALERT_EMAIL_FROM") or username or "alerts@example.invalid",
                recipients=[r.strip() for r in recipients.split(",") if r.strip()],
                username=username,
                password=e.get("ALERT_EMAIL_PASSWORD"),
                use_tls=parse_bool_value(
                    e.get("ALERT_EMAIL_TLS"),
                    name="ALERT_EMAIL_TLS",
                    default=True,
                    logger=logger,
                    strict=True,
                ),
            )
        )

    return sinks


def build_publisher_from_env(
    *,
    enabled: bool,
    extra_sinks: Sequence[AlertSink] = (),
    env: Mapping[str, str] | None = None,
) -> MultiSinkAlertPublisher:
    """Build a publisher from env-declared sinks plus any explicit ``extra_sinks``."""
    sinks: list[AlertSink] = [*extra_sinks, *build_sinks_from_env(env)]
    return MultiSinkAlertPublisher(sinks, enabled=enabled)
