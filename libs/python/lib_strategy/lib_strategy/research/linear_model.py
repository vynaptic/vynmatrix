"""Deterministic L2 return regression for immutable research datasets."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

import numpy as np
from numpy.typing import NDArray

from lib_common.hashing import canonical_json_bytes, canonical_json_hash

from .features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA,
    WARMUP_BARS,
    ResearchBar,
    features_from_bars,
    parse_bars,
    utc_timestamp,
)

REGULARIZATION_CANDIDATES = (0.1, 1.0, 10.0)
MIN_USABLE_OBSERVATIONS = 1000
MIN_TEST_OBSERVATIONS = 200
MAX_HORIZON = 30
MAX_IDENTITY_TEXT = 128
_IDENTITY_KEYS = {"instrument_id", "source", "timeframe", "dataset_hash", "image_hash"}
_MODEL_KEYS = {
    "schema_version",
    "algorithm",
    "algorithm_version",
    "feature_schema",
    "feature_names",
    "horizon",
    "identity",
    "identity_hash",
    "training_bars_hash",
    "scaler",
    "coefficients",
    "intercept",
    "regularization",
    "split",
    "split_hash",
    "training_label_cutoff",
    "embargo_cutoff",
    "training_cutoff",
    "training_mean",
    "artifact_hash",
}
_WINDOW_KEYS = {
    "first_ts",
    "last_ts",
    "last_label_ts",
    "count",
    "first_bar_index",
    "last_bar_index",
}
_SPLIT_KEYS = {
    "usable_count",
    "horizon",
    "embargo_bars",
    "validation_boundary",
    "test_boundary",
    "train",
    "validation",
    "test",
    "refit",
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
FloatArray = NDArray[np.float64]


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


def _horizon(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_HORIZON:
        _refuse("horizon must be an integer from 1 to 30 observed bars")
    return value


def _identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        _refuse("identity requires instrument_id/source/timeframe/dataset_hash/image_hash")
    assert isinstance(value, dict)
    instrument = value["instrument_id"]
    valid_instrument = (type(instrument) is int and instrument > 0) or (
        isinstance(instrument, str)
        and bool(instrument.strip())
        and len(instrument) <= MAX_IDENTITY_TEXT
    )
    valid_labels = all(
        isinstance(value[key], str) and value[key].strip() and len(value[key]) <= MAX_IDENTITY_TEXT
        for key in ("source", "timeframe")
    )
    if not valid_instrument or not valid_labels:
        _refuse("identity has an invalid instrument, source or timeframe")
    for key in ("dataset_hash", "image_hash"):
        digest = value[key]
        if key == "image_hash" and isinstance(digest, str):
            digest = digest.removeprefix("sha256:")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            _refuse("identity requires frozen SHA-256 dataset and image hashes")
    return dict(value)


def _window(indices: list[int], rows: list[ResearchBar], horizon: int) -> dict[str, Any]:
    first, last = indices[0] + WARMUP_BARS, indices[-1] + WARMUP_BARS
    return {
        "first_ts": rows[first].ts,
        "last_ts": rows[last].ts,
        "last_label_ts": rows[last + horizon].ts,
        "count": len(indices),
        "first_bar_index": first,
        "last_bar_index": last,
    }


def _split(
    rows: list[ResearchBar],
    horizon: int,
) -> tuple[dict[str, Any], list[int], list[int], list[int], list[int]]:
    count = len(rows) - WARMUP_BARS - horizon
    if count < MIN_USABLE_OBSERVATIONS:
        _refuse("training requires at least 1000 usable labeled observations")
    validation_boundary, test_boundary = count * 60 // 100, count * 80 // 100
    # Boundary labels are purged; the observed boundary bar is the embargo.
    train = list(range(validation_boundary - horizon))
    validation = list(range(validation_boundary + 1, test_boundary - horizon))
    test = list(range(test_boundary + 1, count))
    if len(test) < MIN_TEST_OBSERVATIONS:
        _refuse("training requires at least 200 untouched final test observations")
    refit = train + validation
    split = {
        "usable_count": count,
        "horizon": horizon,
        "embargo_bars": 1,
        "validation_boundary": rows[WARMUP_BARS + validation_boundary].ts,
        "test_boundary": rows[WARMUP_BARS + test_boundary].ts,
        **{
            name: _window(indices, rows, horizon)
            for name, indices in (
                ("train", train),
                ("validation", validation),
                ("test", test),
                ("refit", refit),
            )
        },
    }
    return split, train, validation, test, refit


def _scaler(matrix: FloatArray) -> dict[str, list[float]]:
    mean, scale = np.mean(matrix, axis=0), np.std(matrix, axis=0)
    scale = np.where(scale > 0, scale, 1.0)
    if not np.isfinite(mean).all() or not np.isfinite(scale).all():
        _refuse("training scaler exceeds finite numerical range")
    return {"mean": mean.tolist(), "scale": scale.tolist()}


def _scaled(matrix: FloatArray, scaler: dict[str, Any]) -> FloatArray:
    return np.asarray((matrix - scaler["mean"]) / scaler["scale"], dtype=np.float64)


def _fit(matrix: FloatArray, labels: FloatArray, alpha: float) -> tuple[list[float], float]:
    # Keep the intercept unpenalized, including when held-out data has a mean shift.
    center = np.mean(matrix, axis=0)
    target_mean = float(np.mean(labels))
    centered = matrix - center
    penalty = alpha * np.eye(matrix.shape[1])
    coefficients = np.linalg.solve(
        centered.T @ centered + penalty, centered.T @ (labels - target_mean)
    )
    intercept = target_mean - float(center @ coefficients)
    if not np.isfinite(coefficients).all() or not math.isfinite(intercept):
        _refuse("fitted model exceeds finite numerical range")
    return coefficients.tolist(), intercept


def _forecast(matrix: FloatArray, coefficients: list[float], intercept: float) -> FloatArray:
    # A fixed per-row reduction also gives identical predictions for prefixes.
    result = np.sum(matrix * coefficients, axis=1) + intercept
    if not np.isfinite(result).all():
        _refuse("prediction exceeds finite numerical range")
    return np.asarray(result, dtype=np.float64)


def _metrics(actual: FloatArray, predicted: FloatArray) -> dict[str, Any]:
    count = len(actual)
    if not count:
        return {"count": 0, "mae": None, "rmse": None, "directional_accuracy": None}
    residual = predicted - actual
    directional = actual != 0
    accuracy = (
        float(np.mean(np.sign(predicted[directional]) == np.sign(actual[directional])))
        if np.any(directional)
        else None
    )
    return {
        "count": count,
        "mae": float(np.mean(np.abs(residual))),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "directional_accuracy": accuracy,
    }


def _baseline_metrics(
    actual: FloatArray,
    predicted: FloatArray,
    training_mean: float,
) -> dict[str, Any]:
    return {
        "model": _metrics(actual, predicted),
        "zero_return": _metrics(actual, np.zeros(len(actual))),
        "training_mean": _metrics(actual, np.full(len(actual), training_mean)),
    }


def _training_arrays(rows: list[ResearchBar], horizon: int) -> tuple[FloatArray, FloatArray]:
    features = features_from_bars(rows)
    matrix = np.asarray([row["values"] for row in features[:-horizon]], dtype=np.float64)
    labels = np.asarray(
        [
            math.log(rows[index + horizon].close) - math.log(rows[index].close)
            for index in range(WARMUP_BARS, len(rows) - horizon)
        ],
        dtype=np.float64,
    )
    return matrix, labels


def _select(
    matrix: FloatArray,
    labels: FloatArray,
    train: list[int],
    validation: list[int],
) -> tuple[float, list[dict[str, Any]], dict[str, list[float]]]:
    scaler = _scaler(matrix[train])
    train_matrix, validation_matrix = (
        _scaled(matrix[train], scaler),
        _scaled(matrix[validation], scaler),
    )
    candidates = []
    for alpha in REGULARIZATION_CANDIDATES:
        coefficients, intercept = _fit(train_matrix, labels[train], alpha)
        metrics = _metrics(
            labels[validation], _forecast(validation_matrix, coefficients, intercept)
        )
        candidates.append({"regularization": alpha, **metrics})
    selected = min(candidates, key=lambda item: (item["rmse"], item["regularization"]))
    return float(selected["regularization"]), candidates, scaler


def train_return_model(
    bars: Sequence[Mapping[str, Any]],
    *,
    horizon: int = 1,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Fit a frozen research model; the final holdout never informs selection."""
    horizon, frozen_identity = _horizon(horizon), _identity(identity)
    rows = parse_bars(bars)
    split, train, validation, test, refit = _split(rows, horizon)
    matrix, labels = _training_arrays(rows, horizon)
    alpha, candidates, selection_scaler = _select(matrix, labels, train, validation)
    scaler = _scaler(matrix[refit])
    coefficients, intercept = _fit(_scaled(matrix[refit], scaler), labels[refit], alpha)
    training_mean = float(np.mean(labels[refit]))
    model = {
        "schema_version": 1,
        "algorithm": "ridge_log_return",
        "algorithm_version": 1,
        "feature_schema": FEATURE_SCHEMA,
        "feature_names": list(FEATURE_NAMES),
        "horizon": horizon,
        "identity": frozen_identity,
        "identity_hash": canonical_json_hash(frozen_identity),
        "training_bars_hash": canonical_json_hash(bars),
        "scaler": scaler,
        "coefficients": coefficients,
        "intercept": intercept,
        "regularization": alpha,
        "split": split,
        "split_hash": canonical_json_hash(split),
        "training_label_cutoff": split["refit"]["last_label_ts"],
        "embargo_cutoff": split["test_boundary"],
        "training_cutoff": split["test"]["first_ts"],
        "training_mean": training_mean,
    }
    model["artifact_hash"] = canonical_json_hash(model)
    predictions = _forecast(_scaled(matrix[test], scaler), coefficients, intercept)
    result = {
        "model": validate_model(model),
        "metrics": _baseline_metrics(labels[test], predictions, training_mean),
        "validation": {
            "candidates": candidates,
            "selection_scaler": selection_scaler,
            "split": split,
            "minimum_usable_observations": MIN_USABLE_OBSERVATIONS,
            "minimum_test_observations": MIN_TEST_OBSERVATIONS,
            "directional_accuracy_policy": "nonzero actual returns; zero predictions are incorrect",
        },
        "predictions": [
            {
                "ts": rows[index + WARMUP_BARS].ts,
                "predicted_log_return": float(prediction),
                "actual_log_return": float(labels[index]),
            }
            for index, prediction in zip(test, predictions, strict=True)
        ],
    }
    canonical_json_bytes(result)
    return result


