"""OBS-5: structured JSON logging so a shipper can extract fields (run_id,
correlation_id, symbol, ...) rather than parse logfmt."""

from __future__ import annotations

import json
import logging

from lib_common.logging import (
    _JsonFormatter,
    _select_formatter,
    _StructuredFormatter,
    setup_logging,
)


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="svc",
        level=logging.INFO,
        pathname="x.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_valid_json_with_rendered_message() -> None:
    payload = json.loads(_JsonFormatter().format(_record()))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "svc"
    assert "ts" in payload


def test_json_formatter_includes_structured_extras() -> None:
    payload = json.loads(_JsonFormatter().format(_record(run_id="r-1", symbol="BTCUSD")))
    assert payload["run_id"] == "r-1"
    assert payload["symbol"] == "BTCUSD"


def test_json_formatter_handles_non_serializable_extra() -> None:
    # Must not raise — non-serializable values fall back to str().
    payload = json.loads(_JsonFormatter().format(_record(obj=object())))
    assert "obj" in payload


def test_select_formatter_json_via_env(monkeypatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert isinstance(_select_formatter(None), _JsonFormatter)


def test_select_formatter_defaults_to_logfmt(monkeypatch) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    assert isinstance(_select_formatter(None), _StructuredFormatter)


def test_explicit_format_string_forces_logfmt(monkeypatch) -> None:
    # An explicit format string forces logfmt even when LOG_FORMAT=json.
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert isinstance(_select_formatter("%(message)s"), _StructuredFormatter)


# ── O3: chatty HTTP-client access logs are quieted by default ──


def test_setup_logging_quiets_http_clients_to_warning(monkeypatch) -> None:
    monkeypatch.delenv("HTTP_CLIENT_LOG_LEVEL", raising=False)
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    setup_logging("INFO")

    for name in ("httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_http_client_log_level_env_override_restores_access_logs(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_CLIENT_LOG_LEVEL", "INFO")

    setup_logging("INFO")

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.INFO
