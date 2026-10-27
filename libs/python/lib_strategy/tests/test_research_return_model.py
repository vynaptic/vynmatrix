"""Research regression tests use deterministic unit fixtures, not market evidence."""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from lib_strategy.research import predict_return_model, train_return_model, validate_model
from lib_strategy.research.features import causal_features

IDENTITY = {
    "instrument_id": 12,
    "source": "recorded-fixture",
    "timeframe": "1h",
    "dataset_hash": "a" * 64,
    "image_hash": "b" * 64,
}


def bars(count=1300, *, constant=False, zero_volume=False):
    result = []
    price = 100.0
    for index in range(count):
        previous = price
        price *= math.exp(0.0001 + 0.003 * math.sin(index * 0.37)) if not constant else 1.0
        result.append(
            {
                "ts": (datetime(2020, 1, 1, tzinfo=UTC) + timedelta(hours=index)).isoformat(),
                "open": str(previous),
                "high": str(max(previous, price) * 1.001),
                "low": str(min(previous, price) * 0.999),
                "close": str(price),
                "volume": str(0 if zero_volume else 1000 + index % 17),
            }
        )
    return result


def test_fits_real_deterministic_model_with_finite_metrics_and_baselines():
    data = bars()
    first = train_return_model(data, identity=IDENTITY)
    assert first == train_return_model(data, identity=IDENTITY)
    json.dumps(first, allow_nan=False)
    model = validate_model(first["model"])
    assert model["identity"] == IDENTITY
    assert model["regularization"] in [0.1, 1.0, 10.0]
    assert any(value != 0 for value in model["coefficients"])
    assert first["metrics"]["model"]["rmse"] < first["metrics"]["zero_return"]["rmse"]
    assert first["metrics"]["model"]["count"] == len(first["predictions"])
    assert first["metrics"]["training_mean"]["count"] >= 200
    assert len(first["validation"]["candidates"]) == 3


def test_split_purges_crossing_labels_and_embargoes_observed_boundary_bars():
    data = bars(1600)
    result = train_return_model(data, horizon=30, identity=IDENTITY)
    split = result["model"]["split"]
    train, validation, test = (split[key] for key in ("train", "validation", "test"))
    assert train["last_label_ts"] < split["validation_boundary"] < validation["first_ts"]
    assert validation["last_label_ts"] < split["test_boundary"] < test["first_ts"]
    assert result["model"]["training_label_cutoff"] == validation["last_label_ts"]
    assert result["model"]["training_cutoff"] == test["first_ts"]
    assert result["predictions"][0]["ts"] == test["first_ts"]
    assert test["count"] >= 200


def test_scaling_and_candidates_never_observe_test_data():
    data = bars()
    original = train_return_model(data, identity=IDENTITY)
    boundary = original["model"]["split"]["test_boundary"]
    changed = copy.deepcopy(data)
    for row in changed:
        if row["ts"] >= boundary:
            for key in ("open", "high", "low", "close"):
                row[key] = str(float(row[key]) * 7)
    rerun = train_return_model(changed, identity=IDENTITY)
    for field in ("scaler", "coefficients", "intercept", "regularization"):
        assert original["model"][field] == rerun["model"][field]
    assert original["validation"]["candidates"] == rerun["validation"]["candidates"]


def test_selection_scaler_uses_only_training_and_has_a_real_chronological_split():
    data = bars()
    result = train_return_model(data, identity=IDENTITY)
    feature_set = causal_features(data)
    split = result["model"]["split"]
    rows = [
        row["values"]
        for row in feature_set
        if split["train"]["first_ts"] <= row["ts"] <= split["train"]["last_ts"]
    ]
    assert result["validation"]["selection_scaler"]["mean"] == pytest.approx(np.mean(rows, axis=0))
    assert split["usable_count"] == len(data) - 20 - 1
    assert split["train"]["count"] == math.floor(split["usable_count"] * 0.6) - 1


@pytest.mark.parametrize("count", [0, 100, 1020, 1021])
def test_refuses_insufficient_training_or_final_test_samples(count):
    with pytest.raises(ValueError, match="observations"):
        train_return_model(bars(count), identity=IDENTITY)


@pytest.mark.parametrize("horizon", [0, 31, True, 1.5])
def test_refuses_unsupported_horizon(horizon):
    with pytest.raises(ValueError, match="horizon"):
        train_return_model(bars(), horizon=horizon, identity=IDENTITY)


def test_prediction_enforces_cutoff_but_allows_nonemitting_warmup():
    data = bars()
    fitted = train_return_model(data, identity=IDENTITY)
    model = fitted["model"]
    with pytest.raises(ValueError, match="cutoff"):
        predict_return_model(model, data, evaluation_start=data[25]["ts"])
    output = predict_return_model(model, data, evaluation_start=model["training_cutoff"])
    assert output["predictions"][:-1] == fitted["predictions"]
    assert output["predictions"][-1]["actual_log_return"] is None
    assert all(row["ts"] >= model["training_cutoff"] for row in output["predictions"])
    with pytest.raises(ValueError, match="warmup"):
        predict_return_model(model, data[-5:])


