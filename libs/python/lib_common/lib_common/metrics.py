"""Shared Prometheus instrumentation helpers."""

from __future__ import annotations

import os
from typing import Any

# Names are Any-typed module aliases assigned from the module object so this
# file type-checks identically whether prometheus-client is installed (CI step
# env) or absent (pre-commit hook env) — no environment-dependent
# `type: ignore` lines and no name redefinition.
CONTENT_TYPE_LATEST: str = "text/plain; version=0.0.4; charset=utf-8"
CollectorRegistry: Any = None
Counter: Any = None
Gauge: Any = None
Histogram: Any = None
generate_latest: Any = None
multiprocess: Any = None

try:
    import prometheus_client as _prometheus_client
    from prometheus_client import multiprocess as _prometheus_multiprocess
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    pass
else:
    CONTENT_TYPE_LATEST = _prometheus_client.CONTENT_TYPE_LATEST
    CollectorRegistry = _prometheus_client.CollectorRegistry
    Counter = _prometheus_client.Counter
    Gauge = _prometheus_client.Gauge
    Histogram = _prometheus_client.Histogram
    generate_latest = _prometheus_client.generate_latest
    multiprocess = _prometheus_multiprocess

_COUNTERS: dict[str, Any] = {}
_GAUGES: dict[str, Any] = {}
_HISTOGRAMS: dict[str, Any] = {}

# Latency buckets (seconds) tuned for the trading data plane: sub-ms to ~30s.
_DEFAULT_LATENCY_BUCKETS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


def prometheus_available() -> bool:
    """Return whether prometheus-client is installed."""
    return generate_latest is not None


def counter(
    name: str,
    documentation: str,
    labelnames: tuple[str, ...] | None = None,
) -> Any:
    """Return a singleton Counter by name."""
    if Counter is None:
        return None
    if name not in _COUNTERS:
        _COUNTERS[name] = Counter(name, documentation, labelnames or ())
    return _COUNTERS[name]


def gauge(
    name: str,
    documentation: str,
    labelnames: tuple[str, ...] | None = None,
) -> Any:
    """Return a singleton Gauge by name."""
    if Gauge is None:
        return None
    if name not in _GAUGES:
        _GAUGES[name] = Gauge(name, documentation, labelnames or ())
    return _GAUGES[name]


def histogram(
    name: str,
    documentation: str,
    labelnames: tuple[str, ...] | None = None,
    buckets: tuple[float, ...] | None = None,
) -> Any:
    """Return a singleton Histogram by name (latency/size distributions)."""
    if Histogram is None:
        return None
    if name not in _HISTOGRAMS:
        _HISTOGRAMS[name] = Histogram(
            name,
            documentation,
            labelnames or (),
            buckets=buckets or _DEFAULT_LATENCY_BUCKETS,
        )
    return _HISTOGRAMS[name]


def metrics_payload() -> bytes:
    """Render Prometheus metrics."""
    if generate_latest is None:
        return b"prometheus_client_not_installed 1\n"
    multiprocess_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiprocess_dir:
        if CollectorRegistry is None or multiprocess is None:  # pragma: no cover
            return b"prometheus_client_multiprocess_unavailable 1\n"
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=multiprocess_dir)
        return bytes(generate_latest(registry))
    return bytes(generate_latest())


__all__ = [
    "CONTENT_TYPE_LATEST",
    "counter",
    "gauge",
    "histogram",
    "metrics_payload",
    "prometheus_available",
]