def _numeric_vector(value: object, *, positive: bool = False) -> None:
    if not isinstance(value, list) or len(value) != len(FEATURE_NAMES):
        _refuse("model vector has an invalid feature dimension")
    assert isinstance(value, list)
    if any(
        type(item) not in (int, float) or not math.isfinite(item) or (positive and item <= 0)
        for item in value
    ):
        _refuse("model vector requires finite numeric values and positive scales")


def _validate_window(window: object, horizon: int) -> None:
    if not isinstance(window, dict) or set(window) != _WINDOW_KEYS:
        _refuse("model split window schema mismatch")
    assert isinstance(window, dict)
    for key in ("count", "first_bar_index", "last_bar_index"):
        if type(window[key]) is not int or window[key] < 1:
            _refuse("model split counts and indices must be positive integers")
    first, last, label = (
        utc_timestamp(window[key]) for key in ("first_ts", "last_ts", "last_label_ts")
    )
    if first > last or last >= label or window["last_bar_index"] < window["first_bar_index"]:
        _refuse("model split timestamps or indices are out of order")
    if window["last_bar_index"] - window["first_bar_index"] + 1 < window["count"] or horizon < 1:
        _refuse("model split count exceeds its index window")


def _validate_split(model: dict[str, Any]) -> None:
    split = model["split"]
    if not isinstance(split, dict) or set(split) != _SPLIT_KEYS:
        _refuse("model split schema mismatch")
    if (
        type(split["usable_count"]) is not int
        or split["usable_count"] < MIN_USABLE_OBSERVATIONS
        or type(split["embargo_bars"]) is not int
        or split["embargo_bars"] != 1
        or type(split["horizon"]) is not int
        or split["horizon"] != model["horizon"]
    ):
        _refuse("model split observations, horizon or embargo mismatch")
    for key in ("train", "validation", "test", "refit"):
        _validate_window(split[key], model["horizon"])
    _validate_split_indices(split, model["horizon"])
    train, validation, test, refit = (
        split[key] for key in ("train", "validation", "test", "refit")
    )
    first_boundary, second_boundary = (
        utc_timestamp(split[key]) for key in ("validation_boundary", "test_boundary")
    )
    if not (
        utc_timestamp(train["last_label_ts"])
        < first_boundary
        < utc_timestamp(validation["first_ts"])
        and utc_timestamp(validation["last_label_ts"])
        < second_boundary
        < utc_timestamp(test["first_ts"])
    ):
        _refuse("model split does not purge crossing labels and embargo boundaries")
    if (
        test["count"] < MIN_TEST_OBSERVATIONS
        or refit["count"] != train["count"] + validation["count"]
        or refit["first_ts"] != train["first_ts"]
        or refit["last_ts"] != validation["last_ts"]
        or refit["last_label_ts"] != validation["last_label_ts"]
    ):
        _refuse("model split holdout or refit lineage mismatch")
    if (
        model["training_label_cutoff"] != refit["last_label_ts"]
        or model["embargo_cutoff"] != split["test_boundary"]
        or model["training_cutoff"] != test["first_ts"]
    ):
        _refuse("model training cutoff does not match its purged split")


