"""Signal utility functions.

This module provides common utilities for signal processing that are
shared across the scoring and execution engines.

These functions were consolidated from:
- apps/execution_engine/execution_engine/api.py
- apps/scoring_engine/scoring_engine/api.py
"""

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from lib_strategy.signals.signal import SignalAction


def extract_price_provenance(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract complete, non-fabricated market-data provenance.

    Runtime workers provide the canonical ``price_*`` keys. Historical callers
    may still use the underlying bar field names, so those are accepted as
    aliases. Source/timeframe are emitted only as a complete pair. Durable
    source-row identity is emitted only when price id, content revision, and
    timestamp are all valid; partial identity is omitted rather than guessed.
    """

    if not metadata:
        return {}

    def _nonempty_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    source = _nonempty_text(metadata.get("price_source")) or _nonempty_text(metadata.get("source"))
    timeframe = _nonempty_text(metadata.get("price_timeframe")) or _nonempty_text(
        metadata.get("timeframe")
    )
    if source is None or timeframe is None:
        return {}
    result: dict[str, Any] = {
        "price_source": source,
        "price_timeframe": timeframe,
    }

    price_id = metadata.get("source_price_id", metadata.get("price_id"))
    content_revision = metadata.get(
        "source_content_revision",
        metadata.get("content_revision"),
    )
    raw_timestamp = metadata.get("source_price_ts", metadata.get("price_ts"))
    if (
        isinstance(price_id, int)
        and not isinstance(price_id, bool)
        and price_id > 0
        and isinstance(content_revision, int)
        and not isinstance(content_revision, bool)
        and content_revision > 0
        and isinstance(raw_timestamp, (datetime, str))
    ):
        try:
            timestamp = (
                raw_timestamp
                if isinstance(raw_timestamp, datetime)
                else datetime.fromisoformat(raw_timestamp)
            )
        except ValueError:
            return result
        result.update(
            source_price_id=price_id,
            source_content_revision=content_revision,
            source_price_ts=ensure_utc(timestamp).isoformat(),
        )
    return result


def ensure_utc(ts: datetime) -> datetime:
    """Ensure a datetime has UTC timezone.

    If the datetime is naive (no timezone), it is assumed to be UTC and the
    timezone is attached. A timezone-aware value is converted to UTC so callers
    never persist an offset wall-clock value into a timezone-naive UTC column.

    Args:
        ts: Datetime to ensure has UTC timezone

    Returns:
        Datetime with UTC timezone

    Examples:
        >>> from datetime import datetime, timezone
        >>> naive = datetime(2024, 1, 1, 12, 0)
        >>> aware = ensure_utc(naive)
        >>> aware.tzinfo == timezone.utc
        True

        >>> already_utc = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        >>> result = ensure_utc(already_utc)
        >>> result == already_utc
        True
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def compute_stable_signal_id(
    *,
    strategy_id: str,
    symbol: str,
    action: SignalAction,
    timestamp: datetime,
    run_id: str | None = None,
    strategy_version: str | None = None,
) -> str:
    """Compute a stable, deterministic signal ID.

    Uses UUID5 with NAMESPACE_URL to generate a reproducible ID from
    the signal's key attributes. This ensures the same signal always
    gets the same ID, which is useful for:
    - Deduplication of signals
    - Correlation between signal and execution logs
    - Idempotent signal processing

    Args:
        strategy_id: Strategy identifier
        symbol: Trading symbol (e.g., "BTCUSD")
        action: Signal action (LONG, SHORT, CLOSE, HOLD)
        timestamp: Signal timestamp
        run_id: Optional run/batch identifier
        strategy_version: Optional strategy version

    Returns:
        UUID string that is deterministic for the same inputs

    Examples:
        >>> from datetime import datetime, timezone
        >>> from lib_strategy.signals.signal import SignalAction
        >>> ts = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        >>> id1 = compute_stable_signal_id(
        ...     strategy_id="my_strategy",
        ...     symbol="BTCUSD",
        ...     action=SignalAction.LONG,
        ...     timestamp=ts,
        ... )
        >>> id2 = compute_stable_signal_id(
        ...     strategy_id="my_strategy",
        ...     symbol="BTCUSD",
        ...     action=SignalAction.LONG,
        ...     timestamp=ts,
        ... )
        >>> id1 == id2  # Same inputs -> same ID
        True
    """
    base = f"{strategy_id}:{symbol}:{action.value}:{timestamp.isoformat()}"
    if run_id:
        base = f"{base}:{run_id}"
    if strategy_version:
        base = f"{base}:{strategy_version}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, base))


def compute_external_signal_id(
    *,
    strategy_id: str,
    symbol: str,
    action: SignalAction,
    bar_close_ts: datetime,
    strategy_version: str | None = None,
    reason: str | None = None,
) -> str:
    """Compute a deterministic external signal ID for canonical signal idempotency.

    This key uniquely identifies a signal decision at a specific bar close so
    that replay or retry of the same SignalWorker decision produces the same
    key and is deduplicated.

    The key is a UUID5 derived from the concatenation of:
        strategy_id + strategy_version + symbol + normalized_action + bar_close_ts + reason

    Args:
        strategy_id: Strategy identifier (e.g. "swing_high_low_pmo_v1")
        symbol: Trading symbol (e.g. "BTCUSD")
        action: Signal action enum (LONG, SHORT, CLOSE, HOLD)
        bar_close_ts: Timestamp of the bar that triggered the signal
        strategy_version: Optional strategy version string
        reason: Optional entry/exit reason for further disambiguation

    Returns:
        Deterministic UUID5 string (128 chars max, suitable for DB column)

    Examples:
        >>> from datetime import datetime, timezone
        >>> from lib_strategy.signals.signal import SignalAction
        >>> ts = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        >>> id1 = compute_external_signal_id(
        ...     strategy_id="swing_pmo_v1",
        ...     symbol="BTCUSD",
        ...     action=SignalAction.LONG,
        ...     bar_close_ts=ts,
        ... )
        >>> id2 = compute_external_signal_id(
        ...     strategy_id="swing_pmo_v1",
        ...     symbol="BTCUSD",
        ...     action=SignalAction.LONG,
        ...     bar_close_ts=ts,
        ... )
        >>> id1 == id2
        True
    """
    # Normalize action to lowercase for consistency across emitters
    action_str = action.value.lower() if isinstance(action, SignalAction) else str(action).lower()
    ts_str = bar_close_ts.isoformat()
    parts = [strategy_id, symbol, action_str, ts_str]
    if strategy_version:
        parts.append(strategy_version)
    if reason:
        parts.append(reason)
    key = ":".join(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"external_signal:{key}"))


