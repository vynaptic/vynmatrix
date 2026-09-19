"""Explicit, versioned JSON checkpoints for bounded streaming indicators.

Only fields declared by a local indicator class are read or restored. Payloads
cannot select classes, import code, or supply arbitrary object attributes.
"""

from __future__ import annotations

import copy
import json
import math
from collections import deque
from collections.abc import Mapping
from typing import Any, ClassVar, cast


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        msg = "Indicator checkpoint requires a finite number"
        raise ValueError(msg)
    return cast(float, value)


def _value(value: Any, kind: str, limit: int) -> Any:
    if kind.endswith("?"):
        return None if value is None else _value(value, kind[:-1], limit)
    if kind == "number":
        return _number(value)
    if kind == "integer":
        if type(value) is not int or value < 0:
            msg = "Indicator checkpoint requires a nonnegative integer"
            raise ValueError(msg)
        return value
    if kind in {"pair", "triple"}:
        size = 2 if kind == "pair" else 3
        if not isinstance(value, (tuple, list)) or len(value) != size:
            msg = "Indicator checkpoint has an invalid numeric tuple"
            raise ValueError(msg)
        return tuple(_number(item) for item in value)
    if kind in {"numbers", "pairs"}:
        if not isinstance(value, (tuple, list, deque)) or len(value) > limit:
            msg = "Indicator checkpoint window exceeds its configured bound"
            raise ValueError(msg)
        item_kind = "number" if kind == "numbers" else "pair"
        return [_value(item, item_kind, limit) for item in value]
    msg = f"Unknown local indicator checkpoint field type: {kind}"
    raise ValueError(msg)


class CheckpointedIndicator:
    """Fields and immutable parameters are explicitly declared by each algorithm."""

    STATE_FIELDS: ClassVar[dict[str, str]] = {}
    STATE_CONFIG: ClassVar[tuple[str, ...]] = ()

    def snapshot_state(self) -> dict[str, Any]:
        state = {}
        for name, kind in self.STATE_FIELDS.items():
            current = getattr(self, name)
            if kind == "indicator":
                state[name] = current.snapshot_state()
            else:
                limit = current.maxlen if isinstance(current, deque) else getattr(self, "period", 0)
                if limit is None:
                    msg = "Checkpoint windows must be bounded"
                    raise ValueError(msg)
                state[name] = _value(current, kind, limit)
        payload = {
            "version": 1,
            "kind": type(self).__name__,
            "config": {name: getattr(self, name) for name in self.STATE_CONFIG},
            "state": state,
        }
        # Produce fresh JSON containers and reject nonfinite data on capture too.
        return cast(dict[str, Any], json.loads(json.dumps(payload, allow_nan=False)))

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping) or set(payload) != {
            "version",
            "kind",
            "config",
            "state",
        }:
            msg = "Malformed indicator checkpoint"
            raise ValueError(msg)
        expected_config = {name: getattr(self, name) for name in self.STATE_CONFIG}
        if (
            type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["kind"] != type(self).__name__
            or json.dumps(payload["config"], sort_keys=True)
            != json.dumps(expected_config, sort_keys=True)
        ):
            msg = "Incompatible indicator checkpoint identity or configuration"
            raise ValueError(msg)
        raw = payload["state"]
        if not isinstance(raw, Mapping) or set(raw) != set(self.STATE_FIELDS):
            msg = "Indicator checkpoint field coverage differs"
            raise ValueError(msg)
        restored = {}
        for name, kind in self.STATE_FIELDS.items():
            current = getattr(self, name)
            if kind == "indicator":
                child = copy.deepcopy(current)
                child.restore_state(raw[name])
                restored[name] = child
            else:
                limit = current.maxlen if isinstance(current, deque) else getattr(self, "period", 0)
                if limit is None:
                    msg = "Checkpoint windows must be bounded"
                    raise ValueError(msg)
                decoded = _value(raw[name], kind, limit)
                restored[name] = (
                    deque(decoded, maxlen=limit) if isinstance(current, deque) else decoded
                )
        candidate = copy.deepcopy(self)
        for name, value in restored.items():
            setattr(candidate, name, value)
        candidate.validate_checkpoint()
        for name in self.STATE_FIELDS:
            setattr(self, name, getattr(candidate, name))

    def validate_checkpoint(self) -> None:
        """Override for readiness relationships beyond field shape and bounds."""
