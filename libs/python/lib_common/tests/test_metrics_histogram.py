"""The histogram primitive backs the pipeline latency instrumentation (G6)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib_common.metrics import counter, histogram, metrics_payload, prometheus_available


def test_histogram_is_singleton_and_observes() -> None:
    h1 = histogram("vm_test_latency_seconds", "test latency")
    h2 = histogram("vm_test_latency_seconds", "test latency")
    if prometheus_available():
        assert h1 is h2  # singleton by name
        h1.observe(0.012)  # must not raise
        h1.observe(3.4)
    else:
        assert h1 is None


def test_labelled_histogram_and_counter_render_in_payload() -> None:
    if not prometheus_available():
        return
    counter("vm_test_g6_total", "g6 counter", ("outcome",)).labels("published").inc()
    histogram("vm_test_g6_seconds", "g6 latency", ("stage",)).labels("ingest").observe(0.05)
    payload = metrics_payload().decode()
    assert "vm_test_g6_total" in payload
    assert "vm_test_g6_seconds_bucket" in payload  # histogram buckets exported


def test_metrics_payload_aggregates_child_process_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The indicator parent can expose counters written by strategy workers."""
    if not prometheus_available():
        return
    package_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(package_root), env.get("PYTHONPATH")) if part
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from lib_common.metrics import counter; "
                "metric = counter("
                "'vm_test_multiprocess_child_total', "
                "'child counter', "
                "('strategy_id',)"
                "); "
                "metric.labels(strategy_id='test_strategy_alpha_v1').inc(2)"
            ),
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    payload = metrics_payload().decode()

    assert "vm_test_multiprocess_child_total" in payload
    assert 'strategy_id="test_strategy_alpha_v1"' in payload
    assert " 2.0" in payload