def test_features_and_past_predictions_are_prefix_invariant():
    data = bars(1500)
    assert causal_features(data[:70]) == causal_features(data)[:50]
    model = train_return_model(data[:1300], identity=IDENTITY)["model"]
    prefix = predict_return_model(model, data[:1400])["predictions"]
    full = predict_return_model(model, data)["predictions"]
    assert [row["predicted_log_return"] for row in prefix] == [
        row["predicted_log_return"] for row in full[: len(prefix)]
    ]


def test_constant_prices_and_zero_volume_are_finite_and_directions_undefined():
    result = train_return_model(bars(constant=True, zero_volume=True), identity=IDENTITY)
    json.dumps(result, allow_nan=False)
    assert result["metrics"]["model"]["rmse"] == 0.0
    assert result["metrics"]["model"]["directional_accuracy"] is None
    assert all(value > 0 for value in result["model"]["scaler"]["scale"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("close", "NaN"), ("open", "0"), ("volume", "-1"), ("high", "1")],
)
def test_rejects_invalid_ohlcv(field, value):
    data = bars()
    data[100][field] = value
    with pytest.raises(ValueError, match="OHLCV"):
        train_return_model(data, identity=IDENTITY)


@pytest.mark.parametrize("timestamp", ["2020-01-01T01:00:00", "2020-01-01T01:00:00+01:00"])
def test_rejects_ambiguous_non_utc_or_nonincreasing_timestamps(timestamp):
    data = bars()
    data[1]["ts"] = timestamp
    with pytest.raises(ValueError, match=r"UTC|increasing"):
        train_return_model(data, identity=IDENTITY)


@pytest.mark.parametrize("mutation", ["coefficients", "extra", "feature_names", "cutoff"])
def test_model_validation_refuses_tampering_and_schema_mismatch(mutation):
    model = train_return_model(bars(), identity=IDENTITY)["model"]
    if mutation == "coefficients":
        model["coefficients"][0] = math.inf
    elif mutation == "extra":
        model["executable"] = "forbidden"
    elif mutation == "feature_names":
        model["feature_names"].reverse()
    else:
        model["training_cutoff"] = "1990-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match=r"finite|schema|cutoff|hash"):
        validate_model(model)


def test_missing_or_mutable_identity_is_rejected():
    identity = {**IDENTITY, "image_hash": "latest"}
    with pytest.raises(ValueError, match=r"identity|hash"):
        train_return_model(bars(), identity=identity)


def test_exact_one_bar_embargo_is_kept_on_irregular_observed_times():
    data = bars(1300)
    for index, row in enumerate(data):
        row["ts"] = (
            datetime(2020, 1, 1, tzinfo=UTC) + timedelta(hours=index + (index // 10) * 48)
        ).isoformat()
    result = train_return_model(data, identity=IDENTITY)
    model = result["model"]
    lookup = [row["ts"] for row in data]
    assert (
        lookup.index(model["training_cutoff"]) - lookup.index(model["training_label_cutoff"]) == 2
    )


def test_ohlcv_bounds_are_checked_before_lossy_float_conversion():
    data = bars()
    data[100].update(
        open="1.0000000000000000001",
        close="1.0000000000000000001",
        low="1.0000000000000000002",
        high="1.0000000000000000003",
    )
    with pytest.raises(ValueError, match="OHLCV"):
        train_return_model(data, identity=IDENTITY)


def test_rehashed_model_still_requires_exact_chronological_split_counts():
    from lib_common.hashing import canonical_json_hash

    model = train_return_model(bars(), identity=IDENTITY)["model"]
    model["split"]["test"]["count"] -= 1
    model["split_hash"] = canonical_json_hash(model["split"])
    model["artifact_hash"] = canonical_json_hash(
        {key: value for key, value in model.items() if key != "artifact_hash"}
    )
    with pytest.raises(ValueError, match=r"count|split"):
        validate_model(model)


def test_forecast_labels_and_zero_baseline_use_horizon_log_return():
    data = bars()
    result = train_return_model(data, horizon=5, identity=IDENTITY)
    by_time = {row["ts"]: index for index, row in enumerate(data)}
    prediction = result["predictions"][0]
    index = by_time[prediction["ts"]]
    assert prediction["actual_log_return"] == pytest.approx(
        math.log(float(data[index + 5]["close"]) / float(data[index]["close"]))
    )
    actual = [row["actual_log_return"] for row in result["predictions"]]
    assert result["metrics"]["zero_return"]["mae"] == pytest.approx(
        sum(abs(value) for value in actual) / len(actual)
    )


def test_causal_recipe_has_known_current_and_trailing_feature_values():
    data = bars(21, constant=True, zero_volume=True)
    data[-1].update(open="100", close="110", high="112", low="99", volume="20")
    feature = causal_features(data)[0]
    assert feature["bar_index"] == 20
    assert feature["values"][:3] == pytest.approx([math.log(1.1)] * 3)
    assert feature["values"][3:5] == pytest.approx([0.1, 13 / 110])
    assert feature["values"][-1] == 19.0
