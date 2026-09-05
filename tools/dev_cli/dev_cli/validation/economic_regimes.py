"""No-lookahead economic-regime calibration from immutable close ledgers."""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dev_cli.validation._diagnostic_common import (
    ECONOMIC_PERIOD_DATE_RULE,
    REGIME_FEATURE_LAG_POLICY_ID,
    REGIME_QUANTILE_METHOD_ID,
    REGIME_RETURN_DEFINITION_ID,
    REGIME_TREND_BOUNDARY_POLICY_ID,
    REGIME_VOLATILITY_BOUNDARY_POLICY_ID,
    REGIME_VOLATILITY_ESTIMATOR_ID,
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)
from dev_cli.validation.evidence import evidence_sha256
from dev_cli.validation.persistence.backtest_manifest_store import validate_sha256_digest


def _validate_registered_indices(
    values: Sequence[int],
    *,
    field_name: str,
) -> tuple[int, ...]:
    indices = tuple(values)
    if not indices:
        invalid(f"{field_name} must not be empty")
    if any(isinstance(index, bool) or not isinstance(index, int) for index in indices):
        invalid(f"{field_name} must contain integers")
    if any(index < 0 for index in indices):
        invalid(f"{field_name} must contain non-negative indices")
    if any(current <= previous for previous, current in itertools.pairwise(indices)):
        invalid(f"{field_name} must be unique and strictly increasing")
    return indices


@dataclass(frozen=True, slots=True)
class RegimeDecisionClose:
    """One indexed daily decision-close price used only for regime features."""

    index: int
    timestamp: datetime
    close: float

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            invalid("decision-close index must be a non-negative integer")
        require_utc(self.timestamp, field_name="decision-close timestamp")
        require_finite(self.close, field_name="decision-close price")
        if self.close <= 0.0:
            invalid("decision-close price must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "close": self.close,
        }


@dataclass(frozen=True, slots=True)
class RegimeDecisionCloseSeries:
    """Exact indexed decision-close ledger for one asset and source artifact."""

    asset: str
    source_id: str
    source_sha256: str
    observations: tuple[RegimeDecisionClose, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.asset, field_name="regime close-series asset")
        require_nonempty(self.source_id, field_name="regime close-series source_id")
        validate_sha256_digest(
            self.source_sha256,
            field="regime close-series source_sha256",
        )
        if not self.observations:
            invalid("regime decision-close observations must not be empty")
        if tuple(item.index for item in self.observations) != tuple(range(len(self.observations))):
            invalid("regime decision-close indices must exactly cover zero through n-1")
        timestamps = tuple(item.timestamp for item in self.observations)
        if any(current <= previous for previous, current in itertools.pairwise(timestamps)):
            invalid("regime decision-close timestamps must be strictly increasing")
        dates = tuple(timestamp.date() for timestamp in timestamps)
        if len(set(dates)) != len(dates):
            invalid("regime decision-close series may contain only one observation per UTC date")
        close_time = (
            timestamps[0].hour,
            timestamps[0].minute,
            timestamps[0].second,
            timestamps[0].microsecond,
        )
        if any(
            (
                timestamp.hour,
                timestamp.minute,
                timestamp.second,
                timestamp.microsecond,
            )
            != close_time
            for timestamp in timestamps[1:]
        ):
            invalid("regime decision-close timestamps must share one UTC daily close time")


@dataclass(frozen=True, slots=True)
class RegimeFoldIndices:
    """Exact eligible training and OOS test identities for one fold."""

    fold_id: str
    training_indices: tuple[int, ...]
    test_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.fold_id, field_name="regime fold_id")
        training = _validate_registered_indices(
            self.training_indices,
            field_name="regime training_indices",
        )
        test = _validate_registered_indices(
            self.test_indices,
            field_name="regime test_indices",
        )
        if set(training) & set(test):
            invalid("regime calibration training indices must exclude every test index")
        if training[-1] >= test[0]:
            invalid("regime calibration training indices must strictly precede test indices")
        if any(current != previous + 1 for previous, current in itertools.pairwise(test)):
            invalid("regime test indices must form a complete contiguous grid")