def _validate_split_indices(split: dict[str, Any], horizon: int) -> None:
    count = split["usable_count"]
    first_boundary, second_boundary = count * 60 // 100, count * 80 // 100
    ranges = {
        "train": (0, first_boundary - horizon),
        "validation": (first_boundary + 1, second_boundary - horizon),
        "test": (second_boundary + 1, count),
    }
    for key, (start, stop) in ranges.items():
        window = split[key]
        if (
            window["count"] != stop - start
            or window["first_bar_index"] != WARMUP_BARS + start
            or window["last_bar_index"] != WARMUP_BARS + stop - 1
        ):
            _refuse("model split counts or indices violate the fixed chronological recipe")
    if (
        split["refit"]["first_bar_index"] != split["train"]["first_bar_index"]
        or split["refit"]["last_bar_index"] != split["validation"]["last_bar_index"]
    ):
        _refuse("model refit indices disagree with its train and validation windows")


def validate_model(model: Mapping[str, Any]) -> dict[str, Any]:
    """Validate finite JSON, exact schema, recipe, dimensions and lineage digests.

    Digests detect changes; registry/owner authorization remains the caller's
    responsibility. They are content identities, not signatures.
    """
    try:
        clone = json.loads(canonical_json_bytes(model))
    except (TypeError, ValueError, OverflowError) as exc:
        message = "model must contain strict finite JSON values"
        raise ValueError(message) from exc
    if not isinstance(clone, dict) or set(clone) != _MODEL_KEYS:
        _refuse("model schema has missing or unknown fields")
    if (
        type(clone["schema_version"]) is not int
        or clone["schema_version"] != 1
        or type(clone["algorithm_version"]) is not int
        or clone["algorithm_version"] != 1
        or clone["algorithm"] != "ridge_log_return"
        or clone["feature_schema"] != FEATURE_SCHEMA
        or clone["feature_names"] != list(FEATURE_NAMES)
    ):
        _refuse("model algorithm or feature schema mismatch")
    _horizon(clone["horizon"])
    _identity(clone["identity"])
    scaler = clone["scaler"]
    if not isinstance(scaler, dict) or set(scaler) != {"mean", "scale"}:
        _refuse("model scaler schema mismatch")
    _numeric_vector(scaler["mean"])
    _numeric_vector(scaler["scale"], positive=True)
    _numeric_vector(clone["coefficients"])
    for key in ("intercept", "training_mean", "regularization"):
        if type(clone[key]) not in (int, float) or not math.isfinite(clone[key]):
            _refuse("model scalar requires finite numeric values")
    if clone["regularization"] not in REGULARIZATION_CANDIDATES:
        _refuse("model regularization is outside the frozen candidates")
    _validate_split(clone)
    for key, content in (("identity_hash", clone["identity"]), ("split_hash", clone["split"])):
        if clone[key] != canonical_json_hash(content):
            _refuse("model identity or split hash mismatch")
    if not isinstance(clone["training_bars_hash"], str) or not _HASH.fullmatch(
        clone["training_bars_hash"]
    ):
        _refuse("model training bars hash is invalid")
    expected = canonical_json_hash(
        {key: value for key, value in clone.items() if key != "artifact_hash"}
    )
    if clone["artifact_hash"] != expected:
        _refuse("model artifact hash mismatch")
    return dict(clone)


