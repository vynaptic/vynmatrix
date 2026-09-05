"""Exact-calendar combinatorially symmetric cross-validation diagnostics."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dev_cli.validation._diagnostic_common import (
    EXPECTED_CSCV_TEST_YEARS,
    EXPECTED_CSCV_YEARS,
    MIN_PBO_CANDIDATES,
    invalid,
    require_finite,
    require_nonempty,
    require_utc,
)
from dev_cli.validation.backtest.metrics import sharpe_ratio
from dev_cli.validation.backtest.optimization import probability_of_backtest_overfitting


@dataclass(frozen=True, slots=True)
class TimestampedDailyReturn:
    """One UTC calendar-day return in a registered candidate series."""

    timestamp: datetime
    daily_return: float

    def __post_init__(self) -> None:
        require_utc(self.timestamp, field_name="timestamp")
        require_finite(self.daily_return, field_name="daily_return")

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "daily_return": self.daily_return,
        }


@dataclass(frozen=True, slots=True)
class CandidateDailyReturnSeries:
    """One candidate's strictly ordered, duplicate-free daily return ledger."""

    candidate_id: str
    observations: tuple[TimestampedDailyReturn, ...]

    def __post_init__(self) -> None:
        require_nonempty(self.candidate_id, field_name="candidate_id")
        if not self.observations:
            invalid("candidate daily return observations must not be empty")
        timestamps = tuple(item.timestamp for item in self.observations)
        if any(current <= previous for previous, current in itertools.pairwise(timestamps)):
            invalid("candidate daily return timestamps must be strictly increasing")
        calendar_days = tuple(timestamp.date() for timestamp in timestamps)
        if len(set(calendar_days)) != len(calendar_days):
            invalid("candidate daily return series must not contain duplicate UTC days")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "observations": [item.to_dict() for item in self.observations],
        }


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """One registered candidate's score within one CSCV partition."""

    candidate_id: str
    score: float

    def __post_init__(self) -> None:
        require_nonempty(self.candidate_id, field_name="candidate_id")
        require_finite(self.score, field_name="score")

    def to_dict(self) -> dict[str, object]:
        return {"candidate_id": self.candidate_id, "score": self.score}


@dataclass(frozen=True, slots=True)
class CalendarYearCSCVSplit:
    """One exact 3-year/3-year split in registered combination order."""

    index: int
    training_years: tuple[int, ...]
    test_years: tuple[int, ...]
    training_scores: tuple[CandidateScore, ...]
    test_scores: tuple[CandidateScore, ...]
    selected_candidate_id: str | None
    selected_oos_rank_percentile: float | None
    selected_oos_rank_logit: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "training_years": list(self.training_years),
            "test_years": list(self.test_years),
            "training_scores": [item.to_dict() for item in self.training_scores],
            "test_scores": [item.to_dict() for item in self.test_scores],
            "selected_candidate_id": self.selected_candidate_id,
            "selected_oos_rank_percentile": self.selected_oos_rank_percentile,
            "selected_oos_rank_logit": self.selected_oos_rank_logit,
        }


@dataclass(frozen=True, slots=True)
class CalendarYearCSCVPBOResult:
    """Exact-calendar CSCV construction and an availability-aware PBO result."""

    calendar_years: tuple[int, ...]
    registered_candidate_order: tuple[str, ...]
    aligned_calendar_days: int
    score_metric: str
    annualization_factor: float
    splits: tuple[CalendarYearCSCVSplit, ...]
    informative_score_profiles: int
    probability_of_backtest_overfitting: float | None
    unavailable_reason: str | None
    evidence_role: str = field(default="retrospective_diagnostic_only", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "calendar_years": list(self.calendar_years),
            "registered_candidate_order": list(self.registered_candidate_order),
            "aligned_calendar_days": self.aligned_calendar_days,
            "score_metric": self.score_metric,
            "annualization_factor": self.annualization_factor,
            "splits": [item.to_dict() for item in self.splits],
            "informative_score_profiles": self.informative_score_profiles,
            "probability_of_backtest_overfitting": self.probability_of_backtest_overfitting,
            "unavailable_reason": self.unavailable_reason,
            "evidence_role": self.evidence_role,
        }