@dataclass(frozen=True, slots=True)
class RegimeFoldIndexRegistry:
    """Source-attested ordered fold-index registry."""

    source_id: str
    source_sha256: str
    folds: tuple[RegimeFoldIndices, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.source_id, field_name="regime fold-index source_id")
        validate_sha256_digest(
            self.source_sha256,
            field="regime fold-index source_sha256",
        )
        if not self.folds:
            invalid("regime fold-index registry must not be empty")
        fold_ids = tuple(item.fold_id for item in self.folds)
        if len(set(fold_ids)) != len(fold_ids):
            invalid("regime fold identities must be unique")
        if any(
            current.test_indices[0] <= previous.test_indices[-1]
            for previous, current in itertools.pairwise(self.folds)
        ):
            invalid("regime fold test grids must be ordered and non-overlapping")


@dataclass(frozen=True, slots=True)
class EconomicRegimeCalibrationConfig:
    """Generic registered regime methodology and identity order."""

    calibration_id: str
    asset_order: tuple[str, ...]
    fold_order: tuple[str, ...]
    volatility_lookback_bars: int
    trend_lookback_bars: int
    annualization_factor: float
    economic_period_date_rule: str

    def __post_init__(self) -> None:
        require_nonempty(self.calibration_id, field_name="regime calibration_id")
        for field_name, values in (
            ("regime asset_order", self.asset_order),
            ("regime fold_order", self.fold_order),
        ):
            if not values:
                invalid(f"{field_name} must not be empty")
            if len(set(values)) != len(values):
                invalid(f"{field_name} identities must be unique")
            for identity in values:
                require_nonempty(identity, field_name=f"{field_name} identity")
        for field_name, count_value, minimum in (
            ("volatility_lookback_bars", self.volatility_lookback_bars, 2),
            ("trend_lookback_bars", self.trend_lookback_bars, 1),
        ):
            if (
                isinstance(count_value, bool)
                or not isinstance(count_value, int)
                or count_value < minimum
            ):
                invalid(f"{field_name} must be an integer of at least {minimum}")
        require_finite(self.annualization_factor, field_name="regime annualization_factor")
        if self.annualization_factor <= 0.0:
            invalid("regime annualization_factor must be positive")
        if self.economic_period_date_rule != ECONOMIC_PERIOD_DATE_RULE:
            invalid("regime economic-period rule differs from close-minus-one-calendar-day")

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_id": self.calibration_id,
            "asset_order": list(self.asset_order),
            "fold_order": list(self.fold_order),
            "volatility_lookback_bars": self.volatility_lookback_bars,
            "trend_lookback_bars": self.trend_lookback_bars,
            "annualization_factor": self.annualization_factor,
            "economic_period_date_rule": self.economic_period_date_rule,
        }


@dataclass(frozen=True, slots=True)
class RegimeCloseSourceProvenance:
    """Content hash and source identity for one validated close ledger."""

    asset: str
    source_id: str
    source_sha256: str
    decision_close_ledger_sha256: str
    observations: int
    first_decision_close_ts: datetime
    last_decision_close_ts: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "decision_close_ledger_sha256": self.decision_close_ledger_sha256,
            "observations": self.observations,
            "first_decision_close_ts": self.first_decision_close_ts.isoformat(),
            "last_decision_close_ts": self.last_decision_close_ts.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class EconomicRegimeCalibrationProvenance:
    """Complete methodology and immutable input provenance for one build."""

    config: EconomicRegimeCalibrationConfig
    close_sources: tuple[RegimeCloseSourceProvenance, ...]
    fold_index_source_id: str
    fold_index_source_sha256: str
    fold_index_ledger_sha256: str
    return_definition_id: str
    feature_lag_policy_id: str
    volatility_estimator_id: str
    quantile_method_id: str
    volatility_boundary_policy_id: str
    trend_boundary_policy_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "config": self.config.to_dict(),
            "close_sources": [item.to_dict() for item in self.close_sources],
            "fold_index_source_id": self.fold_index_source_id,
            "fold_index_source_sha256": self.fold_index_source_sha256,
            "fold_index_ledger_sha256": self.fold_index_ledger_sha256,
            "return_definition_id": self.return_definition_id,
            "feature_lag_policy_id": self.feature_lag_policy_id,
            "volatility_estimator_id": self.volatility_estimator_id,
            "quantile_method_id": self.quantile_method_id,
            "volatility_boundary_policy_id": self.volatility_boundary_policy_id,
            "trend_boundary_policy_id": self.trend_boundary_policy_id,
        }


