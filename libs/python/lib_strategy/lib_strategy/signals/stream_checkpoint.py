"""Checkpoint a locally declared indicator graph without an indicator dependency."""

from __future__ import annotations

import copy
import math
from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

_PAIR_SIZE = 2


@runtime_checkable
class CheckpointedStream(Protocol):
    def snapshot_state(self) -> dict[str, Any]: ...

    def restore_state(self, payload: Mapping[str, Any]) -> None: ...


def _decode(value: Any, kind: str, template: Any) -> Any:
    if kind.endswith("?"):
        return None if value is None else _decode(value, kind[:-1], template)
    if kind == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            msg = "Stream checkpoint requires a finite number"
            raise ValueError(msg)
        return value
    if kind == "integer":
        if type(value) is not int or value < 0:
            msg = "Stream checkpoint requires a nonnegative integer"
            raise ValueError(msg)
        return value
    if kind == "pair":
        if not isinstance(value, (tuple, list)) or len(value) != _PAIR_SIZE:
            msg = "Stream checkpoint requires a numeric pair"
            raise ValueError(msg)
        return tuple(_decode(item, "number", None) for item in value)
    if kind in {"numbers", "pairs"}:
        if (
            not isinstance(template, deque)
            or template.maxlen is None
            or not isinstance(value, (tuple, list, deque))
            or len(value) > template.maxlen
        ):
            msg = "Stream checkpoint window exceeds its configured bound"
            raise ValueError(msg)
        item_kind = "number" if kind == "numbers" else "pair"
        return deque((_decode(item, item_kind, None) for item in value), maxlen=template.maxlen)
    msg = f"Unknown local stream checkpoint field type: {kind}"
    raise ValueError(msg)


def capture_stream(stream: dict[str, Any], fields: Mapping[str, str]) -> dict[str, Any]:
    result = {}
    if not set(fields) <= set(stream):
        msg = "Declared stream checkpoint field is absent"
        raise ValueError(msg)
    for name, current in stream.items():
        if name in fields:
            value = _decode(current, fields[name], current)
            result[name] = list(value) if isinstance(value, deque) else value
        elif isinstance(current, CheckpointedStream):
            result[name] = current.snapshot_state()
        else:
            msg = f"Stream {name!r} has no explicit checkpoint contract"
            raise ValueError(msg)
    return result


def restore_stream(
    payload: Any, template: dict[str, Any], fields: Mapping[str, str]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != set(template):
        msg = "Stream checkpoint field coverage differs"
        raise ValueError(msg)
    result = {}
    for name, current in template.items():
        if name in fields:
            result[name] = _decode(payload[name], fields[name], current)
        elif isinstance(current, CheckpointedStream):
            child = copy.deepcopy(current)
            child.restore_state(payload[name])
            result[name] = child
        else:
            msg = f"Stream {name!r} has no explicit checkpoint contract"
            raise ValueError(msg)
    return result
