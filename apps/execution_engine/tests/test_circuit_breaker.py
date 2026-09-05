from __future__ import annotations

from datetime import UTC, datetime, timedelta

from execution_engine.circuit_breaker import CircuitBreaker


def test_circuit_breaker_opens_after_threshold_within_window() -> None:
    breaker = CircuitBreaker(threshold=3, window_sec=300, cooldown_sec=900)
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)

    assert breaker.record_failure("k", reason="reject-1", now=now) is False
    assert breaker.record_failure("k", reason="reject-2", now=now + timedelta(seconds=1)) is False
    assert breaker.record_failure("k", reason="reject-3", now=now + timedelta(seconds=2)) is True
    assert breaker.is_open("k", now=now + timedelta(seconds=3)) is True


def test_circuit_breaker_recovers_after_cooldown() -> None:
    breaker = CircuitBreaker(threshold=2, window_sec=60, cooldown_sec=30)
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)

    breaker.record_failure("k", reason="reject-1", now=now)
    breaker.record_failure("k", reason="reject-2", now=now + timedelta(seconds=1))

    assert breaker.is_open("k", now=now + timedelta(seconds=2)) is True
    assert breaker.is_open("k", now=now + timedelta(seconds=31)) is False
    assert breaker.snapshot("k")["failure_count"] == 0