def predict_return_model(
    model: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    evaluation_start: str | None = None,
) -> dict[str, Any]:
    """Predict only after the frozen cutoff, allowing preceding bars as warmup.

    The owner-scoped caller verifies compatible instrument/source/timeframe;
    plain OHLCV rows contain no independent instrument authority to inspect.
    """
    frozen = validate_model(model)
    cutoff = utc_timestamp(frozen["training_cutoff"])
    start = utc_timestamp(evaluation_start) if evaluation_start is not None else cutoff
    if start < cutoff:
        _refuse("evaluation_start precedes the model training cutoff and embargo")
    rows = parse_bars(bars)
    if len(rows) <= WARMUP_BARS:
        _refuse("prediction requires at least 20 preceding warmup bars and one evaluation bar")
    first_evaluation = next(
        (index for index, row in enumerate(rows) if row.timestamp >= start), None
    )
    if first_evaluation is None:
        _refuse("no observations remain after the model training cutoff")
    if first_evaluation < WARMUP_BARS:
        _refuse("evaluation window lacks 20 preceding warmup bars")
    feature_rows = features_from_bars(rows)[first_evaluation - WARMUP_BARS :]
    matrix = np.asarray([row["values"] for row in feature_rows], dtype=np.float64)
    values = _forecast(
        _scaled(matrix, frozen["scaler"]), frozen["coefficients"], frozen["intercept"]
    )
    predictions, actual, scored = [], [], []
    for feature, prediction in zip(feature_rows, values, strict=True):
        index = feature["bar_index"]
        label = (
            math.log(rows[index + frozen["horizon"]].close) - math.log(rows[index].close)
            if index + frozen["horizon"] < len(rows)
            else None
        )
        predictions.append(
            {
                "ts": feature["ts"],
                "predicted_log_return": float(prediction),
                "actual_log_return": label,
            }
        )
        if label is not None:
            actual.append(label)
            scored.append(float(prediction))
    result = {
        "predictions": predictions,
        "metrics": _baseline_metrics(
            np.asarray(actual), np.asarray(scored), frozen["training_mean"]
        ),
        "validation": {
            "training_cutoff": frozen["training_cutoff"],
            "evaluation_start": start.isoformat(),
            "artifact_hash": frozen["artifact_hash"],
            "warmup_bars": WARMUP_BARS,
        },
    }
    canonical_json_bytes(result)
    return result
