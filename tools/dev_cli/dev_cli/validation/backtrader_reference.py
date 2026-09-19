"""Capture reviewed Backtrader classes and native decisions on recorded OHLCV.

Run with the repository's strategy-validation interpreter. Source imports,
feed classes and download/plot harnesses are excluded, while strategy and
indicator class bodies remain unmodified. This is behavioral evidence, not
an economic attestation or authority to activate a strategy.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib
import io
import math
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_cli.validation.evidence import (
    evidence_sha256,
    file_sha256,
    load_json_object,
    require_descendant,
    write_content_addressed_manifest,
)
from lib_common.runner_utils import build_strategy_core_parameters
from lib_data.bars import ohlcv_invariant_error
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.pure_strategy import MarketState


def extract_source_classes(path: Path, expected_sha256: str) -> tuple[ast.Module, str]:
    """Extract one hash-bound strategy and its direct Backtrader indicator helpers."""
    if file_sha256(path) != expected_sha256:
        msg = f"Source hash mismatch: {path.name}"
        raise ValueError(msg)
    nodes: list[ast.stmt] = []
    strategies = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {ast.unparse(base) for base in node.bases}
        if "bt.Strategy" in bases:
            strategies.append(node.name)
            nodes.append(node)
        elif "bt.Indicator" in bases:
            nodes.append(node)
    if len(strategies) != 1:
        msg = f"Reference requires exactly one direct bt.Strategy: {path.name}"
        raise ValueError(msg)
    return ast.Module(body=nodes, type_ignores=[]), strategies[0]


def load_recorded_bars(path: Path) -> dict[str, Any]:
    """Read the repository's provenance-bearing frozen OHLCV fixture format."""
    payload = load_json_object(path)
    if (
        not isinstance(payload.get("source"), str)
        or not payload["source"]
        or not isinstance(payload.get("product"), str)
        or not payload["product"]
        or type(payload.get("granularity_seconds")) is not int
        or payload["granularity_seconds"] <= 0
        or not isinstance(payload.get("bars"), list)
        or not payload["bars"]
    ):
        msg = "Recorded input requires source, product, granularity_seconds and bars"
        raise ValueError(msg)
    previous = None
    for row in payload["bars"]:
        timestamp = row.get("ts")
        if type(timestamp) is not int or (previous is not None and timestamp <= previous):
            msg = "Recorded input must have strictly chronological integer timestamps"
            raise ValueError(msg)
        error = ohlcv_invariant_error(
            open_price=row.get("open"),
            high=row.get("high"),
            low=row.get("low"),
            close=row.get("close"),
            volume=row.get("volume"),
        )
        if error is not None:
            msg = f"Recorded input has invalid OHLCV at {timestamp}: {error}"
            raise ValueError(msg)
        previous = timestamp
    return payload


def package_identity(module: Any) -> dict[str, Any]:
    """Bind loaded Python and compiled package code, excluding incidental caches."""
    root = Path(module.__file__).resolve().parent
    files = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".so", ".pyd", ".dll", ".dylib"}
    }
    return {"file_count": len(files), "code_sha256": evidence_sha256(files)}


def _source_trace(instance: Any, name: str) -> dict[str, Any]:
    values: dict[str, Any] = {"index": len(instance) - 1}
    if name == "WilliamsPullback":
        values.update(
            macd_line=instance.macd_histo.macd[0],
            histogram=instance.macd_histo.histo[0],
            williams=instance.wr[0],
            trend_ema=instance.trend_ema[0],
            buy_cross=instance.buy_signal[0],
            sell_cross=instance.sell_signal[0],
        )
    elif name == "BBSqueezeBreakout":
        values.update(
            rank=instance.bw_pct_rank[0],
            previous_rank=instance.bw_pct_rank[-1],
            obv=instance.obv[0],
            obv_sma=instance.obv_sma[0],
        )
    elif name == "TimeDecayAdaptiveEMA":
        values.update(
            pending_order=bool(instance.order),
            protective_order=bool(instance.trail_order),
            filter_samples=len(instance.td_aema_values),
            position=instance.position.size,
        )
    else:
        return {}
    return {
        key: value if not isinstance(value, float) or math.isfinite(value) else None
        for key, value in values.items()
    }


