"""Magnitude-based threshold semantics for ``evaluate_binding_thresholds``.

The user-binding evaluator is the single source of truth for translating
3-tier scores (asset / sector / market) into an execution decision. It
treats long and short signals symmetrically: a score of ``-0.75`` and
``+0.75`` both pass a threshold of ``0.6``, but with opposite direction.

These tests pin down the boundary cases that the rest of the pipeline
depends on:

* sign of the score → derived direction (long / short / neutral),
* magnitude (``abs(score)``) is what the threshold compares against,
* equality at the threshold passes (``>=``, not ``>``),
* sector / market tiers are AND-gated and configurable independently,
* a configured tier that's missing its score fails closed (no silent skip).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lib_strategy.scoring.binding_evaluator import (
    BindingEvaluationResult,
    ThresholdResult,
    evaluate_binding_thresholds,
    evaluate_threshold,
)

# ---------------------------------------------------------------------------
# evaluate_threshold — single tier
# ---------------------------------------------------------------------------


def test_positive_score_above_threshold_passes_long() -> None:
    result = evaluate_threshold(Decimal("0.75"), Decimal("0.6"))
    assert result.passes is True
    assert result.direction == "long"
    assert result.magnitude == Decimal("0.75")


def test_negative_score_above_threshold_passes_short() -> None:
    """``-0.75`` has magnitude ``0.75`` — same threshold pass as ``+0.75``."""
    result = evaluate_threshold(Decimal("-0.75"), Decimal("0.6"))
    assert result.passes is True
    assert result.direction == "short"
    assert result.magnitude == Decimal("0.75")


def test_score_below_threshold_fails_but_keeps_direction() -> None:
    """A failing score still reports its direction so callers can log it."""
    long_fail = evaluate_threshold(Decimal("0.5"), Decimal("0.6"))
    short_fail = evaluate_threshold(Decimal("-0.5"), Decimal("0.6"))
    assert long_fail.passes is False
    assert long_fail.direction == "long"
    assert short_fail.passes is False
    assert short_fail.direction == "short"


def test_score_exactly_equal_to_threshold_passes() -> None:
    """``magnitude >= threshold`` — not strict ``>``."""
    pos = evaluate_threshold(Decimal("0.6"), Decimal("0.6"))
    neg = evaluate_threshold(Decimal("-0.6"), Decimal("0.6"))
    assert pos.passes is True
    assert neg.passes is True


def test_zero_score_is_neutral_and_only_passes_zero_threshold() -> None:
    zero_vs_zero = evaluate_threshold(Decimal("0"), Decimal("0"))
    zero_vs_positive = evaluate_threshold(Decimal("0"), Decimal("0.0001"))
    assert zero_vs_zero.passes is True
    assert zero_vs_zero.direction == "neutral"
    assert zero_vs_positive.passes is False
    assert zero_vs_positive.direction == "neutral"


@pytest.mark.parametrize(
    ("score", "threshold", "expected_passes", "expected_direction"),
    [
        (Decimal("1.0"), Decimal("0.6"), True, "long"),
        (Decimal("0.6"), Decimal("0.6"), True, "long"),
        (Decimal("0.59999"), Decimal("0.6"), False, "long"),
        (Decimal("-1.0"), Decimal("0.6"), True, "short"),
        (Decimal("-0.6"), Decimal("0.6"), True, "short"),
        (Decimal("-0.59999"), Decimal("0.6"), False, "short"),
        (Decimal("0"), Decimal("0.6"), False, "neutral"),
    ],
)
def test_threshold_truth_table(
    score: Decimal,
    threshold: Decimal,
    expected_passes: bool,
    expected_direction: str,
) -> None:
    result = evaluate_threshold(score, threshold)
    assert result.passes is expected_passes
    assert result.direction == expected_direction


def test_threshold_result_to_dict_round_trips_floats() -> None:
    """``to_dict`` emits floats so it can be JSON-serialised for logs/audit."""
    result = evaluate_threshold(Decimal("-0.75"), Decimal("0.6"))
    payload = result.to_dict()
    assert payload == {
        "passes": True,
        "score_value": -0.75,
        "magnitude": 0.75,
        "threshold": 0.6,
        "direction": "short",
    }


# ---------------------------------------------------------------------------
# evaluate_binding_thresholds — 3-tier AND logic
# ---------------------------------------------------------------------------


def test_asset_only_binding_passes_when_threshold_met() -> None:
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.75"),
        asset_threshold=Decimal("0.6"),
    )
    assert isinstance(result, BindingEvaluationResult)
    assert result.passes_all is True
    assert result.derived_direction == "long"
    assert result.sector_result is None
    assert result.market_result is None


def test_full_3_tier_binding_passes_only_when_all_pass() -> None:
    """All configured tiers must pass — AND logic."""
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.75"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("0.65"),
        sector_threshold=Decimal("0.5"),
        market_score=Decimal("0.55"),
        market_threshold=Decimal("0.5"),
    )
    assert result.passes_all is True
    assert isinstance(result.sector_result, ThresholdResult)
    assert isinstance(result.market_result, ThresholdResult)
    assert result.sector_result.passes is True
    assert result.market_result.passes is True


def test_sector_failure_blocks_overall_pass() -> None:
    """Asset and market pass but sector fails → ``passes_all`` is False."""
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("0.3"),  # < threshold
        sector_threshold=Decimal("0.5"),
        market_score=Decimal("0.7"),
        market_threshold=Decimal("0.5"),
    )
    assert result.passes_all is False
    assert result.asset_result.passes is True
    assert result.sector_result is not None
    assert result.sector_result.passes is False
    assert result.market_result is not None
    assert result.market_result.passes is True


def test_market_failure_blocks_overall_pass() -> None:
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("0.7"),
        sector_threshold=Decimal("0.5"),
        market_score=Decimal("0.4"),  # < threshold
        market_threshold=Decimal("0.5"),
    )
    assert result.passes_all is False
    assert result.market_result is not None
    assert result.market_result.passes is False


def test_short_signal_passes_all_tiers_with_negative_scores() -> None:
    """Symmetric treatment: a strong short across all tiers passes."""
    result = evaluate_binding_thresholds(
        asset_score=Decimal("-0.8"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("-0.7"),
        sector_threshold=Decimal("0.5"),
        market_score=Decimal("-0.6"),
        market_threshold=Decimal("0.5"),
    )
    assert result.passes_all is True
    assert result.derived_direction == "short"
    assert result.sector_result is not None
    assert result.sector_result.direction == "short"
    assert result.market_result is not None
    assert result.market_result.direction == "short"


def test_mixed_directions_still_pass_because_magnitude_is_what_matters() -> None:
    """A short asset with a long sector both pass — magnitude is symmetric.

    Direction reconciliation is the executor's job; the binding evaluator
    only enforces threshold magnitudes.
    """
    result = evaluate_binding_thresholds(
        asset_score=Decimal("-0.8"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("0.7"),
        sector_threshold=Decimal("0.5"),
    )
    assert result.passes_all is True
    assert result.asset_result.direction == "short"
    assert result.sector_result is not None
    assert result.sector_result.direction == "long"
    # Overall direction tracks the asset score (not sector).
    assert result.derived_direction == "short"


def test_sector_threshold_set_but_score_missing_fails_closed() -> None:
    """A configured sector tier with no score = explicit fail, not silent skip."""
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
        sector_score=None,
        sector_threshold=Decimal("0.5"),
    )
    assert result.passes_all is False
    assert result.sector_result is not None
    assert result.sector_result.passes is False
    assert result.sector_result.direction == "neutral"
    assert result.sector_result.magnitude == Decimal("0")


def test_market_threshold_set_but_score_missing_fails_closed() -> None:
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
        market_score=None,
        market_threshold=Decimal("0.5"),
    )
    assert result.passes_all is False
    assert result.market_result is not None
    assert result.market_result.passes is False


def test_unconfigured_tiers_are_silently_skipped() -> None:
    """No threshold + no score ⇒ tier is not present in the result."""
    result = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
        sector_score=None,
        sector_threshold=None,
        market_score=None,
        market_threshold=None,
    )
    assert result.passes_all is True
    assert result.sector_result is None
    assert result.market_result is None


def test_to_dict_includes_only_configured_tiers() -> None:
    asset_only = evaluate_binding_thresholds(
        asset_score=Decimal("0.9"),
        asset_threshold=Decimal("0.6"),
    )
    payload = asset_only.to_dict()
    assert payload["passes_all"] is True
    assert payload["derived_direction"] == "long"
    assert "asset" in payload
    assert "sector" not in payload
    assert "market" not in payload

    full = evaluate_binding_thresholds(
        asset_score=Decimal("-0.9"),
        asset_threshold=Decimal("0.6"),
        sector_score=Decimal("-0.7"),
        sector_threshold=Decimal("0.5"),
        market_score=Decimal("-0.6"),
        market_threshold=Decimal("0.5"),
    )
    full_payload = full.to_dict()
    assert full_payload["passes_all"] is True
    assert full_payload["derived_direction"] == "short"
    assert {"asset", "sector", "market"} <= set(full_payload)


def test_decimal_precision_is_preserved_through_evaluation() -> None:
    """Precision-sensitive scores must not silently round to the wrong side."""
    result = evaluate_threshold(Decimal("0.6000000001"), Decimal("0.6"))
    assert result.passes is True
    assert result.magnitude == Decimal("0.6000000001")

    result = evaluate_threshold(Decimal("0.5999999999"), Decimal("0.6"))
    assert result.passes is False
    assert result.magnitude == Decimal("0.5999999999")
