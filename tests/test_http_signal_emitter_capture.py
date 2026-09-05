from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "python" / "lib_strategy"))

from lib_common.internal_events import (
    ModelRebalanceSubmissionEvent,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
)
from lib_strategy.signals.emitter import HttpSignalEmitter
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id


def _signal() -> Signal:
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=SignalAction.LONG,
        confidence=0.75,
        timestamp=datetime(2026, 3, 5, 9, 30, tzinfo=UTC),
        horizon="1D",
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        external_signal_id="external-signal-1",
        expires_at=datetime(2026, 3, 5, 9, 30, tzinfo=UTC) + timedelta(hours=1),
        metadata={"price_source": "coinbase_live", "custom": "kept"},
    )


def _read_capture(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "captured_at" in record
    return record["signal"]


def _model_rebalance_event() -> ModelRebalanceSubmissionEvent:
    cutoff = datetime(2025, 12, 31, 21, 0, tzinfo=UTC)
    execute_at = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    kwargs: dict[str, Any] = {
        "strategy_id": "us_quality_compounder_v1",
        "strategy_version": "1.0.0",
        "effective_session": date(2025, 12, 31),
        "decision_cutoff": cutoff,
        "execute_not_before": execute_at,
        "execution_session_sha256": "e" * 64,
        "data_use_scope": "paper_forward",
        "provider_authority_sha256": "d" * 64,
        "configuration_sha256": "a" * 64,
        "input_snapshot_sha256": "b" * 64,
        "rank_snapshot_sha256": "c" * 64,
        "intentional_cash_slots": 25,
        "legs": [],
    }
    content_sha256 = compute_model_rebalance_content_sha256(**kwargs)
    rebalance_id = compute_model_rebalance_id(
        strategy_id=kwargs["strategy_id"],
        strategy_version=kwargs["strategy_version"],
        effective_session=kwargs["effective_session"],
        execute_not_before=execute_at,
        execution_session_sha256=kwargs["execution_session_sha256"],
        data_use_scope=kwargs["data_use_scope"],
        provider_authority_sha256=kwargs["provider_authority_sha256"],
        configuration_sha256=kwargs["configuration_sha256"],
        input_snapshot_sha256=kwargs["input_snapshot_sha256"],
        content_sha256=content_sha256,
    )
    return ModelRebalanceSubmissionEvent(
        rebalance_id=rebalance_id,
        content_sha256=content_sha256,
        expected_leg_count=0,
        **kwargs,
    )


def test_http_signal_emitter_capture_only_writes_insight_signal_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "signals.jsonl"
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_ONLY", "1")
    observed: list[Signal] = []
    signal = _signal()

    emitter = HttpSignalEmitter(base_url="capture://local", on_emitted=observed.append)
    emitter.emit(signal)

    payload = _read_capture(capture_path)
    assert payload["strategy_id"] == "swing_high_low_pmo_v1"
    assert payload["symbol"] == "BTCUSD"
    assert payload["insight"]["direction"] == "Up"
    assert payload["insight"]["confidence"] == 0.75
    assert payload["context"]["entry_price"] == 100.0
    assert payload["context"]["external_signal_id"] == "external-signal-1"
    assert payload["context"]["expires_at"] == "2026-03-05T10:30:00+00:00"
    assert payload["context"]["price_source"] == "coinbase_live"
    assert payload["context"]["custom"] == "kept"
    assert observed == [signal]


def test_http_signal_emitter_does_not_report_missing_capture_as_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VM_SIGNAL_CAPTURE_PATH", raising=False)
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_ONLY", "1")
    observed: list[Signal] = []

    HttpSignalEmitter(
        base_url="capture://local",
        on_emitted=observed.append,
    ).emit(_signal())

    assert observed == []


def test_http_signal_emitter_captures_and_posts_live_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_path = tmp_path / "signals.jsonl"
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_PATH", str(capture_path))
    monkeypatch.delenv("VM_SIGNAL_CAPTURE_ONLY", raising=False)
    posts: list[dict[str, Any]] = []

    class _Response:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> _Response:
            posts.append({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)

    emitter = HttpSignalEmitter(base_url="http://scoring-engine:8001", api_key="key")
    signal = replace(_signal(), external_signal_id=None)
    emitter.emit(signal)

    payload = _read_capture(capture_path)
    expected_external_id = compute_external_signal_id(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        action=signal.action,
        bar_close_ts=signal.timestamp,
    )
    assert payload["context"]["external_signal_id"] == expected_external_id
    assert payload["context"]["external_signal_id"] != signal.signal_id
    assert posts == [
        {
            "url": "http://scoring-engine:8001/api/v1/signals",
            "json": payload,
            "headers": {"Content-Type": "application/json", "X-API-Key": "key"},
        }
    ]


def test_http_signal_emitter_retries_status_error_with_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, Any]] = []
    observed: list[Signal] = []
    request = httpx.Request("POST", "http://scoring-engine:8001/api/v1/signals")

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            attempts.append({"url": url, "json": json, "headers": headers})
            if len(attempts) == 1:
                return httpx.Response(503, request=request)
            # Empty response body is valid; the success log must not parse JSON.
            return httpx.Response(202, request=request)

    monkeypatch.setattr(httpx, "Client", _Client)
    emitter = HttpSignalEmitter(
        base_url="http://scoring-engine:8001",
        max_retries=1,
        retry_base_delay=0,
        retry_max_delay=0,
        retry_jitter=0,
        on_emitted=observed.append,
    )

    signal = _signal()
    emitter.emit(signal)

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0]["json"]["context"]["external_signal_id"] == "external-signal-1"
    assert observed == [signal]