def _capture_source(
    bt: Any,
    np: Any,
    tree: ast.Module,
    class_name: str,
    *,
    path: Path,
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    module_name = f"_vynmatrix_reference_{name}"
    module = types.ModuleType(module_name)
    module.__dict__.update(bt=bt, btind=bt.indicators, np=np)
    sys.modules[module_name] = module
    try:
        exec(compile(tree, str(path), "exec"), module.__dict__)
        original = getattr(module, class_name)
        events: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        first_next: list[int] = []
        order_ids: dict[int, int] = {}

        def order_id(order: Any) -> int | None:
            if order is None:
                return None
            if order.ref not in order_ids:
                order_ids[order.ref] = len(order_ids) + 1
            return order_ids[order.ref]

        def next_capture(self: Any) -> None:
            if not first_next:
                first_next.append(len(self) - 1)
            original.next(self)
            trace = _source_trace(self, name)
            if trace:
                traces.append(trace)

        def order_capture(method: Any, action: str) -> Any:
            def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
                nested = getattr(self, "_reference_in_close", False)
                if action == "close":
                    self._reference_in_close = True
                try:
                    order = method(self, *args, **kwargs)
                finally:
                    if action == "close":
                        self._reference_in_close = nested
                if not nested:
                    events.append(
                        {
                            "index": len(self) - 1,
                            "action": action,
                            "order_type": bt.Order.ExecTypes[
                                kwargs.get("exectype") or bt.Order.Market
                            ],
                            "position": self.position.size,
                            "submitted": order is not None,
                            "order_id": order_id(order),
                        }
                    )
                return order

            return wrapped

        def notify_capture(self: Any, order: Any) -> None:
            if order.status == order.Completed:
                completed.append(
                    {
                        "index": len(self) - 1,
                        "side": "buy" if order.isbuy() else "sell",
                        "order_type": bt.Order.ExecTypes[order.exectype],
                        "order_id": order_id(order),
                        "price": order.executed.price,
                        "executed_size": order.executed.size,
                        "post_fill_position": order.executed.psize,
                        "position_at_notification": self.position.size,
                    }
                )
            original.notify_order(self, order)

        captured = type(
            f"Captured{name}",
            (original,),
            {
                "__module__": module_name,
                "next": next_capture,
                "buy": order_capture(bt.Strategy.buy, "buy"),
                "sell": order_capture(bt.Strategy.sell, "sell"),
                "close": order_capture(bt.Strategy.close, "close"),
                "notify_order": notify_capture,
            },
        )
        # PandasData is the source harness's standard chronological OHLCV feed.
        pd = importlib.import_module("pandas")
        frame = pd.DataFrame(payload["bars"])
        frame.index = pd.to_datetime(frame.pop("ts"), unit="s", utc=True)
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(1_000_000)
        cerebro.adddata(
            bt.feeds.PandasData(
                dataname=frame,
                timeframe=bt.TimeFrame.Seconds,
                compression=payload["granularity_seconds"],
            )
        )
        overrides = {
            key: False for key in ("printlog", "debug") if key in original.params._getkeys()
        }
        cerebro.addstrategy(captured, **overrides)
        with contextlib.redirect_stdout(io.StringIO()):
            cerebro.run(runonce=True, preload=True)
        return {
            "parameters": dict(original.params._getitems()),
            "logging_overrides": overrides,
            "first_next_index": first_next[0] if first_next else None,
            "events": events,
            "completed": completed,
            "traces": traces,
        }
    finally:
        sys.modules.pop(module_name, None)


def capture_reference(*, repo_root: Path, source_dir: Path, input_path: Path) -> dict[str, Any]:
    """Capture every explicitly source-bound native port on identical real inputs."""
    bt = importlib.import_module("backtrader")
    np = importlib.import_module("numpy")
    talib = importlib.import_module("talib")
    if bt.__version__ != "1.9.78.123" or talib.__version__ != "0.6.8":
        msg = "Use the pinned Backtrader/TA-Lib strategy-validation environment"
        raise ValueError(msg)
    payload = load_recorded_bars(input_path)
    records = []
    bars = [
        MarketState(
            symbol=payload["product"],
            timestamp=datetime.fromtimestamp(row["ts"], UTC),
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
        )
        for row in payload["bars"]
    ]
    indexes = {bar.timestamp: index for index, bar in enumerate(bars)}
    for config_path in sorted((repo_root / "strategies/indicator").glob("*/config.json")):
        config = load_json_object(config_path)
        metadata = config.get("metadata", {})
        if "source_file" not in metadata:
            continue
        path = require_descendant(source_dir / metadata["source_file"], source_dir, field="source")
        tree, class_name = extract_source_classes(path, metadata["source_sha256"])
        name = config_path.parent.name
        source = _capture_source(bt, np, tree, class_name, path=path, name=name, payload=payload)
        native = load_pure_strategy_core(config_path.parent)(
            strategy_id=config["strategy_id"],
            strategy_type="indicator",
            config=build_strategy_core_parameters(config),
        )
        signals = native.run(bars)
        records.append(
            {
                "name": name,
                "source_file": path.name,
                "source_sha256": file_sha256(path),
                "source_class": class_name,
                "config_sha256": file_sha256(config_path),
                "native_core_sha256": file_sha256(config_path.parent / "core.py"),
                "native_warmup_bars": native.warmup_bars_needed,
                "source": source,
                "native_events": [
                    {
                        "index": indexes[signal.timestamp],
                        "action": signal.action.value,
                        "entry_price": signal.entry_price,
                        "stop_loss": signal.stop_loss,
                    }
                    for signal in signals
                ],
            }
        )
    if not records:
        msg = "No source-bound native configurations found"
        raise ValueError(msg)
    return {
        "schema_version": 1,
        "scope": "Source/native component comparison; no performance or daily-feed qualification",
        "capture_sha256": file_sha256(Path(__file__)),
        "environment": {
            "backtrader": bt.__version__,
            "numpy": np.__version__,
            "ta_lib": talib.__version__,
            "ta_c_library": talib.__ta_version__.decode(),
            "python": sys.version.split()[0],
            "loaded_code": {
                name: package_identity(importlib.import_module(name))
                for name in (
                    "backtrader",
                    "talib",
                    "numpy",
                    "pandas",
                    "lib_common",
                    "lib_data",
                    "lib_strategy",
                    "lib_indicators",
                )
            },
        },
        "execution": {
            "runonce": True,
            "preload": True,
            "initial_cash": 1_000_000,
            "commission": 0,
            "slippage": 0,
            "cheat_on_close": False,
        },
        "input": {
            "name": input_path.name,
            "sha256": file_sha256(input_path),
            "source": payload["source"],
            "product": payload["product"],
            "granularity_seconds": payload["granularity_seconds"],
            "bar_count": len(bars),
        },
        "strategies": records,
    }


def reference_witnesses(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep independent source decisions and bounded defect witnesses for unit tests."""
    strategies = []
    checkpoints = {35, 50, 58, 59, 127, 255, 511, 767, 1023, 1279, 1500}
    actions = {"buy": "LONG", "sell": "SHORT", "close": "CLOSE"}
    for record in payload["strategies"]:
        source = record["source"]
        decisions = [
            [event["index"], actions[event["action"]]]
            for event in source["events"]
            if event["order_type"] == "Market" and event["submitted"]
        ]
        row = {
            "name": record["name"],
            "source_sha256": record["source_sha256"],
            "config_sha256": record["config_sha256"],
            "source_parameters": source["parameters"],
            "first_next_index": source["first_next_index"],
            "source_decisions": decisions,
            "trace_checkpoints": [
                trace for trace in source["traces"] if trace["index"] in checkpoints
            ],
            "fill_witnesses": [
                fill for fill in source["completed"] if fill["index"] in {20, 43, 817}
            ],
        }
        if record["name"] == "BBSqueezeBreakout":
            series = [
                [trace["index"], float(trace["obv"]).hex(), float(trace["obv_sma"]).hex()]
                for trace in source["traces"]
            ]
            row["obv_series"] = {
                "first_index": series[0][0],
                "last_index": series[-1][0],
                "count": len(series),
                "sha256": evidence_sha256(series),
            }
        strategies.append(row)
    return {
        key: payload[key]
        for key in (
            "schema_version",
            "scope",
            "capture_sha256",
            "environment",
            "execution",
            "input",
        )
    } | {
        "full_capture_sha256": evidence_sha256(payload),
        "strategies": strategies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    payload = capture_reference(
        repo_root=args.repo_root, source_dir=args.source_dir, input_path=args.input
    )
    root = args.repo_root.resolve()
    output = require_descendant(
        root / ".artifacts/research/strategy-validation", root, field="research artifacts"
    )
    path, digest = write_content_addressed_manifest(
        output, payload, manifest_directory="migration-source-references"
    )
    witnesses, witness_digest = write_content_addressed_manifest(
        output, reference_witnesses(payload), manifest_directory="migration-source-witnesses"
    )
    print(f"{path}\nsha256={digest} strategies={len(payload['strategies'])}")
    print(f"{witnesses}\nsha256={witness_digest}")


if __name__ == "__main__":
    main()
