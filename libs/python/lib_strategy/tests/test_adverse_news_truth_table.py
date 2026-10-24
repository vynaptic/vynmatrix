"""Truth tables for the adverse-news article verdict and instrument gate."""

from __future__ import annotations

import itertools

import pytest

from lib_strategy.adverse_news_questions import (
    UNSETTLED_VERDICTS,
    AdverseNewsAnswers,
    ArticleVerdict,
    GateReason,
    classify_article,
    gate_reason,
)

YES, NO, MID = 0.9, 0.1, 0.5
MATERIAL = (0.0, 0.0, 0.1, 0.9)
IMMATERIAL = (0.9, 0.1, 0.0, 0.0)
UNSETTLED_SEVERITY = (0.25, 0.25, 0.25, 0.25)


def _answers(
    about: float = YES,
    recap: float = NO,
    new: float = YES,
    negative: float = YES,
    confirmed: float = YES,
    severity: tuple[float, float, float, float] = MATERIAL,
) -> AdverseNewsAnswers:
    return AdverseNewsAnswers(
        about_company=about,
        price_recap=recap,
        new_development=new,
        negative_development=negative,
        negative_confirmed=confirmed,
        negative_severity=severity,
    )


def _confirmed_negative(
    severity: tuple[float, float, float, float],
) -> AdverseNewsAnswers:
    return _answers(severity=severity)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (_answers(about=NO), ArticleVerdict.NOT_ABOUT_COMPANY),
        (_answers(about=MID), ArticleVerdict.NOT_ABOUT_COMPANY),
        (_answers(recap=YES), ArticleVerdict.PRICE_RECAP),
        (_answers(new=NO), ArticleVerdict.NOT_NEW),
        (_answers(recap=MID, new=MID, negative=NO), ArticleVerdict.NOT_NEGATIVE),
        (_answers(negative=MID), ArticleVerdict.NEGATIVE_UNSETTLED),
        (_answers(confirmed=NO), ArticleVerdict.NEGATIVE_UNCONFIRMED),
        (_answers(confirmed=MID), ArticleVerdict.NEGATIVE_CONFIRMATION_UNSETTLED),
        (_answers(severity=IMMATERIAL), ArticleVerdict.NEGATIVE_IMMATERIAL),
        (_answers(severity=UNSETTLED_SEVERITY), ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED),
        (_answers(), ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL),
    ],
)
def test_each_truth_table_row(answers: AdverseNewsAnswers, expected: ArticleVerdict) -> None:
    assert classify_article(answers) is expected


@pytest.mark.parametrize(
    ("p", "expected"),
    [(0.70, "yes"), (0.6999, "unsettled"), (0.30, "no"), (0.3001, "unsettled")],
)
def test_threshold_edges_on_about_company(p: float, expected: str) -> None:
    verdict = classify_article(_answers(about=p))
    if expected == "yes":
        assert verdict is not ArticleVerdict.NOT_ABOUT_COMPANY
    else:
        assert verdict is ArticleVerdict.NOT_ABOUT_COMPANY


def test_severity_uses_upper_mass_not_expected_score() -> None:
    # Expected score 1.5, upper mass 0.5 -> unsettled.
    assert (
        classify_article(_confirmed_negative((0.5, 0.0, 0.0, 0.5)))
        is ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED
    )


def test_severity_exact_threshold_sum_is_material() -> None:
    # 0.35 + 0.35 sums to exactly 0.70, the yes threshold; on the threshold is material.
    assert (
        classify_article(_confirmed_negative((0.3, 0.0, 0.35, 0.35)))
        is ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL
    )


def test_severity_exact_no_threshold_is_immaterial() -> None:
    assert (
        classify_article(_confirmed_negative((0.5, 0.2, 0.1, 0.2)))
        is ArticleVerdict.NEGATIVE_IMMATERIAL
    )


def test_unsettled_verdicts_are_rows_5_7_9() -> None:
    assert (
        frozenset(
            {
                ArticleVerdict.NEGATIVE_UNSETTLED,
                ArticleVerdict.NEGATIVE_CONFIRMATION_UNSETTLED,
                ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED,
            }
        )
        == UNSETTLED_VERDICTS
    )


def test_gate_reason_order() -> None:
    assert (
        gate_reason(
            coverage_complete=False,
            evidence_valid=False,
            verdicts=[ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL],
        )
        is GateReason.NEWS_COVERAGE_UNAVAILABLE
    )
    assert (
        gate_reason(coverage_complete=True, evidence_valid=False, verdicts=[])
        is GateReason.NEWS_EVIDENCE_INVALID
    )
    assert (
        gate_reason(
            coverage_complete=True,
            evidence_valid=True,
            verdicts=[ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL],
        )
        is GateReason.MATERIAL_ADVERSE_NEWS
    )
    assert (
        gate_reason(
            coverage_complete=True,
            evidence_valid=True,
            verdicts=[ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED],
        )
        is None
    )


def test_every_answer_combination_has_one_verdict() -> None:
    severities = (MATERIAL, IMMATERIAL, UNSETTLED_SEVERITY)
    for combo in itertools.product((NO, MID, YES), repeat=5):
        for severity in severities:
            about, recap, new, negative, confirmed = combo
            verdict = classify_article(_answers(about, recap, new, negative, confirmed, severity))
            assert isinstance(verdict, ArticleVerdict)
