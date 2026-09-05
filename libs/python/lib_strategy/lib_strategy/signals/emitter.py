"""
SignalEmitter interface and implementations.

Provides abstraction for emitting signals to different backends:
- BacktestSignalEmitter: Collects signals in memory for backtesting
- LogSignalEmitter: Logs signals for debugging/development
- HttpSignalEmitter: Delivers canonical signals to the scoring API

This enables the same strategy code to work in backtest, paper, and live modes.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lib_common.env_utils import parse_bool_env
from lib_common.http_exceptions import is_retryable_status
from lib_common.internal_events import ModelRebalanceSubmissionEvent
from lib_common.logging import get_logger
from lib_common.retries import retry_sync
from lib_common.time_utils import now_utc
from lib_strategy.signals.signal import Signal, SignalAction
from lib_strategy.signals.utils import compute_external_signal_id

# ``logging`` import is retained because :class:`LogSignalEmitter` accepts
# ``logging.INFO`` (etc.) as a default level and supports a ``logger_name``
# override that wraps a stdlib logger directly (line ~226).
logger = get_logger(__name__)


class _RetryableHttpStatusError(RuntimeError):
    """Internal retry marker that retains the original httpx response error."""

    def __init__(self, http_error: BaseException) -> None:
        super().__init__(str(http_error))
        self.http_error = http_error


def _check_http_response(response: Any, *, httpx_module: Any) -> None:
    """Retry only transient statuses while retaining the original response."""

    try:
        response.raise_for_status()
    except httpx_module.HTTPStatusError as exc:
        status_code = int(response.status_code)
        if is_retryable_status(status_code) or status_code in {408, 425}:
            raise _RetryableHttpStatusError(exc) from exc
        raise


class SignalEmitter(ABC):
    """
    Abstract interface for emitting trading signals.

    Strategies use this to emit signals without knowing the destination.
    Different implementations route signals to different backends.
    """

    @abstractmethod
    def emit(self, signal: Signal) -> None:
        """
        Emit a single signal.

        Args:
            signal: The Signal to emit

        Raises:
            SignalEmissionError: If emission fails
        """

    @abstractmethod
    def flush(self) -> None:
        """
        Flush any buffered signals.

        Called at end of processing cycle or before shutdown.
        """

    def emit_batch(self, signals: list[Signal]) -> None:
        """
        Emit multiple signals.

        Default implementation emits one by one.
        Override for batch-optimized backends.

        Args:
            signals: List of Signals to emit
        """
        for signal in signals:
            self.emit(signal)
        self.flush()

    def emit_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Emit one indivisible cross-sectional model-rebalance event.

        A portfolio batch cannot be degraded into independent signal calls:
        downstream scoring must validate and persist the complete ordered model
        decision before producing any tenant/account execution plan.
        """

        message = f"{type(self).__name__} does not support atomic model-rebalance delivery"
        raise NotImplementedError(message)


