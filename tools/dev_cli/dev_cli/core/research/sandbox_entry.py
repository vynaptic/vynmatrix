"""Isolated stdin/stdout worker; never import this module in the backend.

The trusted controller verifies Docker containment before supplying the first
message. This entrypoint intentionally has no database or broker integration.
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import datetime
from typing import Any, NoReturn

from lib_strategy.signals.pure_strategy import MarketState, PureSignalStrategy

MESSAGE_BYTES = 65_536
SOURCE_BYTES = 262_144
MAX_BARS = 100_000
MAX_WARMUP = 10_000
MAX_SIGNALS_PER_BAR = 8


def _refuse(message: str) -> NoReturn:
    """Keep malformed wire data under the explicit ValueError contract."""
    raise ValueError(message)


def _reject_constant(value: str) -> None:
    msg = f"Nonfinite JSON value: {value}"
    raise ValueError(msg)


def read_message() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MESSAGE_BYTES + 1)
    if not raw or len(raw) > MESSAGE_BYTES or not raw.endswith(b"\n"):
        msg = "Missing or oversized research protocol message"
        raise ValueError(msg)
    message = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(message, dict):
        _refuse("Research protocol requires a JSON object")
    return message


def write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n"
    if len(encoded.encode()) > MESSAGE_BYTES:
        msg = "Research result exceeds protocol message bound"
        raise ValueError(msg)
    sys.stdout.write(encoded)
    sys.stdout.flush()


def _result(value: dict[str, Any]) -> None:
    predictions = value.get("predictions", [])
    if not isinstance(predictions, list):
        _refuse("Predictions must be a bounded list")
    for start in range(0, len(predictions), 100):
        write_message(
            {"kind": "part", "field": "predictions", "rows": predictions[start : start + 100]}
        )
    write_message(
        {
            "kind": "result",
            "value": {**value, **({"predictions": []} if "predictions" in value else {})},
        }
    )


def _source() -> str:
    source = bytearray()
    while True:
        message = read_message()
        if message.get("kind") == "start":
            break
        if (
            set(message) != {"kind", "text"}
            or message["kind"] != "source"
            or not isinstance(message["text"], str)
        ):
            msg = "Expected bounded strategy source chunks"
            raise ValueError(msg)
        source.extend(message["text"].encode("utf-8"))
        if len(source) > SOURCE_BYTES:
            msg = "Strategy source exceeds 256 KiB"
            raise ValueError(msg)
    return source.decode("utf-8")


def _core(config: dict[str, Any]) -> PureSignalStrategy:
    source = _source()
    namespace: dict[str, Any] = {"__name__": "research_user_core"}
    with contextlib.redirect_stdout(sys.stderr):
        exec(compile(source, "<research-user-core>", "exec"), namespace)
        core = namespace.get(config["class_name"])
        if not isinstance(core, type) or not issubclass(core, PureSignalStrategy):
            _refuse("UserStrategy must subclass the canonical PureSignalStrategy")
        strategy = core(
            strategy_id=config["strategy_id"],
            strategy_type="indicator",
            config=config["parameters"],
        )
        strategy.run([])
    warmup = strategy.warmup_bars_needed
    if type(warmup) is not int or not 0 <= warmup <= MAX_WARMUP:
        msg = "Strategy warmup must be an integer in [0, 10000]"
        raise ValueError(msg)
    return strategy


def _signals(strategy: PureSignalStrategy, message: dict[str, Any]) -> list[dict[str, Any]]:
    bar = message["bar"]
    at = datetime.fromisoformat(bar["ts"].replace("Z", "+00:00"))
    if at.tzinfo is None:
        msg = "Research bar timestamp must be timezone aware"
        raise ValueError(msg)
    state = MarketState(
        symbol=bar["symbol"],
        timestamp=at,
        open=float(bar["open"]),
        high=float(bar["high"]),
        low=float(bar["low"]),
        close=float(bar["close"]),
        volume=float(bar["volume"]),
        metadata=bar.get("metadata", {}),
    )
    with contextlib.redirect_stdout(sys.stderr):
        if message["warmup"]:
            strategy.bootstrap_history([state])
            return []
        signals = strategy.run([state])
    if len(signals) > MAX_SIGNALS_PER_BAR:
        msg = "Per-bar signal limit exceeded"
        raise ValueError(msg)
    return [
        {
            "action": item.action.value,
            "symbol": item.symbol,
            "timestamp": item.timestamp.isoformat(),
            "confidence": item.confidence,
            "entry_price": item.entry_price,
            "stop_loss": item.stop_loss,
            "take_profit": item.take_profit,
            "expected_return": item.expected_return,
            "predicted_risk": item.predicted_risk,
            "horizon": item.horizon,
            "horizon_days": item.horizon_days,
        }
        for item in signals
    ]


def _run_strategy(initial: dict[str, Any]) -> None:
    strategy = _core(initial["config"])
    if initial["mode"] == "validate_strategy":
        _result(
            {
                "valid": True,
                "warmup_bars": strategy.warmup_bars_needed,
                "scope": "isolated syntax and canonical API checks; no release authority",
            }
        )
        return
    write_message({"kind": "ready", "warmup_bars": strategy.warmup_bars_needed})
    for sequence in range(MAX_BARS + 1):
        message = read_message()
        if message.get("kind") == "end":
            return
        if (
            sequence == MAX_BARS
            or message.get("kind") != "bar"
            or message.get("sequence") != sequence
        ):
            msg = "Research bar sequence/count invalid"
            raise ValueError(msg)
        write_message({"sequence": sequence, "signals": _signals(strategy, message)})


def _run_model(initial: dict[str, Any]) -> None:
    # Pure numerical code from the stamped image; no source upload is accepted.
    from lib_strategy.research.linear_model import (  # noqa: PLC0415 - load NumPy only for model jobs
        predict_return_model,
        train_return_model,
    )

    bars = []
    while True:
        message = read_message()
        if message.get("kind") == "compute":
            break
        if message.get("kind") != "rows" or not isinstance(message.get("bars"), list):
            msg = "Expected bounded recorded bar chunks"
            raise ValueError(msg)
        bars.extend(message["bars"])
        if len(bars) > MAX_BARS:
            msg = "Research dataset exceeds 100000 bars"
            raise ValueError(msg)
    if initial["mode"] == "train":
        result = train_return_model(bars, horizon=initial["horizon"], identity=initial["identity"])
    else:
        result = predict_return_model(
            initial["model"], bars, evaluation_start=initial.get("evaluation_start")
        )
    _result(result)


def main() -> None:
    initial = read_message()
    if initial.get("kind") != "init":
        msg = "Research protocol must initialize first"
        raise ValueError(msg)
    mode = initial.get("mode")
    if mode in {"strategy", "validate_strategy"}:
        _run_strategy(initial)
    elif mode in {"train", "predict"}:
        _run_model(initial)
    else:
        msg = "Unsupported research sandbox mode"
        raise ValueError(msg)


if __name__ == "__main__":
    main()