def compute_execution_dedup_key(
    identity: str,
    user_id: str,
    broker_account_id: int,
    symbol: str,
    action: str,
) -> str:
    """Compute the canonical execution dedup / idempotency key.

    SINGLE source of truth for the ``execution_decision_logs.idempotency_key`` —
    the durable duplicate-submission guard. Both the scoring engine (which
    pre-creates the decision row) and the execution engine (which claims it
    before any broker side effect) MUST derive the key identically, or a
    redelivered command orphans the scoring row and a second order can be placed.

    ``identity`` must be the stable canonical ``external_signal_id``. Using that
    per-bar identity makes redelivery (worker restart, NOTIFY redelivery, a fresh
    ``run_id`` per POST) dedup to the same key (SC-1).

    Args:
        identity: Stable external signal identity
        user_id: User identifier
        broker_account_id: Positive linked broker-account identifier
        symbol: Trading symbol
        action: Lowercase signal action ("long"/"short"/"close"/...)

    Returns:
        Key of the form
        ``exec:<sha256(identity:user_id:broker_account_id:symbol:action)[:32]>``
    """
    if (
        isinstance(broker_account_id, bool)
        or not isinstance(broker_account_id, int)
        or broker_account_id <= 0
    ):
        msg = "Execution dedup broker_account_id must be a positive integer"
        raise ValueError(msg)
    components = {
        "identity": identity,
        "user_id": user_id,
        "symbol": symbol,
        "action": action,
    }
    normalized: dict[str, str] = {}
    for name, value in components.items():
        stripped = value.strip()
        if not stripped:
            msg = f"Execution dedup {name} must be non-empty"
            raise ValueError(msg)
        normalized[name] = stripped
    composite = (
        f"{normalized['identity']}:{normalized['user_id']}:"
        f"{broker_account_id}:{normalized['symbol']}:{normalized['action']}"
    )
    key_hash = hashlib.sha256(composite.encode()).hexdigest()[:32]
    return f"exec:{key_hash}"


def normalize_signal_api_base_url(raw_url: str) -> str:
    """Normalize a scoring-engine base URL when callers pass the signals endpoint."""
    url = raw_url.rstrip("/")
    suffix = "/api/v1/signals"
    if url.endswith(suffix):
        return url[: -len(suffix)]
    return url


__all__ = [
    "compute_external_signal_id",
    "compute_stable_signal_id",
    "ensure_utc",
    "extract_price_provenance",
    "normalize_signal_api_base_url",
]
