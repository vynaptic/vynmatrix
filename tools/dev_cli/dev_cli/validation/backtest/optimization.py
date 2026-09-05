"""Selection-bias diagnostics used by registered historical validation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

_MEDIAN_RANK_PERCENTILE = 0.5
_MIN_PBO_CANDIDATES = 2


@dataclass(frozen=True)
class BacktestOverfittingDiagnostic:
    """PBO diagnostic over precomputed CPCV train/test candidate scores."""

    splits: int
    probability_of_backtest_overfitting: float
    registered_candidate_order: tuple[str, ...]
    selected_candidates: tuple[str, ...]
    out_of_sample_rank_percentiles: tuple[float, ...]
    rank_logits: tuple[float, ...]
    evidence_role: str = field(default="retrospective_diagnostic", init=False)


def probability_of_backtest_overfitting(
    training_scores: Sequence[Mapping[str, float]],
    test_scores: Sequence[Mapping[str, float]],
    *,
    registered_candidate_order: Sequence[str],
) -> BacktestOverfittingDiagnostic:
    """Estimate PBO from aligned CPCV candidate-score mappings.

    For each split, the best in-sample candidate is ranked out of sample.  A
    training tie is resolved by the immutable ``registered_candidate_order``;
    mapping insertion order and lexical candidate names never affect selection.
    Out-of-sample ties receive their average rank.  A rank percentile at or
    below one half is counted as overfit.  Inputs should come from the explicitly
    retrospective CPCV splits in :mod:`folds`; this function cannot turn them
    into promotion evidence.
    """

    if not training_scores or len(training_scores) != len(test_scores):
        msg = "training_scores and test_scores must be non-empty and aligned"
        raise ValueError(msg)
    registered_order = tuple(registered_candidate_order)
    if len(registered_order) < _MIN_PBO_CANDIDATES:
        msg = "registered_candidate_order must contain at least two candidates"
        raise ValueError(msg)
    if any(not isinstance(candidate, str) or not candidate for candidate in registered_order):
        msg = "registered_candidate_order must contain non-empty candidate names"
        raise ValueError(msg)
    if len(set(registered_order)) != len(registered_order):
        msg = "registered_candidate_order must contain unique candidates"
        raise ValueError(msg)

    selected_candidates: list[str] = []
    percentiles: list[float] = []
    logits: list[float] = []
    overfit = 0
    registered_candidates = set(registered_order)
    for training, testing in zip(training_scores, test_scores, strict=True):
        if not training or set(training) != set(testing):
            msg = "every split must contain the same non-empty candidate set"
            raise ValueError(msg)
        if set(training) != registered_candidates:
            msg = "registered_candidate_order must match every split's candidate set"
            raise ValueError(msg)
        if any(
            not math.isfinite(float(score)) for score in [*training.values(), *testing.values()]
        ):
            msg = "candidate scores must be finite"
            raise ValueError(msg)
        best_training_score = max(float(score) for score in training.values())
        training_ties = tuple(
            candidate
            for candidate in registered_order
            if float(training[candidate]) == best_training_score
        )
        selected = training_ties[0]
        selected_candidates.append(selected)
        selected_test_score = float(testing[selected])
        count = len(testing)
        # Rank scores from worst=1 to best=count.  Equal scores occupy the
        # average of their rank positions; a unique best therefore scores 1.0.
        below = sum(float(score) < selected_test_score for score in testing.values())
        equal = sum(float(score) == selected_test_score for score in testing.values())
        average_rank = below + (equal + 1.0) / 2.0
        percentile = average_rank / count
        percentiles.append(percentile)
        bounded = min(max(percentile, 1e-12), 1.0 - 1e-12)
        logit = math.log(bounded / (1.0 - bounded))
        logits.append(logit)
        if percentile <= _MEDIAN_RANK_PERCENTILE:
            overfit += 1
    return BacktestOverfittingDiagnostic(
        splits=len(training_scores),
        probability_of_backtest_overfitting=overfit / len(training_scores),
        registered_candidate_order=registered_order,
        selected_candidates=tuple(selected_candidates),
        out_of_sample_rank_percentiles=tuple(percentiles),
        rank_logits=tuple(logits),
    )
