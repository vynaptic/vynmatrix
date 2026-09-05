"""Tests for the pluggable multi-sink alert publisher."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from lib_common.alerting import (
    Alert,
    EmailSink,
    MultiSinkAlertPublisher,
    TelegramSink,
    WebhookSink,
    build_sinks_from_env,
)


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url, json):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, "json": json})

    def close(self) -> None:
        return None


class _FakeSmtp:
    sent: ClassVar[list[Any]] = []

    def __init__(self) -> None:
        self.tls = False
        self.login_args: tuple[str, str] | None = None
        self.quit_called = False

    def starttls(self) -> None:
        self.tls = True

    def login(self, user, password):  # type: ignore[no-untyped-def]
        self.login_args = (user, password)

    def send_message(self, msg):  # type: ignore[no-untyped-def]
        _FakeSmtp.sent.append(msg)

    def quit(self) -> None:
        self.quit_called = True


def _alert() -> Alert:
    return Alert(
        event_type="feed_stale",
        severity="critical",
        message="no data for 5m",
        payload={"symbol": "BTC-USD"},
        source="market_data_ingestor",
    )


def test_alert_text_formatting() -> None:
    a = _alert()
    assert a.title() == "[CRITICAL] market_data_ingestor feed_stale"
    assert a.as_text() == (
        "[CRITICAL] market_data_ingestor feed_stale\nno data for 5m\nsymbol: BTC-USD"
    )


def test_webhook_sink_posts_rich_payload() -> None:
    http = _FakeHttp()
    WebhookSink("https://hook.test/x", client=http).send(_alert())
    assert http.calls[0]["url"] == "https://hook.test/x"
    body = http.calls[0]["json"]
    assert body["event_type"] == "feed_stale"
    assert body["source"] == "market_data_ingestor"
    assert "feed_stale" in body["text"]


def test_telegram_sink_posts_to_bot_api() -> None:
    http = _FakeHttp()
    TelegramSink("TOK", "CHAT", client=http, api_base="https://tg.test").send(_alert())
    assert http.calls[0]["url"] == "https://tg.test/botTOK/sendMessage"
    assert http.calls[0]["json"]["chat_id"] == "CHAT"
    assert "no data for 5m" in http.calls[0]["json"]["text"]


def test_email_sink_builds_and_sends_message() -> None:
    _FakeSmtp.sent = []
    smtp = _FakeSmtp()
    EmailSink(
        host="smtp.test",
        sender="alerts@example.invalid",
        recipients=["ops@example.invalid", "oncall@example.invalid"],
        username="u",
        password="p",
        smtp_factory=lambda: smtp,
    ).send(_alert())
    assert smtp.tls is True
    assert smtp.login_args == ("u", "p")
    assert smtp.quit_called is True
    msg = _FakeSmtp.sent[0]
    assert msg["Subject"] == "[CRITICAL] market_data_ingestor feed_stale"
    assert msg["To"] == "ops@example.invalid, oncall@example.invalid"


def test_multi_sink_fans_out_best_effort() -> None:
    good1, good2 = _FakeHttp(), _FakeHttp()

    class _BoomSink:
        name = "boom"

        def send(self, alert):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

        def close(self) -> None:
            return None

    pub = MultiSinkAlertPublisher(
        [WebhookSink("u1", client=good1), _BoomSink(), WebhookSink("u2", client=good2)],
        dispatch_async=False,
    )
    pub.publish(_alert())
    # A failing sink does not stop the others.
    assert len(good1.calls) == 1
    assert len(good2.calls) == 1


def test_disabled_or_empty_publisher_is_noop() -> None:
    http = _FakeHttp()
    disabled = MultiSinkAlertPublisher(
        [WebhookSink("u", client=http)], enabled=False, dispatch_async=False
    )
    disabled.publish(_alert())
    assert http.calls == []
    # No sinks -> enabled collapses to False.
    empty = MultiSinkAlertPublisher([], enabled=True, dispatch_async=False)
    assert empty.enabled is False


def test_build_sinks_from_env_selects_configured_channels() -> None:
    assert build_sinks_from_env({}) == []
    sinks = build_sinks_from_env(
        {
            "ALERT_WEBHOOK_URL": "https://hook.test",
            "ALERT_TELEGRAM_BOT_TOKEN": "TOK",
            "ALERT_TELEGRAM_CHAT_ID": "CHAT",
            "ALERT_EMAIL_SMTP_HOST": "smtp.test",
            "ALERT_EMAIL_TO": "ops@example.invalid",
        }
    )
    assert sorted(s.name for s in sinks) == ["email", "telegram", "webhook"]
    for s in sinks:
        s.close()
    # Telegram needs BOTH token and chat id.
    assert build_sinks_from_env({"ALERT_TELEGRAM_BOT_TOKEN": "TOK"}) == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALERT_EMAIL_SMTP_PORT", "not-a-port"),
        ("ALERT_EMAIL_TLS", "sometimes"),
    ],
)
def test_build_sinks_from_env_rejects_invalid_typed_values(
    name: str,
    value: str,
) -> None:
    env = {
        "ALERT_EMAIL_SMTP_HOST": "smtp.test",
        "ALERT_EMAIL_TO": "ops@example.invalid",
        name: value,
    }

    with pytest.raises(ValueError, match=name):
        build_sinks_from_env(env)
