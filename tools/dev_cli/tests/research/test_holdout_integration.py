"""The trusted coordinator adds holdout evidence to actual result timestamps."""

from __future__ import annotations

from datetime import datetime

import pytest

from dev_cli.core.research import worker


def inputs():
    identity = {"instrument_id": 1, "source": "fixture", "timeframe": "1m"}
    times = [
        "2020-01-01T11:59:00+00:00",
        "2020-01-01T12:00:00+00:00",
        "2020-01-01T13:00:00+00:00",
        "2020-01-02T12:00:00+00:00",
    ]
    return {
        "dataset": {
            "metadata": identity,
            "bars": [{"bar_close": at} for at in times],
        },
        "model": {
            "identity": identity,
            "artifact_hash": "a" * 64,
            "training_cutoff": times[1],
            "split": {
                "test": {
                    "first_ts": times[1],
                    "last_ts": times[2],
                    "last_label_ts": "2020-01-01T13:01:00+00:00",
                }
            },
        },
        "revision": {},
    }


class Wire:
    def send(self, _message):
        pass

    def receive(self):
        return {
            "kind": "result",
            "value": {
                "predictions": [{"ts": "2020-01-01T12:00:00+00:00"}],
                "validation": {"artifact_hash": "a" * 64},
            },
        }

    def finish(self):
        pass


def test_prediction_retains_validation_and_adds_actual_holdout_reuse(monkeypatch):
    values = inputs()
    monkeypatch.setattr(worker, "model_rows", lambda _dataset: [])
    monkeypatch.setattr(
        worker,
        "compatible_model",
        lambda model, _dataset, _start: (
            model,
            datetime.fromisoformat(model["training_cutoff"]),
        ),
    )
    result = worker.execute_job(Wire(), {"kind": "predict", "inputs": {}}, values)
    assert result["validation"]["artifact_hash"] == "a" * 64
    assert result["validation"]["holdout_reuse"]["holdout_reused"] is True
    assert result["validation"]["holdout_reuse"]["overlap_observations"] == 1


@pytest.mark.parametrize(
    ("evaluation_start", "expected"),
    [(None, 2), ("2020-01-02T12:00:00+00:00", 0)],
)
def test_model_backtest_marks_only_evaluation_bars_excluding_warmup(
    monkeypatch, evaluation_start, expected
):
    values = inputs()
    monkeypatch.setattr(worker, "run_backtest", lambda *_args, **_kwargs: {"currency": "USD"})
    result = worker.execute_job(
        Wire(),
        {"kind": "backtest", "inputs": {"assumptions": {}, "evaluation_start": evaluation_start}},
        values,
    )
    evidence = result["validation"]["holdout_reuse"]
    assert evidence["overlap_observations"] == expected
    assert evidence["holdout_reused"] is (expected > 0)
    assert result["currency"] == "USD"


def test_model_backtest_warns_for_horizon_three_label_tail(monkeypatch):
    values = inputs()
    values["model"]["horizon"] = 3
    values["model"]["split"]["test"]["last_label_ts"] = "2020-01-01T13:03:00+00:00"
    values["dataset"]["bars"].insert(3, {"bar_close": "2020-01-01T13:02:00+00:00"})
    monkeypatch.setattr(worker, "run_backtest", lambda *_args, **_kwargs: {"currency": "USD"})
    result = worker.execute_job(
        Wire(),
        {
            "kind": "backtest",
            "inputs": {"assumptions": {}, "evaluation_start": "2020-01-01T13:02:00+00:00"},
        },
        values,
    )
    evidence = result["validation"]["holdout_reuse"]
    assert evidence["holdout_reused"] is True
    assert evidence["overlap_observations"] == 1
    assert evidence["holdout_end"] == "2020-01-01T13:03:00+00:00"