def test_http_signal_emitter_raises_after_transport_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, Any]] = []
    observed: list[Signal] = []
    request = httpx.Request("POST", "http://scoring-engine:8001/api/v1/signals")

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            attempts.append({"url": url, "json": json, "headers": headers})
            raise httpx.ConnectError("scoring unavailable", request=request)

    monkeypatch.setattr(httpx, "Client", _Client)
    emitter = HttpSignalEmitter(
        base_url="http://scoring-engine:8001",
        max_retries=2,
        retry_base_delay=0,
        retry_max_delay=0,
        retry_jitter=0,
        on_emitted=observed.append,
    )

    with pytest.raises(httpx.ConnectError, match="scoring unavailable"):
        emitter.emit(_signal())

    assert len(attempts) == 3
    assert all(attempt == attempts[0] for attempt in attempts)
    assert observed == []


def test_model_rebalance_emitter_does_not_retry_permanent_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, Any]] = []
    request = httpx.Request(
        "POST",
        "http://scoring-engine:8001/api/v1/model-rebalances",
    )

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            attempts.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(400, request=request, json={"detail": "invalid lineage"})

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", _Client)
    emitter = HttpSignalEmitter(
        base_url="http://scoring-engine:8001",
        max_retries=3,
        retry_base_delay=0,
        retry_max_delay=0,
        retry_jitter=0,
    )

    with pytest.raises(httpx.HTTPStatusError) as caught:
        emitter.emit_model_rebalance(_model_rebalance_event())

    assert caught.value.response.status_code == 400
    assert caught.value.response.json() == {"detail": "invalid lineage"}
    assert len(attempts) == 1


def test_model_rebalance_emitter_retries_transient_503_with_identical_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[dict[str, Any]] = []
    request = httpx.Request(
        "POST",
        "http://scoring-engine:8001/api/v1/model-rebalances",
    )

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            attempts.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(503 if len(attempts) == 1 else 202, request=request)

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", _Client)
    emitter = HttpSignalEmitter(
        base_url="http://scoring-engine:8001",
        max_retries=1,
        retry_base_delay=0,
        retry_max_delay=0,
        retry_jitter=0,
    )

    event = _model_rebalance_event()
    emitter.emit_model_rebalance(event)

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0]["url"].endswith("/api/v1/model-rebalances")
    assert attempts[0]["json"] == event.model_dump(mode="json")


def test_http_signal_emitter_does_not_retry_non_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class _Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise ValueError("local payload failure")

    monkeypatch.setattr(httpx, "Client", _Client)
    emitter = HttpSignalEmitter(
        base_url="http://scoring-engine:8001",
        max_retries=3,
        retry_base_delay=0,
        retry_max_delay=0,
        retry_jitter=0,
    )

    with pytest.raises(ValueError, match="local payload failure"):
        emitter.emit(_signal())

    assert attempts == 1


def _signal_with_action(action: SignalAction) -> Signal:
    return Signal(
        strategy_id="swing_high_low_pmo_v1",
        strategy_type="indicator",
        symbol="BTCUSD",
        action=action,
        confidence=0.75,
        timestamp=datetime(2026, 3, 5, 9, 30, tzinfo=UTC),
        horizon="1D",
        entry_price=100.0,
        external_signal_id="external-signal-1",
    )


def test_http_signal_emitter_drops_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOLD is a no-op: it must not be captured or posted (M10).

    The insight wire only expresses Up/Down/Flat, so emitting HOLD would collapse
    to Flat and be mis-scored as a CLOSE (position flatten)."""
    capture_path = tmp_path / "signals.jsonl"
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_ONLY", "1")
    observed: list[Signal] = []

    HttpSignalEmitter(
        base_url="capture://local",
        on_emitted=observed.append,
    ).emit(_signal_with_action(SignalAction.HOLD))

    assert not capture_path.exists(), "HOLD must not be emitted (no-op)"
    assert observed == []


def test_http_signal_emitter_close_still_maps_to_flat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLOSE is a real flatten and must still be delivered as Flat."""
    capture_path = tmp_path / "signals.jsonl"
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_PATH", str(capture_path))
    monkeypatch.setenv("VM_SIGNAL_CAPTURE_ONLY", "1")

    HttpSignalEmitter(base_url="capture://local").emit(_signal_with_action(SignalAction.CLOSE))

    payload = _read_capture(capture_path)
    assert payload["insight"]["direction"] == "Flat"
