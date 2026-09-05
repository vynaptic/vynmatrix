from __future__ import annotations

from lib_common.internal_events import CanonicalSignalSnapshot
from lib_strategy.signals.signal import Signal


def signal_snapshot(signal: Signal) -> CanonicalSignalSnapshot:
    """Project a domain Signal into the canonical internal-event snapshot."""
    payload = signal.to_dict()
    allowed_fields = set(CanonicalSignalSnapshot.model_fields.keys())
    payload = {key: value for key, value in payload.items() if key in allowed_fields}
    payload["action"] = str(payload["action"]).lower()
    payload["metadata"] = dict(payload.get("metadata") or {})
    return CanonicalSignalSnapshot(**payload)
