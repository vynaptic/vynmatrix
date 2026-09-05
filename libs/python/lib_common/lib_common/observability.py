"""Observability helpers for correlation IDs and structured logging."""

from __future__ import annotations

import importlib
import os
import uuid
from typing import Any


def ensure_run_id(run_id: str | None = None) -> str:
    """Return a run_id, preferring explicit value, then env RUN_ID, else UUID."""
    if run_id:
        return run_id
    env_run_id = os.getenv("RUN_ID")
    if env_run_id:
        return env_run_id
    return str(uuid.uuid4())


def build_trace_context(**kwargs: Any) -> dict[str, Any]:
    """Build a log context dict with only non-empty values."""
    context: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        context[key] = value
    return context


def init_tracing(service_name: str) -> bool:
    """Initialize OpenTelemetry tracing if (and only if) configured.

    No-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set AND the optional
    ``opentelemetry-sdk`` / OTLP-exporter packages are installed, so production
    behavior is unchanged until a tracing backend (collector) is provisioned —
    a deferred owner action. Returns ``True`` when a tracer provider was
    installed, ``False`` otherwise.

    The pipeline already threads ``run_id`` / ``correlation_id`` /
    ``causation_id`` through structured logs and internal events; once a
    collector exists, set the endpoint and install the optional deps to emit
    spans without further code changes.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    # Import the optional OpenTelemetry stack by name so a missing dependency is a
    # clean no-op (no static import to satisfy when otel is not installed).
    try:
        otel_trace = importlib.import_module("opentelemetry.trace")
        otlp = importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        resources = importlib.import_module("opentelemetry.sdk.resources")
        sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
        sdk_export = importlib.import_module("opentelemetry.sdk.trace.export")
    except ImportError:
        return False

    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(
        sdk_export.BatchSpanProcessor(otlp.OTLPSpanExporter(endpoint=endpoint))
    )
    otel_trace.set_tracer_provider(provider)
    return True
