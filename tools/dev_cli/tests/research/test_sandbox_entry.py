"""Protocol behavior with trusted test source; OS isolation is tested in Docker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE = """from lib_strategy.signals.pure_strategy import PureSignalStrategy
class UserStrategy(PureSignalStrategy):
    def initialize(self):
        self._warmup_bars_needed = 1
    def on_data(self, state):
        print("user diagnostic")
        self.emit_long(state.symbol, state.close, timestamp=state.timestamp)
"""


def run_entry(messages):
    path = Path(__file__).resolve().parents[2] / "dev_cli/core/research/sandbox_entry.py"
    return subprocess.run(
        [sys.executable, str(path)],
        input="".join(json.dumps(message) + "\n" for message in messages),
        env={"PATH": os.environ["PATH"], "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def start(mode="strategy", source=SOURCE):
    return [
        {
            "kind": "init",
            "mode": mode,
            "config": {"class_name": "UserStrategy", "strategy_id": "draft_v1", "parameters": {}},
        },
        {"kind": "source", "text": source},
        {"kind": "start"},
    ]


def test_validation_imports_canonical_core_and_keeps_diagnostics_off_protocol():
    result = run_entry(start("validate_strategy"))
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["kind"] == "result"
    assert value["value"]["valid"] is True
    assert value["value"]["warmup_bars"] == 1


def test_bar_protocol_retains_state_but_suppresses_warmup_signals():
    bar = {
        "symbol": "BTCUSDC",
        "ts": "2026-06-10T12:00:00+00:00",
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10",
        "volume": "1",
    }
    messages = [
        *start(),
        {"kind": "bar", "sequence": 0, "warmup": True, "bar": bar},
        {
            "kind": "bar",
            "sequence": 1,
            "warmup": False,
            "bar": {**bar, "ts": "2026-06-10T12:01:00+00:00", "close": "11"},
        },
        {"kind": "end"},
    ]
    result = run_entry(messages)
    assert result.returncode == 0, result.stderr
    values = [json.loads(line) for line in result.stdout.splitlines()]
    assert values[0] == {"kind": "ready", "warmup_bars": 1}
    assert values[1] == {"sequence": 0, "signals": []}
    assert values[2]["signals"][0]["entry_price"] == 11
    assert "user diagnostic" in result.stderr


def test_syntax_failure_is_a_failed_process_without_false_success():
    result = run_entry(start("validate_strategy", source="invalid Python !"))
    assert result.returncode != 0
    assert "SyntaxError" in result.stderr
    assert '"valid": true' not in result.stdout


def test_noncanonical_core_is_rejected_at_runtime_validation():
    result = run_entry(start("validate_strategy", source="class UserStrategy: pass"))
    assert result.returncode != 0
    assert (
        "ValueError: UserStrategy must subclass the canonical PureSignalStrategy" in result.stderr
    )


def test_shipped_editor_templates_pass_the_actual_canonical_loader_protocol():
    from backend.research_templates import strategy_templates

    for template in strategy_templates():
        result = run_entry(start("validate_strategy", template["source"]))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["value"]["valid"] is True