@dataclass(frozen=True, slots=True)
class EconomicRegimeVolatilityThresholds:
    """Nearest-rank volatility thresholds fitted on one asset/fold training set."""

    asset: str
    fold_id: str
    threshold_source_id: str
    registered_training_indices: tuple[int, ...]
    volatility_feature_training_indices: tuple[int, ...]
    registered_test_indices: tuple[int, ...]
    calibration_input_sha256: str
    calibration_test_observations: int
    volatility_training_observations: int
    p25: float
    p50: float
    p75: float

    def __post_init__(self) -> None:
        require_nonempty(self.asset, field_name="regime threshold asset")
        require_nonempty(self.fold_id, field_name="regime threshold fold_id")
        require_nonempty(self.threshold_source_id, field_name="regime threshold source_id")
        validate_sha256_digest(
            self.calibration_input_sha256,
            field="regime calibration_input_sha256",
        )
        if self.calibration_test_observations != 0:
            invalid("regime threshold calibration must contain zero test observations")
        if self.volatility_training_observations != len(self.volatility_feature_training_indices):
            invalid("regime volatility training count must match its feature indices")
        if set(self.volatility_feature_training_indices) - set(self.registered_training_indices):
            invalid("regime volatility feature indices must be registered training indices")
        if set(self.volatility_feature_training_indices) & set(self.registered_test_indices):
            invalid("regime volatility feature indices cannot contain test rows")
        for field_name, value in (("p25", self.p25), ("p50", self.p50), ("p75", self.p75)):
            require_finite(value, field_name=f"regime threshold {field_name}")
        if not self.p25 <= self.p50 <= self.p75:
            invalid("regime volatility thresholds must be non-decreasing")

    def to_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "fold_id": self.fold_id,
            "threshold_source_id": self.threshold_source_id,
            "registered_training_indices": list(self.registered_training_indices),
            "volatility_feature_training_indices": list(self.volatility_feature_training_indices),
            "registered_test_indices": list(self.registered_test_indices),
            "calibration_input_sha256": self.calibration_input_sha256,
            "calibration_test_observations": self.calibration_test_observations,
            "volatility_training_observations": self.volatility_training_observations,
            "p25": self.p25,
            "p50": self.p50,
            "p75": self.p75,
        }


