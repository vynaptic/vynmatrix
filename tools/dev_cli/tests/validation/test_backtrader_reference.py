"""Reference extraction binds reviewed source without running its harness."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_cli.validation.backtrader_reference import extract_source_classes, load_recorded_bars


def test_reference_extraction_keeps_indicator_helpers_and_skips_side_effects(tmp_path: Path):
    source = tmp_path / "source.py"
    source.write_text("""
import unavailable_downloader
raise RuntimeError('source harness ran')
class Feed(bt.feeds.PandasData):
    raise RuntimeError('source feed class ran')
class Helper(bt.Indicator):
    pass
class Candidate(bt.Strategy):
    label = 'reviewed'
def run_backtest():
    raise RuntimeError('backtest harness ran')
""")
    tree, strategy_name = extract_source_classes(
        source, hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert strategy_name == "Candidate"
    assert [node.name for node in tree.body] == ["Helper", "Candidate"]
    namespace = {
        "bt": SimpleNamespace(
            Indicator=type("Indicator", (), {}), Strategy=type("Strategy", (), {})
        )
    }
    exec(compile(tree, str(source), "exec"), namespace)
    assert namespace["Candidate"].label == "reviewed"
    assert "run_backtest" not in namespace


def test_reference_refuses_modified_source_before_extraction(tmp_path: Path):
    source = tmp_path / "source.py"
    source.write_text("class Candidate(bt.Strategy): pass\n")
    with pytest.raises(ValueError, match="hash"):
        extract_source_classes(source, "0" * 64)


def test_reference_requires_one_unambiguous_strategy(tmp_path: Path):
    source = tmp_path / "source.py"
    source.write_text("class A(bt.Strategy): pass\nclass B(bt.Strategy): pass\n")
    with pytest.raises(ValueError, match="exactly one"):
        extract_source_classes(source, hashlib.sha256(source.read_bytes()).hexdigest())


def test_reference_requires_chronological_recorded_ohlcv(tmp_path: Path):
    import json

    payload = {
        "source": "recorded-test",
        "product": "BTC-USD",
        "granularity_seconds": 60,
        "bars": [
            {"ts": 60, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            {"ts": 60, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
        ],
    }
    source = tmp_path / "bars.json"
    source.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="chronological"):
        load_recorded_bars(source)
    payload["bars"][1]["ts"] = 120
    payload["bars"][1]["high"] = 8
    source.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="OHLCV"):
        load_recorded_bars(source)


def test_loaded_package_identity_detects_same_version_source_change(tmp_path: Path):
    from dev_cli.validation.backtrader_reference import package_identity

    package = tmp_path / "library"
    package.mkdir()
    entry = package / "__init__.py"
    entry.write_text("VERSION = '1.0'\n")
    implementation = package / "indicator.py"
    implementation.write_text("value = 1\n")
    module = SimpleNamespace(__file__=str(entry))
    original = package_identity(module)
    implementation.write_text("value = 2\n")
    assert package_identity(module) != original
    implementation.write_text("value = 1\n")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "indicator.pyc").write_bytes(b"incidental cache")
    assert package_identity(module) == original


def test_capture_cli_retains_distinct_inputs_and_reuses_identical_evidence(tmp_path, monkeypatch):
    import json
    import sys

    from dev_cli.validation import backtrader_reference as reference

    payload = {
        "schema_version": 1,
        "scope": "unit capture",
        "capture_sha256": "0" * 64,
        "environment": {},
        "execution": {},
        "input": {"name": "first"},
        "strategies": [],
    }
    monkeypatch.setattr(reference, "capture_reference", lambda **_kwargs: payload)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "--repo-root",
            str(tmp_path),
            "--source-dir",
            str(tmp_path),
            "--input",
            str(tmp_path / "input.json"),
        ],
    )
    reference.main()
    folder = tmp_path / ".artifacts/research/strategy-validation"
    original = {path: path.read_bytes() for path in folder.rglob("*.json")}
    assert len(original) == 2
    payload["input"]["name"] = "second"
    reference.main()
    assert all(path.read_bytes() == contents for path, contents in original.items())
    paths = list(folder.rglob("*.json"))
    assert len(paths) == 4
    reference.main()
    assert len(list(folder.rglob("*.json"))) == 4
    assert {json.loads(path.read_text())["manifest"]["input"]["name"] for path in paths} == {
        "first",
        "second",
    }
