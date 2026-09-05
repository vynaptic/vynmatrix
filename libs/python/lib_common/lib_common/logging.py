"""Logging utilities.

This module provides structured logging support with keyword argument binding.
Uses structlog-style API (logger.info("msg", key=value)) but with stdlib logging.
"""

import json
import logging
import os
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any


class _StructuredLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Logger adapter that supports keyword arguments for structured logging."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        """Process log message to include extra keyword arguments."""
        # Extract extra kwargs that aren't standard logging kwargs
        standard_keys = {"exc_info", "stack_info", "stacklevel", "extra"}
        extra_data = {k: v for k, v in kwargs.items() if k not in standard_keys}

        # Remove extra kwargs from kwargs dict
        for key in extra_data:
            kwargs.pop(key, None)

        # Merge with existing extra
        existing_extra = kwargs.get("extra", {})
        if isinstance(existing_extra, dict):
            existing_extra.update(extra_data)
        else:
            existing_extra = extra_data
        kwargs["extra"] = existing_extra

        return msg, kwargs


def _standard_log_record_attrs() -> set[str]:
    record = logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    )
    return set(record.__dict__.keys())


_STANDARD_LOG_RECORD_ATTRS = _standard_log_record_attrs()


def _iter_extra_fields(record: logging.LogRecord) -> Iterable[tuple[str, Any]]:
    extra_keys = [key for key in record.__dict__ if key not in _STANDARD_LOG_RECORD_ATTRS]
    for key in sorted(extra_keys):
        yield key, record.__dict__.get(key)


def _format_extra_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (int, float, bool)):
        return str(value)
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or "=" in text:
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


class _StructuredFormatter(logging.Formatter):
    """Formatter that appends structured extra fields as logfmt."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra_parts = [
            f"{key}={_format_extra_value(value)}" for key, value in _iter_extra_fields(record)
        ]
        if not extra_parts:
            return base
        return f"{base} | {' '.join(extra_parts)}"


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per record so a log shipper can extract fields.

    Standard fields plus any ``get_logger(...).info("msg", key=value)`` extras
    (run_id, correlation_id, symbol, ...) become top-level keys. Non-serializable
    values fall back to ``str``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _iter_extra_fields(record):
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _select_formatter(format_string: str | None) -> logging.Formatter:
    """Pick the JSON formatter when ``LOG_FORMAT=json`` (and no explicit logfmt
    format string is given), else the human-readable logfmt formatter."""
    if format_string is None and os.getenv("LOG_FORMAT", "logfmt").strip().lower() == "json":
        return _JsonFormatter()
    if format_string is None:
        format_string = (
            "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
        )
    return _StructuredFormatter(format_string)


def setup_logging(level: str = "INFO", format_string: str | None = None) -> None:
    """Setup logging configuration.

    Output format is JSON when ``LOG_FORMAT=json`` (set in deployment env so
    log shippers get structured fields), otherwise the human-readable logfmt
    formatter. An explicit ``format_string`` forces logfmt.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_select_formatter(format_string))

    resolved_level = getattr(logging, level.upper(), None)
    if resolved_level is None:
        resolved_level = logging.INFO
        logging.warning(
            "Invalid LOG_LEVEL '%s'; falling back to INFO. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL",
            level,
        )

    logging.basicConfig(
        level=resolved_level,
        handlers=[handler],
    )

    # Quiet chatty third-party HTTP clients. httpx/httpcore/urllib3 emit one INFO
    # access line per request (each carrying a full request URL); in the soak
    # these were 56-75% of some service logs and dwarfed the service's own output
    # (O3). Default WARNING; set HTTP_CLIENT_LOG_LEVEL=INFO to restore access logs
    # when debugging an HTTP client.
    http_client_level = getattr(
        logging, os.getenv("HTTP_CLIENT_LOG_LEVEL", "WARNING").upper(), logging.WARNING
    )
    for noisy_logger in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy_logger).setLevel(http_client_level)


def get_logger(name: str) -> Any:
    """Get a structured logger instance.

    Returns a logger that supports keyword arguments for structured logging:
        logger = get_logger(__name__)
        logger.info("User action", user_id=123, action="login")

    The returned logger accepts arbitrary keyword arguments which are
    passed to the underlying log record's extra dict.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance supporting keyword arguments
    """
    base_logger = logging.getLogger(name)
    return _StructuredLoggerAdapter(base_logger, {})