@dataclass(frozen=True, slots=True)
class EconomicRegimeObservation:
    """One precomputed, no-lookahead regime label for an economic period."""

    asset: str
    fold_id: str
    economic_period_date: date
    volatility_bucket: str
    trend_bucket: str
    threshold_source_id: str
    unclassified_reason: str | None = None
    decision_index: int | None = None
    decision_close_ts: datetime | None = None

    def __post_init__(self) -> None:
        require_nonempty(self.asset, field_name="asset")
        require_nonempty(self.fold_id, field_name="fold_id")
        require_nonempty(self.threshold_source_id, field_name="threshold_source_id")
        if self.volatility_bucket not in {"q1", "q2", "q3", "q4", "unclassified"}:
            invalid("volatility_bucket must be q1/q2/q3/q4/unclassified")
        if self.trend_bucket not in {"positive", "nonpositive", "unclassified"}:
            invalid("trend_bucket must be positive/nonpositive/unclassified")
        is_unclassified = "unclassified" in {self.volatility_bucket, self.trend_bucket}
        if is_unclassified and self.unclassified_reason != "registered_lag_history_unavailable":
            invalid("unclassified regimes require the registered lag-history reason")
        if not is_unclassified and self.unclassified_reason is not None:
            invalid("classified regimes cannot carry an unclassified reason")
        if (self.decision_index is None) != (self.decision_close_ts is None):
            invalid("regime decision index and close timestamp must be supplied together")
        if self.decision_index is not None:
            if (
                isinstance(self.decision_index, bool)
                or not isinstance(self.decision_index, int)
                or self.decision_index < 0
            ):
                invalid("regime decision_index must be a non-negative integer")
            assert self.decision_close_ts is not None
            require_utc(self.decision_close_ts, field_name="regime decision_close_ts")
            expected_date = (self.decision_close_ts - timedelta(days=1)).date()
            if self.economic_period_date != expected_date:
                invalid("regime economic date must equal decision close minus one calendar day")

    @property
    def regime_id(self) -> str:
        if "unclassified" in {self.volatility_bucket, self.trend_bucket}:
            return "unclassified"
        return f"{self.volatility_bucket}:{self.trend_bucket}"

    def to_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "fold_id": self.fold_id,
            "economic_period_date": self.economic_period_date.isoformat(),
            "volatility_bucket": self.volatility_bucket,
            "trend_bucket": self.trend_bucket,
            "threshold_source_id": self.threshold_source_id,
            "unclassified_reason": self.unclassified_reason,
            "decision_index": self.decision_index,
            "decision_close_ts": (
                self.decision_close_ts.isoformat() if self.decision_close_ts is not None else None
            ),
            "regime_id": self.regime_id,
        }


@dataclass(frozen=True, slots=True)
class EconomicRegimeCalibrationResult:
    """Ordered no-lookahead regime rows with calibration provenance."""

    provenance: EconomicRegimeCalibrationProvenance
    thresholds: tuple[EconomicRegimeVolatilityThresholds, ...]
    observations: tuple[EconomicRegimeObservation, ...]
    evidence_role: str = field(default="retrospective_no_lookahead_regime_evidence", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "provenance": self.provenance.to_dict(),
            "thresholds": [item.to_dict() for item in self.thresholds],
            "observations": [item.to_dict() for item in self.observations],
            "evidence_role": self.evidence_role,
        }


def _regime_close_ledger_sha256(series: RegimeDecisionCloseSeries) -> str:
    return evidence_sha256(
        {
            "asset": series.asset,
            "observations": [
                {
                    "index": item.index,
                    "timestamp": item.timestamp.isoformat(),
                    "close": format(item.close, ".17g"),
                }
                for item in series.observations
            ],
        }
    )


def _regime_fold_index_ledger_sha256(registry: RegimeFoldIndexRegistry) -> str:
    return evidence_sha256(
        {
            "folds": [
                {
                    "fold_id": fold.fold_id,
                    "training_indices": list(fold.training_indices),
                    "test_indices": list(fold.test_indices),
                }
                for fold in registry.folds
            ]
        }
    )


def _nearest_rank_ceiling(values: Sequence[float], *, quantile: float) -> float:
    if not values:
        invalid("nearest-rank calibration requires at least one value")
    if not 0.0 < quantile < 1.0:
        invalid("nearest-rank quantile must be in (0, 1)")
    ordered = sorted(values)
    return float(ordered[math.ceil(quantile * len(ordered)) - 1])


def _lagged_regime_features(
    series: RegimeDecisionCloseSeries,
    *,
    config: EconomicRegimeCalibrationConfig,
) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
    closes = tuple(item.close for item in series.observations)
    return_rows: list[float | None] = [None]
    for previous, current in itertools.pairwise(closes):
        close_return = current / previous - 1.0
        require_finite(close_return, field_name="close-to-close return")
        return_rows.append(close_return)
    close_returns = tuple(return_rows)
    lagged_volatility: list[float | None] = []
    lagged_trend: list[float | None] = []
    volatility_lookback = config.volatility_lookback_bars
    trend_lookback = config.trend_lookback_bars
    for index in range(len(closes)):
        if index >= volatility_lookback + 1:
            window = close_returns[index - volatility_lookback : index]
            if any(value is None for value in window):
                invalid("lagged volatility window unexpectedly contains a missing return")
            returns = tuple(float(value) for value in window if value is not None)
            volatility = statistics.stdev(returns) * math.sqrt(config.annualization_factor)
            require_finite(volatility, field_name="lagged realized volatility")
            lagged_volatility.append(volatility)
        else:
            lagged_volatility.append(None)

        if index >= trend_lookback + 1:
            trend_return = closes[index - 1] / closes[index - trend_lookback - 1] - 1.0
            require_finite(trend_return, field_name="lagged close return")
            lagged_trend.append(trend_return)
        else:
            lagged_trend.append(None)
    return tuple(lagged_volatility), tuple(lagged_trend)