def _complete_calendar_dates(calendar_years: Sequence[int]) -> tuple[date, ...]:
    """Return every calendar date in the explicitly supplied year span."""
    start = date(calendar_years[0], 1, 1)
    end_exclusive = date(calendar_years[-1] + 1, 1, 1)
    count = (end_exclusive - start).days
    return tuple(start + timedelta(days=offset) for offset in range(count))


def _registered_calendar_dates(
    calendar_years: tuple[int, ...],
    registered_calendar_dates: Sequence[date],
) -> tuple[date, ...]:
    expected_dates = tuple(registered_calendar_dates)
    if not expected_dates:
        invalid("registered_calendar_dates must not be empty")
    if any(isinstance(item, datetime) or not isinstance(item, date) for item in expected_dates):
        invalid("registered_calendar_dates must contain dates")
    if any(current <= previous for previous, current in itertools.pairwise(expected_dates)):
        invalid("registered_calendar_dates must be strictly increasing and unique")
    if any(item.year not in calendar_years for item in expected_dates):
        invalid("registered_calendar_dates must remain inside calendar_years")
    if {item.year for item in expected_dates} != set(calendar_years):
        invalid("registered_calendar_dates must cover every calendar_year")
    return expected_dates


def build_exact_calendar_cscv_pbo(
    candidate_series: Sequence[CandidateDailyReturnSeries],
    *,
    registered_candidate_order: Sequence[str],
    calendar_years: Sequence[int],
    annualization_factor: float,
    registered_calendar_dates: Sequence[date],
) -> CalendarYearCSCVPBOResult:
    """Build all 20 registered-calendar 3/3 CSCV splits and calculate PBO.

    Each registered candidate must provide exactly one UTC observation for
    every pre-registered observation date in the six consecutive registered
    years. The pre-registered observation calendar is mandatory. Missing dates,
    extra dates, duplicated dates, misaligned candidates, and unregistered
    candidates fail closed. PBO remains unavailable when fewer than two distinct
    score profiles exist; an all-tied registry is not evidence.
    """

    years = tuple(calendar_years)
    if any(isinstance(year, bool) or not isinstance(year, int) for year in years):
        invalid("calendar_years must contain integers")
    if len(years) != EXPECTED_CSCV_YEARS or len(set(years)) != len(years):
        invalid("calendar_years must contain exactly six unique years")
    if years != tuple(range(years[0], years[0] + EXPECTED_CSCV_YEARS)):
        invalid("calendar_years must be six strictly consecutive years")
    require_finite(annualization_factor, field_name="annualization_factor")
    if annualization_factor <= 0.0:
        invalid("annualization_factor must be positive")

    registered = tuple(registered_candidate_order)
    if len(registered) < MIN_PBO_CANDIDATES:
        invalid("registered_candidate_order must contain at least two candidates")
    if any(not isinstance(item, str) or not item.strip() for item in registered):
        invalid("registered_candidate_order must contain non-empty candidate ids")
    if len(set(registered)) != len(registered):
        invalid("registered_candidate_order must contain unique candidates")

    series = tuple(candidate_series)
    series_by_id = {item.candidate_id: item for item in series}
    if len(series_by_id) != len(series):
        invalid("candidate_series must contain unique candidate ids")
    if set(series_by_id) != set(registered):
        invalid("candidate_series must exactly match registered_candidate_order")

    expected_dates = _registered_calendar_dates(years, registered_calendar_dates)
    expected_date_set = set(expected_dates)
    returns_by_candidate: dict[str, dict[date, float]] = {}
    timestamp_grid: tuple[datetime, ...] | None = None
    for candidate_id in registered:
        candidate = series_by_id[candidate_id]
        dates = tuple(item.timestamp.date() for item in candidate.observations)
        observed_set = set(dates)
        if observed_set != expected_date_set or len(dates) != len(expected_dates):
            missing = len(expected_date_set - observed_set)
            extra = len(observed_set - expected_date_set)
            invalid(
                f"candidate {candidate_id} must cover every registered calendar day "
                f"exactly once (missing={missing}, extra={extra})"
            )
        timestamps = tuple(item.timestamp for item in candidate.observations)
        if timestamp_grid is None:
            timestamp_grid = timestamps
        elif timestamps != timestamp_grid:
            invalid("candidate daily return timestamps must be exactly aligned")
        returns_by_candidate[candidate_id] = {
            item.timestamp.date(): item.daily_return for item in candidate.observations
        }

    test_year_sets = tuple(itertools.combinations(years, EXPECTED_CSCV_TEST_YEARS))
    training_maps: list[dict[str, float]] = []
    test_maps: list[dict[str, float]] = []
    split_years: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for test_years in test_year_sets:
        test_set = set(test_years)
        training_years = tuple(year for year in years if year not in test_set)
        training_dates = tuple(day for day in expected_dates if day.year in training_years)
        test_dates = tuple(day for day in expected_dates if day.year in test_set)
        training = {
            candidate_id: sharpe_ratio(
                [returns_by_candidate[candidate_id][day] for day in training_dates],
                annualization_factor=annualization_factor,
            )
            for candidate_id in registered
        }
        testing = {
            candidate_id: sharpe_ratio(
                [returns_by_candidate[candidate_id][day] for day in test_dates],
                annualization_factor=annualization_factor,
            )
            for candidate_id in registered
        }
        training_maps.append(training)
        test_maps.append(testing)
        split_years.append((training_years, test_years))

    score_profiles = {
        tuple(
            [training[candidate_id] for training in training_maps]
            + [testing[candidate_id] for testing in test_maps]
        )
        for candidate_id in registered
    }
    informative_profiles = len(score_profiles)
    pbo_available = informative_profiles >= MIN_PBO_CANDIDATES
    diagnostic = (
        probability_of_backtest_overfitting(
            training_maps,
            test_maps,
            registered_candidate_order=registered,
        )
        if pbo_available
        else None
    )

    splits: list[CalendarYearCSCVSplit] = []
    for index, ((training_years, test_years), training, testing) in enumerate(
        zip(split_years, training_maps, test_maps, strict=True)
    ):
        splits.append(
            CalendarYearCSCVSplit(
                index=index,
                training_years=training_years,
                test_years=test_years,
                training_scores=tuple(
                    CandidateScore(candidate_id, training[candidate_id])
                    for candidate_id in registered
                ),
                test_scores=tuple(
                    CandidateScore(candidate_id, testing[candidate_id])
                    for candidate_id in registered
                ),
                selected_candidate_id=(
                    diagnostic.selected_candidates[index] if diagnostic is not None else None
                ),
                selected_oos_rank_percentile=(
                    diagnostic.out_of_sample_rank_percentiles[index]
                    if diagnostic is not None
                    else None
                ),
                selected_oos_rank_logit=(
                    diagnostic.rank_logits[index] if diagnostic is not None else None
                ),
            )
        )
    return CalendarYearCSCVPBOResult(
        calendar_years=years,
        registered_candidate_order=registered,
        aligned_calendar_days=len(expected_dates),
        score_metric="annualized_sharpe_of_expected_cost_daily_returns",
        annualization_factor=annualization_factor,
        splits=tuple(splits),
        informative_score_profiles=informative_profiles,
        probability_of_backtest_overfitting=(
            diagnostic.probability_of_backtest_overfitting if diagnostic is not None else None
        ),
        unavailable_reason=(
            None
            if diagnostic is not None
            else "fewer_than_two_informative_non_tied_candidate_score_profiles"
        ),
    )
