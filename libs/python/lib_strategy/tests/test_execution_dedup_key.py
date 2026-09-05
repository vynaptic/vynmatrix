"""compute_execution_dedup_key is the single source of truth for the execution
idempotency key, shared by the scoring and execution engines (SC-1)."""

from __future__ import annotations

from lib_strategy.signals.utils import compute_execution_dedup_key


def test_format_is_exec_prefixed_32_hex() -> None:
    key = compute_execution_dedup_key("ext-1", "user-1", 11, "BTCUSD", "long")
    assert key.startswith("exec:")
    digest = key.removeprefix("exec:")
    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)


def test_deterministic_for_same_inputs() -> None:
    a = compute_execution_dedup_key("ext-1", "user-1", 11, "BTCUSD", "long")
    b = compute_execution_dedup_key("ext-1", "user-1", 11, "BTCUSD", "long")
    assert a == b


def test_each_component_changes_the_key() -> None:
    base = compute_execution_dedup_key("ext-1", "user-1", 11, "BTCUSD", "long")
    assert base != compute_execution_dedup_key("ext-2", "user-1", 11, "BTCUSD", "long")
    assert base != compute_execution_dedup_key("ext-1", "user-2", 11, "BTCUSD", "long")
    assert base != compute_execution_dedup_key("ext-1", "user-1", 12, "BTCUSD", "long")
    assert base != compute_execution_dedup_key("ext-1", "user-1", 11, "ETHUSD", "long")
    assert base != compute_execution_dedup_key("ext-1", "user-1", 11, "BTCUSD", "short")