class BacktestSignalEmitter(SignalEmitter):
    """
    Collects signals in memory for backtesting.

    Signals are stored in a list and can be retrieved after backtest
    for analysis, performance calculation, or export.

    Example:
        >>> emitter = BacktestSignalEmitter()
        >>> strategy.run(data, emitter)
        >>> signals = emitter.get_signals()
        >>> print(f"Generated {len(signals)} signals")
    """

    def __init__(self) -> None:
        """Initialize with empty signal list."""
        self._signals: list[Signal] = []
        self._model_rebalances: list[ModelRebalanceSubmissionEvent] = []

    def emit(self, signal: Signal) -> None:
        """Store signal in memory."""
        self._signals.append(signal)
        logger.debug(
            "Backtest signal: %s %s %s @ %.2f (conf: %.2f)",
            signal.action.value,
            signal.symbol,
            signal.strategy_id,
            signal.entry_price or 0.0,
            signal.confidence,
        )

    def flush(self) -> None:
        """No-op for in-memory storage."""

    def emit_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Store one validated atomic rebalance for deterministic backtests."""

        self._model_rebalances.append(event)

    def get_model_rebalances(self) -> list[ModelRebalanceSubmissionEvent]:
        """Return collected model rebalance batches in delivery order."""

        return list(self._model_rebalances)

    def get_signals(self) -> list[Signal]:
        """Get all collected signals."""
        return list(self._signals)

    def clear(self) -> None:
        """Clear all collected signals."""
        self._signals.clear()
        self._model_rebalances.clear()

    def get_signals_for_symbol(self, symbol: str) -> list[Signal]:
        """Get signals for a specific symbol."""
        return [s for s in self._signals if s.symbol == symbol]

    def get_entry_signals(self) -> list[Signal]:
        """Get only entry signals (LONG/SHORT)."""
        return [s for s in self._signals if s.is_entry]

    def get_exit_signals(self) -> list[Signal]:
        """Get only exit signals (CLOSE)."""
        return [s for s in self._signals if s.is_exit]

    def to_dataframe(self) -> Any:
        """Convert signals to pandas DataFrame (if pandas available)."""
        try:
            import pandas as pd  # noqa: PLC0415

            return pd.DataFrame([s.to_dict() for s in self._signals])
        except ImportError:
            msg = "pandas is required for to_dataframe()"
            raise ImportError(msg)  # noqa: B904


class LogSignalEmitter(SignalEmitter):
    """
    Logs signals for debugging and development.

    Useful for:
    - Strategy development without live infrastructure
    - Debugging signal generation logic
    - Dry-run testing

    Example:
        >>> emitter = LogSignalEmitter(level=logging.INFO)
        >>> strategy.run(data, emitter)  # Signals logged but not executed
    """

    def __init__(
        self,
        level: int = logging.INFO,
        logger_name: str | None = None,
    ) -> None:
        """
        Initialize log emitter.

        Args:
            level: Logging level (default: INFO)
            logger_name: Logger name (default: module logger)
        """
        self._level = level
        # Use the canonical wrapper for the override path too so the public API
        # consistently returns a structured logger regardless of whether the
        # caller provided a custom name.
        self._logger = get_logger(logger_name) if logger_name else logger

    def emit(self, signal: Signal) -> None:
        """Log signal details."""
        entry_str = f"@ {signal.entry_price:.4f}" if signal.entry_price else ""
        sl_str = f"SL: {signal.stop_loss:.4f}" if signal.stop_loss else ""
        tp_str = f"TP: {signal.take_profit:.4f}" if signal.take_profit else ""

        self._logger.log(
            self._level,
            "[SIGNAL] %s | %s %s %s | Conf: %.2f | %s %s %s | %s",
            signal.strategy_id,
            signal.action.value,
            signal.symbol,
            entry_str,
            signal.confidence,
            sl_str,
            tp_str,
            signal.horizon or "",
            signal.signal_id[:8],
        )

    def flush(self) -> None:
        """No-op for logging."""

    def emit_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Log the atomic batch identity without expanding it into signals."""

        self._logger.log(
            self._level,
            "[MODEL_REBALANCE] %s | %s | legs=%d | cash_slots=%d",
            event.strategy_id,
            event.rebalance_id,
            event.expected_leg_count,
            event.intentional_cash_slots,
        )


