"""Internal validation helpers shared by focused diagnostic modules."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Never

CALENDAR_DAYS_PER_YEAR = 365.0
EXPECTED_CSCV_YEARS = 6
EXPECTED_CSCV_TEST_YEARS = 3
MIN_PBO_CANDIDATES = 2
MAX_ABS_OBSERVED_LAG1 = 0.94
MIN_LAG1_CORRELATION_OBSERVATIONS = 3
REPORT_SHA256_HEX_LENGTH = 64
REPORT_EQUITY_POINT_LENGTH = 2
PERCENT_MAX = 100.0
ECONOMIC_PERIOD_DATE_RULE = "normalized_close_timestamp_minus_one_calendar_day"
MIN_DSR_DISPERSION_VALUES = 2
REGIME_RETURN_DEFINITION_ID = "simple_close_to_close_return"
REGIME_FEATURE_LAG_POLICY_ID = "strictly_prior_decision_closes"
REGIME_VOLATILITY_ESTIMATOR_ID = "sample_stdev_annualized"
REGIME_QUANTILE_METHOD_ID = "nearest_rank_ceiling"
REGIME_VOLATILITY_BOUNDARY_POLICY_ID = "q1_lte_p25_q2_lte_p50_q3_lte_p75_q4_gt_p75"
REGIME_TREND_BOUNDARY_POLICY_ID = "positive_gt_zero_nonpositive_lte_zero"


def invalid(message: str) -> Never:
    raise ValueError(message)


def require_nonempty(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        invalid(f"{field_name} must not be blank")


def require_finite(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        invalid(f"{field_name} must be finite")


def require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        invalid(f"{field_name} must be UTC timezone-aware")