def _regime_volatility_bucket(
    value: float,
    *,
    p25: float,
    p50: float,
    p75: float,
) -> str:
    if value <= p25:
        return "q1"
    if value <= p50:
        return "q2"
    if value <= p75:
        return "q3"
    return "q4"


def build_no_lookahead_economic_regimes(
    decision_close_series: Sequence[RegimeDecisionCloseSeries],
    *,
    config: EconomicRegimeCalibrationConfig,
    fold_registry: RegimeFoldIndexRegistry,
) -> EconomicRegimeCalibrationResult:
    """Calibrate per-asset fold thresholds and label exact OOS decision rows."""

    series_rows = tuple(decision_close_series)
    assets = tuple(item.asset for item in series_rows)
    if assets != config.asset_order:
        invalid("regime close series must exactly follow the registered asset order")
    if tuple(item.fold_id for item in fold_registry.folds) != config.fold_order:
        invalid("regime fold registry must exactly follow the registered fold order")

    reference_grid = tuple((item.index, item.timestamp) for item in series_rows[0].observations)
    if any(
        tuple((item.index, item.timestamp) for item in series.observations) != reference_grid
        for series in series_rows[1:]
    ):
        invalid("all regime close series must share the exact decision-index timestamp grid")
    observation_count = len(reference_grid)
    for fold in fold_registry.folds:
        if (
            fold.training_indices[-1] >= observation_count
            or fold.test_indices[-1] >= observation_count
        ):
            invalid("regime fold indices exceed decision-close coverage")

    fold_index_ledger_sha256 = _regime_fold_index_ledger_sha256(fold_registry)
    close_provenance = tuple(
        RegimeCloseSourceProvenance(
            asset=series.asset,
            source_id=series.source_id,
            source_sha256=series.source_sha256,
            decision_close_ledger_sha256=_regime_close_ledger_sha256(series),
            observations=len(series.observations),
            first_decision_close_ts=series.observations[0].timestamp,
            last_decision_close_ts=series.observations[-1].timestamp,
        )
        for series in series_rows
    )
    features_by_asset = {
        series.asset: _lagged_regime_features(series, config=config) for series in series_rows
    }
    series_by_asset = {series.asset: series for series in series_rows}

    thresholds: list[EconomicRegimeVolatilityThresholds] = []
    observations: list[EconomicRegimeObservation] = []
    for fold in fold_registry.folds:
        test_set = set(fold.test_indices)
        for asset in config.asset_order:
            lagged_volatility, lagged_trend = features_by_asset[asset]
            eligible_training_features = tuple(
                (index, value)
                for index in fold.training_indices
                if (value := lagged_volatility[index]) is not None
            )
            eligible_training_indices = tuple(index for index, _value in eligible_training_features)
            if not eligible_training_indices:
                invalid(
                    "regime calibration has no eligible lagged volatility for "
                    f"{fold.fold_id}/{asset}"
                )
            if set(eligible_training_indices) & test_set:
                invalid("regime volatility calibration cannot contain test observations")
            training_values = tuple(value for _index, value in eligible_training_features)
            p25 = _nearest_rank_ceiling(training_values, quantile=0.25)
            p50 = _nearest_rank_ceiling(training_values, quantile=0.50)
            p75 = _nearest_rank_ceiling(training_values, quantile=0.75)
            calibration_input_sha256 = evidence_sha256(
                {
                    "asset": asset,
                    "fold_id": fold.fold_id,
                    "training_features": [
                        {
                            "index": index,
                            "lagged_volatility": format(value, ".17g"),
                        }
                        for index, value in zip(
                            eligible_training_indices,
                            training_values,
                            strict=True,
                        )
                    ],
                }
            )
            threshold_digest = evidence_sha256(
                {
                    "calibration_id": config.calibration_id,
                    "asset": asset,
                    "fold_id": fold.fold_id,
                    "calibration_input_sha256": calibration_input_sha256,
                    "volatility_lookback_bars": config.volatility_lookback_bars,
                    "annualization_factor": format(config.annualization_factor, ".17g"),
                    "training_indices": list(eligible_training_indices),
                    "thresholds": [format(value, ".17g") for value in (p25, p50, p75)],
                }
            )
            threshold_source_id = (
                f"{config.calibration_id}:{fold.fold_id}:{asset}:{threshold_digest[:16]}"
            )
            thresholds.append(
                EconomicRegimeVolatilityThresholds(
                    asset=asset,
                    fold_id=fold.fold_id,
                    threshold_source_id=threshold_source_id,
                    registered_training_indices=fold.training_indices,
                    volatility_feature_training_indices=eligible_training_indices,
                    registered_test_indices=fold.test_indices,
                    calibration_input_sha256=calibration_input_sha256,
                    calibration_test_observations=0,
                    volatility_training_observations=len(training_values),
                    p25=p25,
                    p50=p50,
                    p75=p75,
                )
            )
            series = series_by_asset[asset]
            for index in fold.test_indices:
                volatility = lagged_volatility[index]
                trend = lagged_trend[index]
                volatility_bucket = (
                    "unclassified"
                    if volatility is None
                    else _regime_volatility_bucket(volatility, p25=p25, p50=p50, p75=p75)
                )
                trend_bucket = (
                    "unclassified"
                    if trend is None
                    else "positive"
                    if trend > 0.0
                    else "nonpositive"
                )
                decision_close = series.observations[index]
                observations.append(
                    EconomicRegimeObservation(
                        asset=asset,
                        fold_id=fold.fold_id,
                        economic_period_date=(decision_close.timestamp - timedelta(days=1)).date(),
                        volatility_bucket=volatility_bucket,
                        trend_bucket=trend_bucket,
                        threshold_source_id=threshold_source_id,
                        unclassified_reason=(
                            "registered_lag_history_unavailable"
                            if "unclassified" in {volatility_bucket, trend_bucket}
                            else None
                        ),
                        decision_index=index,
                        decision_close_ts=decision_close.timestamp,
                    )
                )

    expected_observation_identities = tuple(
        (fold.fold_id, asset, index)
        for fold in fold_registry.folds
        for asset in config.asset_order
        for index in fold.test_indices
    )
    actual_observation_identities = tuple(
        (item.fold_id, item.asset, item.decision_index) for item in observations
    )
    if actual_observation_identities != expected_observation_identities:
        invalid("economic regime output does not exactly cover the registered test grid")
    threshold_ids = tuple(item.threshold_source_id for item in thresholds)
    if len(set(threshold_ids)) != len(threshold_ids):
        invalid("economic regime threshold source identities must be unique")

    provenance = EconomicRegimeCalibrationProvenance(
        config=config,
        close_sources=close_provenance,
        fold_index_source_id=fold_registry.source_id,
        fold_index_source_sha256=fold_registry.source_sha256,
        fold_index_ledger_sha256=fold_index_ledger_sha256,
        return_definition_id=REGIME_RETURN_DEFINITION_ID,
        feature_lag_policy_id=REGIME_FEATURE_LAG_POLICY_ID,
        volatility_estimator_id=REGIME_VOLATILITY_ESTIMATOR_ID,
        quantile_method_id=REGIME_QUANTILE_METHOD_ID,
        volatility_boundary_policy_id=REGIME_VOLATILITY_BOUNDARY_POLICY_ID,
        trend_boundary_policy_id=REGIME_TREND_BOUNDARY_POLICY_ID,
    )
    return EconomicRegimeCalibrationResult(
        provenance=provenance,
        thresholds=tuple(thresholds),
        observations=tuple(observations),
    )