class HttpSignalEmitter(SignalEmitter):
    """
    Posts canonical signals to the scoring engine ingestion endpoint.

    Example:
        >>> emitter = HttpSignalEmitter(
        ...     base_url="http://localhost:8000",
        ...     api_key="secret",
        ... )
        >>> emitter.emit(signal)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 10.0,
        user_id: str | None = None,
        profile: dict[str, Any] | None = None,
        user_strategy_config: dict[str, Any] | None = None,
        strategy_type: str = "indicator",
        max_retries: int = 2,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 5.0,
        retry_jitter: float = 0.1,
        on_emitted: Callable[[Signal], None] | None = None,
    ) -> None:
        """
        Initialize HTTP emitter.

        ``HttpSignalEmitter`` is the supported HTTP path for emitting signals
        from SignalWorker to the scoring engine ``/api/v1/signals`` endpoint.

        Args:
            base_url: Base URL of scoring engine (e.g., http://localhost:8001)
            api_key: Optional API key for X-API-Key authentication
            timeout: Request timeout in seconds
            user_id: User ID for execution context
            profile: User profile for execution context
            user_strategy_config: Strategy config for execution context
            strategy_type: Strategy type (currently ``indicator``)
            max_retries: Retries after the initial HTTP attempt
            retry_base_delay: Initial retry delay in seconds
            retry_max_delay: Maximum retry delay in seconds
            retry_jitter: Fractional jitter applied to retry delays
            on_emitted: Optional observer invoked after a non-HOLD signal is
                captured or successfully accepted by the scoring endpoint
        """
        if max_retries < 0:
            msg = "max_retries must be nonnegative"
            raise ValueError(msg)
        if retry_base_delay < 0 or retry_max_delay < 0:
            msg = "retry delays must be nonnegative"
            raise ValueError(msg)
        self._client: Any | None = None
        if not 0 <= retry_jitter <= 1:
            msg = "retry_jitter must be between 0 and 1"
            raise ValueError(msg)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._user_id = user_id or "default_user"
        self._profile = profile or {}
        self._user_strategy_config = user_strategy_config or {}
        self._strategy_type = strategy_type
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._retry_jitter = retry_jitter
        self._on_emitted = on_emitted

    @staticmethod
    def _action_to_direction(action: SignalAction) -> str:
        """Map a canonical action to the scoring insight direction."""
        _MAP = {"LONG": "Up", "SHORT": "Down", "CLOSE": "Flat", "HOLD": "Flat"}  # noqa: N806
        return _MAP.get(action.value, "Flat")

    def _capture_only_enabled(self) -> bool:
        return parse_bool_env(
            "VM_SIGNAL_CAPTURE_ONLY",
            default=False,
            logger=logger,
        ) or self._base_url.startswith("capture://")

    def _capture_payload(
        self,
        payload: dict[str, Any],
        *,
        record_type: str = "signal",
    ) -> bool:
        capture_path = os.environ.get("VM_SIGNAL_CAPTURE_PATH")
        if not capture_path:
            return False

        try:
            path = Path(capture_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "captured_at": now_utc().isoformat(),
                record_type: payload,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record))
                handle.write("\n")
        except Exception as exc:
            logger.warning("Failed to capture signal payload: %s", exc)
            return False
        return True

    def _build_payload(self, signal: Signal) -> dict[str, Any]:
        # Build the InsightSignalPayload body expected by /api/v1/signals.
        # The scoring endpoint reconstructs Signal fields from "context", so we
        # must forward ALL signal attributes that the internal Signal model uses.
        context: dict[str, Any] = {
            "strategy_type": self._strategy_type,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "size_hint": signal.size_hint,
            "entry_price": signal.entry_price,
            "expected_return": signal.expected_return,
            "predicted_risk": signal.predicted_risk,
            "asset_class": signal.asset_class,
            "sector": signal.sector,
            "industry": signal.industry,
            "index": signal.index,
            "instrument_id": signal.instrument_id,
            "strategy_version": signal.strategy_version,
            "horizon_days": signal.horizon_days,
            "source": signal.source,
            # HTTP retries carry one deterministic per-bar ingest identity even
            # for direct Signal callers outside PureSignalStrategy.
            "external_signal_id": signal.external_signal_id
            or compute_external_signal_id(
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                symbol=signal.symbol,
                action=signal.action,
                bar_close_ts=signal.timestamp,
                reason=(
                    signal.metadata.get("entry_reason")
                    or signal.metadata.get("exit_reason")
                    or signal.metadata.get("reason")
                ),
            ),
            "expires_at": signal.expires_at.isoformat() if signal.expires_at else None,
        }
        # Merge caller-supplied metadata (signal.metadata) without overwriting
        # the structured fields above.
        if signal.metadata:
            for k, v in signal.metadata.items():
                if k not in context or context[k] is None:
                    context[k] = v

        # insight.magnitude is used by scoring as the fallback for expected_return
        # (when context.expected_return is absent), so send the real expected_return
        # if available, otherwise 0.0. Confidence goes only in insight.confidence.
        magnitude = 0.0
        er = signal.expected_return
        if er is not None:
            magnitude = float(er)

        return {
            "ts": signal.timestamp.isoformat(),
            "strategy_id": signal.strategy_id,
            "symbol": signal.symbol,
            "insight": {
                "direction": self._action_to_direction(signal.action),
                "magnitude": magnitude,
                "confidence": signal.confidence,
                "horizon": signal.horizon or "1D",
            },
            "run_id": signal.run_id,
            "context": {k: v for k, v in context.items() if v is not None},
        }

    def emit(self, signal: Signal) -> None:
        """Post a provider-neutral insight payload to scoring /api/v1/signals."""
        # HOLD means "no action, maintain current state". The insight wire only
        # expresses Up/Down/Flat, so a HOLD would collapse to Flat and be
        # mis-scored downstream as a CLOSE (position flatten). Drop it at the
        # boundary instead of emitting a spurious flatten — CLOSE still maps to
        # Flat and is delivered normally.
        if signal.action == SignalAction.HOLD:
            logger.debug("Skipping HOLD signal %s (no-op, not emitted)", signal.signal_id[:8])
            return
        payload = self._build_payload(signal)
        captured = self._capture_payload(payload)

        if self._capture_only_enabled():
            if captured:
                logger.info("Captured signal %s without HTTP delivery", signal.signal_id[:8])
                self._notify_emitted(signal)
            else:
                logger.warning(
                    "Capture-only signal %s was not written; no capture path is available",
                    signal.signal_id[:8],
                )
            return

        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            msg = "httpx is required for HttpSignalEmitter. Install with: pip install httpx"
            raise ImportError(msg)  # noqa: B904

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        endpoint = f"{self._base_url}/api/v1/signals"

        # One pooled client per emitter: the durable relay retries deliveries
        # on a hot path, and a fresh Client per attempt paid a new TCP/TLS
        # handshake every time.
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self._timeout)
            self._client = client

        def _post() -> Any:
            response = client.post(endpoint, json=payload, headers=headers)
            _check_http_response(response, httpx_module=httpx)
            return response

        # This is bounded, fail-visible retry rather than durable delivery. The
        # same payload/idempotency identity is reused for every attempt. Only
        # transport failures and HTTP status failures are retried; serialization,
        # programming, and other local errors fail immediately.
        try:
            response = retry_sync(
                _post,
                retries=self._max_retries,
                base_delay=self._retry_base_delay,
                max_delay=self._retry_max_delay,
                jitter=self._retry_jitter,
                retry_on=(httpx.TransportError, _RetryableHttpStatusError),
                operation_name="signal HTTP POST",
                log=logger,
            )
        except _RetryableHttpStatusError as exc:
            raise exc.http_error from exc
        # Do not parse the response body solely for logging: successful endpoints
        # may legitimately return an empty/non-JSON body.
        logger.info(
            "Posted signal",
            signal_id=signal.signal_id[:8],
            external_signal_id=payload["context"]["external_signal_id"],
            destination=endpoint,
            status_code=getattr(response, "status_code", None),
        )
        self._notify_emitted(signal)

    def emit_model_rebalance(self, event: ModelRebalanceSubmissionEvent) -> None:
        """Post one typed model batch to scoring without per-leg reconstruction."""

        payload = event.model_dump(mode="json")
        captured = self._capture_payload(payload, record_type="model_rebalance")

        if self._capture_only_enabled():
            if captured:
                logger.info(
                    "Captured model rebalance without HTTP delivery",
                    rebalance_id=event.rebalance_id,
                    expected_leg_count=event.expected_leg_count,
                )
            else:
                logger.warning(
                    "Capture-only model rebalance was not written; no capture path is available",
                    rebalance_id=event.rebalance_id,
                )
            return

        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            msg = "httpx is required for HttpSignalEmitter. Install with: pip install httpx"
            raise ImportError(msg)  # noqa: B904

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        endpoint = f"{self._base_url}/api/v1/model-rebalances"
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self._timeout)
            self._client = client

        def _post() -> Any:
            response = client.post(endpoint, json=payload, headers=headers)
            _check_http_response(response, httpx_module=httpx)
            return response

        try:
            response = retry_sync(
                _post,
                retries=self._max_retries,
                base_delay=self._retry_base_delay,
                max_delay=self._retry_max_delay,
                jitter=self._retry_jitter,
                retry_on=(httpx.TransportError, _RetryableHttpStatusError),
                operation_name="model rebalance HTTP POST",
                log=logger,
            )
        except _RetryableHttpStatusError as exc:
            raise exc.http_error from exc
        logger.info(
            "Posted model rebalance",
            rebalance_id=event.rebalance_id,
            expected_leg_count=event.expected_leg_count,
            destination=endpoint,
            status_code=getattr(response, "status_code", None),
        )

    def close(self) -> None:
        """Close the pooled HTTP client (safe to call multiple times)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _notify_emitted(self, signal: Signal) -> None:
        """Notify the runtime only after a canonical emission succeeds."""
        if self._on_emitted is not None:
            self._on_emitted(signal)

    def flush(self) -> None:
        """No-op for HTTP (signals sent immediately)."""
