"""TypeSafe adverse-news question set: state, request, answers, and digests.

Pure domain code with no I/O. This module is the single source of truth for the
question wording and criteria, the question-set version, the thresholds, the
request shape, answer parsing, and the provenance digests. The HTTP client in
the infrastructure layer imports it and never defines wording.

Any change to wording, criteria, thresholds, or question IDs must bump
``ADVERSE_NEWS_QUESTION_SET_VERSION``: that changes the prompt digest, which makes
earlier observations inadmissible for the new version.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, NoReturn

from lib_common.hashing import canonical_json_hash

ADVERSE_NEWS_QUESTION_SET_VERSION: Final = "adverse-news-questions-v1"
ADVERSE_NEWS_MODEL: Final = "jev-1.14.0"
ADVERSE_NEWS_MODEL_RELEASED_ON: Final = date(2026, 10, 14)
TYPESAFE_ENDPOINT: Final = "https://api.typesafe.ai/v1/systemone"

YES_THRESHOLD: Final = 0.70
NO_THRESHOLD: Final = 0.30
LOOKBACK_DAYS: Final = 90
# Conservative stand-in for a 24k-token body budget at about 3 characters per token.
MAX_BODY_CHARACTERS: Final = 72_000
ALLOWED_DOCUMENT_TYPES: Final = frozenset({"news", "press_release", "regulatory_notice"})

_SEVERITY_LEVELS: Final = 4
_SUM_TOLERANCE: Final = 1e-6
# Absorbs float rounding so a sum that is exactly on a threshold counts as on it.
_THRESHOLD_TOLERANCE: Final = 1e-9

ADVERSE_NEWS_QUESTIONS: Final[Mapping[str, Mapping[str, object]]] = {
    "about_company": {
        "type": "noul",
        "instructions": "Is `article` mainly about `company.name`?",
        "criteria": {
            "true": (
                "The article's main subject is this company, its products, its people, "
                "or its results."
            ),
            "false": (
                "The company is mentioned in passing, in a list of several companies, "
                "as a competitor, or as a comparison."
            ),
        },
    },
    "price_recap": {
        "type": "noul",
        "instructions": (
            "Is `article` mainly a report that the share price of `company.name` moved?"
        ),
        "criteria": {
            "true": (
                "The article mainly describes a share-price move, trading volume, or "
                "analyst price-target changes, without reporting a new event at the company."
            ),
            "false": (
                "The article reports an event at the company, even if it also mentions "
                "the share price."
            ),
        },
    },
    "negative_development": {
        "type": "noul",
        "instructions": "Does `article` report a development that harms `company.name`?",
        "criteria": {
            "true": (
                "The article reports an event that hurts the company, such as a loss of "
                "customers, a regulatory action, a lawsuit, an accounting problem, a "
                "recall, a guidance cut, or a leadership departure under pressure."
            ),
            "false": (
                "The article reports only good news, neutral news, or an event that has "
                "no adverse effect on the company."
            ),
        },
    },
    "negative_confirmed": {
        "type": "noul",
        "instructions": (
            "Has the harmful development in `article` happened or been officially announced?"
        ),
        "criteria": {
            "true": (
                "The company, a regulator, a court, or another named authority has "
                "announced it, or the article reports it as an established fact."
            ),
            "false": (
                "The development is a rumour, comes from unnamed sources, is an "
                "analyst's speculation, or is described as a possibility."
            ),
        },
    },
    "negative_severity": {
        "type": "score",
        "instructions": "How badly could the harmful development in `article` hurt `company.name`?",
        "criteria": [
            "A routine setback that is unlikely to change the company's earnings outlook.",
            "A setback that could hurt the company's results for about one quarter.",
            "A setback that could reduce the company's earnings for a year or more.",
            "A setback that could threaten the company's survival, independence, or core business.",
        ],
    },
    "new_development": {
        "type": "noul",
        "instructions": (
            "Does `article` report something about `company.name` that was not public "
            "before the article's `published_at`?"
        ),
        "criteria": {
            "true": (
                "The article is the first report of an event, an announcement, or new "
                "facts about an earlier event."
            ),
            "false": (
                "The article recaps earlier news, previews a scheduled event, or repeats "
                "facts that were already public."
            ),
        },
    },
}


class AdverseNewsAnswerError(ValueError):
    """A TypeSafe response or stored answer set is malformed or untrustworthy."""


def _reject(message: str) -> NoReturn:
    raise AdverseNewsAnswerError(message)


@dataclass(frozen=True, slots=True)
class AdverseNewsCompany:
    """Catalogued security identity; never derived from the article."""

    name: str
    ticker: str
    sector: str
    industry: str
    other_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdverseNewsArticle:
    """One provider article with its original publication and availability times."""

    provider_article_id: str
    headline: str
    source: str
    body: str
    language: str
    document_type: str
    published_at: datetime
    source_available_at: datetime

    def __post_init__(self) -> None:
        for name in ("published_at", "source_available_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                message = f"{name} must be timezone-aware"
                raise ValueError(message)


def article_skip_reason(article: AdverseNewsArticle) -> str | None:
    """Return why an article is not judged, or ``None`` when it should be."""
    if article.language.strip().lower() != "en":
        return "language_not_en"
    if article.document_type not in ALLOWED_DOCUMENT_TYPES:
        return "document_type_not_allowed"
    if not article.body.strip():
        return "empty_body"
    return None


def _truncate_body(body: str) -> tuple[str, bool]:
    """Keep whole paragraphs; if the first alone is too long, cut at a sentence end."""
    text = body.replace("\r\n", "\n").strip()
    if len(text) <= MAX_BODY_CHARACTERS:
        return text, False
    cut = text.rfind("\n\n", 0, MAX_BODY_CHARACTERS + 2)
    if cut > 0:
        return text[:cut].rstrip(), True
    window = text[:MAX_BODY_CHARACTERS]
    sentence_end = max(window.rfind(mark) for mark in (". ", "? ", "! "))
    if sentence_end > 0:
        return window[: sentence_end + 1], True
    whitespace = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    if whitespace > 0:
        return window[:whitespace].rstrip(), True
    return window, True


def build_adverse_news_state(
    company: AdverseNewsCompany, article: AdverseNewsArticle
) -> tuple[dict[str, object], bool]:
    """Build the request state and report whether the body was truncated."""
    body, truncated = _truncate_body(article.body)
    company_state: dict[str, object] = {
        "name": company.name,
        "ticker": company.ticker,
        "sector": company.sector,
        "industry": company.industry,
    }
    if company.other_names:
        company_state["other_names"] = list(company.other_names)
    published = article.published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state: dict[str, object] = {
        "company": company_state,
        "article": {
            "headline": article.headline,
            "source": article.source,
            "published_at": published,
            "body": body,
        },
    }
    return state, truncated


def adverse_news_request_body(state: Mapping[str, object]) -> dict[str, object]:
    """Return the exact request body: pinned model, state, and questions."""
    return {
        "model": ADVERSE_NEWS_MODEL,
        "state": dict(state),
        "questions": copy.deepcopy(dict(ADVERSE_NEWS_QUESTIONS)),
    }


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        _reject(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        _reject(f"{label} must be within [0, 1]")
    return number


def _checked_severity(values: Sequence[float]) -> tuple[float, float, float, float]:
    if len(values) != _SEVERITY_LEVELS:
        _reject("negative_severity needs exactly four probabilities")
    if abs(sum(values) - 1.0) > _SUM_TOLERANCE:
        _reject("negative_severity probabilities must sum to 1")
    return (values[0], values[1], values[2], values[3])


@dataclass(frozen=True, slots=True)
class AdverseNewsAnswers:
    """Validated answers to the adverse-news questions for one article."""

    about_company: float
    price_recap: float
    new_development: float
    negative_development: float
    negative_confirmed: float
    negative_severity: tuple[float, float, float, float]

    @classmethod
    def from_response(cls, response: Mapping[str, object]) -> AdverseNewsAnswers:
        """Parse a TypeSafe response, failing closed on any deviation."""
        model = response.get("model")
        if model != ADVERSE_NEWS_MODEL:
            _reject(f"response model {model!r} does not match pinned model {ADVERSE_NEWS_MODEL!r}")
        answers = response.get("answers")
        if not isinstance(answers, Mapping):
            _reject("response has no answers object")

        def answer(question_id: str, expected_type: str) -> Mapping[str, object]:
            item = answers.get(question_id)
            if not isinstance(item, Mapping):
                _reject(f"missing answer {question_id}")
            if item.get("type") != expected_type:
                _reject(
                    f"answer {question_id} has type {item.get('type')!r}, not {expected_type!r}"
                )
            return item

        def noul(question_id: str) -> float:
            return _probability(answer(question_id, "noul").get("noul"), question_id)

        severity_item = answer("negative_severity", "score")
        probabilities = severity_item.get("probabilities")
        if not isinstance(probabilities, Mapping) or set(probabilities) != {
            str(level) for level in range(_SEVERITY_LEVELS)
        }:
            _reject("negative_severity probabilities must have keys 0..3")
        severity = _checked_severity(
            [
                _probability(probabilities[str(level)], f"negative_severity[{level}]")
                for level in range(_SEVERITY_LEVELS)
            ]
        )
        return cls(
            about_company=noul("about_company"),
            price_recap=noul("price_recap"),
            new_development=noul("new_development"),
            negative_development=noul("negative_development"),
            negative_confirmed=noul("negative_confirmed"),
            negative_severity=severity,
        )

    @classmethod
    def from_values(cls, values: Mapping[str, Sequence[tuple[int, Decimal]]]) -> AdverseNewsAnswers:
        """Rebuild answers from stored observation values keyed by field name."""

        def single(field: str) -> float:
            rows = values.get(field)
            if rows is None or len(rows) != 1:
                _reject(f"{field} needs exactly one value")
            return _probability(rows[0][1], field)

        rows = values.get("negative_severity_probability")
        if rows is None or sorted(ordinal for ordinal, _ in rows) != list(range(_SEVERITY_LEVELS)):
            _reject("negative_severity_probability needs ordinals 0..3")
        by_ordinal = dict(rows)
        severity = _checked_severity(
            [
                _probability(by_ordinal[level], f"negative_severity_probability[{level}]")
                for level in range(_SEVERITY_LEVELS)
            ]
        )
        return cls(
            about_company=single("about_company_probability"),
            price_recap=single("price_recap_probability"),
            new_development=single("new_development_probability"),
            negative_development=single("negative_development_probability"),
            negative_confirmed=single("negative_confirmed_probability"),
            negative_severity=severity,
        )


class ArticleVerdict(StrEnum):
    """Outcome of the article truth table (spec section 5.1)."""

    NOT_ABOUT_COMPANY = "not_about_company"
    PRICE_RECAP = "price_recap"
    NOT_NEW = "not_new"
    NOT_NEGATIVE = "not_negative"
    NEGATIVE_UNSETTLED = "negative_unsettled"
    NEGATIVE_UNCONFIRMED = "negative_unconfirmed"
    NEGATIVE_CONFIRMATION_UNSETTLED = "negative_confirmation_unsettled"
    NEGATIVE_IMMATERIAL = "negative_immaterial"
    NEGATIVE_SEVERITY_UNSETTLED = "negative_severity_unsettled"
    ADVERSE_CONFIRMED_MATERIAL = "adverse_confirmed_material"


class GateReason(StrEnum):
    """Why an instrument is excluded at the cutoff (spec section 5.2)."""

    NEWS_COVERAGE_UNAVAILABLE = "news_coverage_unavailable"
    NEWS_EVIDENCE_INVALID = "news_evidence_invalid"
    MATERIAL_ADVERSE_NEWS = "material_adverse_news"


UNSETTLED_VERDICTS: Final = frozenset(
    {
        ArticleVerdict.NEGATIVE_UNSETTLED,
        ArticleVerdict.NEGATIVE_CONFIRMATION_UNSETTLED,
        ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED,
    }
)


class _Tri(StrEnum):
    YES = "yes"
    NO = "no"
    UNSETTLED = "unsettled"


def _tri(probability: float) -> _Tri:
    if probability >= YES_THRESHOLD - _THRESHOLD_TOLERANCE:
        return _Tri.YES
    if probability <= NO_THRESHOLD + _THRESHOLD_TOLERANCE:
        return _Tri.NO
    return _Tri.UNSETTLED


def _classify_confirmed(answers: AdverseNewsAnswers) -> ArticleVerdict:
    """Rows 6-10: the article reports a settled negative development."""
    confirmed = _tri(answers.negative_confirmed)
    if confirmed is _Tri.NO:
        return ArticleVerdict.NEGATIVE_UNCONFIRMED
    if confirmed is _Tri.UNSETTLED:
        return ArticleVerdict.NEGATIVE_CONFIRMATION_UNSETTLED
    # Severity reads level probabilities, not the expected score.
    severity = _tri(answers.negative_severity[2] + answers.negative_severity[3])
    if severity is _Tri.NO:
        return ArticleVerdict.NEGATIVE_IMMATERIAL
    if severity is _Tri.UNSETTLED:
        return ArticleVerdict.NEGATIVE_SEVERITY_UNSETTLED
    return ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL


def classify_article(answers: AdverseNewsAnswers) -> ArticleVerdict:
    """Apply spec section 5.1 top to bottom; the first matching row wins."""
    if _tri(answers.about_company) is not _Tri.YES:
        return ArticleVerdict.NOT_ABOUT_COMPANY
    if _tri(answers.price_recap) is _Tri.YES:
        return ArticleVerdict.PRICE_RECAP
    if _tri(answers.new_development) is _Tri.NO:
        return ArticleVerdict.NOT_NEW
    negative = _tri(answers.negative_development)
    if negative is _Tri.NO:
        return ArticleVerdict.NOT_NEGATIVE
    if negative is _Tri.UNSETTLED:
        return ArticleVerdict.NEGATIVE_UNSETTLED
    return _classify_confirmed(answers)


def gate_reason(
    *,
    coverage_complete: bool,
    evidence_valid: bool,
    verdicts: Sequence[ArticleVerdict],
) -> GateReason | None:
    """Apply spec section 5.2 in order; ``None`` means the gate is clear."""
    if not coverage_complete:
        return GateReason.NEWS_COVERAGE_UNAVAILABLE
    if not evidence_valid:
        return GateReason.NEWS_EVIDENCE_INVALID
    if ArticleVerdict.ADVERSE_CONFIRMED_MATERIAL in verdicts:
        return GateReason.MATERIAL_ADVERSE_NEWS
    return None


def prompt_schema_sha256() -> str:
    """Digest of the question-set version and the exact questions sent (no state)."""
    return canonical_json_hash(
        {
            "question_set_version": ADVERSE_NEWS_QUESTION_SET_VERSION,
            "questions": ADVERSE_NEWS_QUESTIONS,
        }
    )


def output_schema_sha256() -> str:
    """Digest of the question-set version and each answer ID with its type."""
    return canonical_json_hash(
        {
            "question_set_version": ADVERSE_NEWS_QUESTION_SET_VERSION,
            "answers": {key: value["type"] for key, value in ADVERSE_NEWS_QUESTIONS.items()},
        }
    )


def inference_configuration_sha256() -> str:
    """Digest of the endpoint and pinned model request settings; never the API key."""
    return canonical_json_hash(
        {
            "endpoint": TYPESAFE_ENDPOINT,
            "model_requested": ADVERSE_NEWS_MODEL,
            "long_context": False,
        }
    )


def model_artifact_sha256(model: str) -> str:
    """Identity digest of the versioned model ID.

    This hashes the model ID, not the weights: TypeSafe publishes no weights digest.
    """
    return canonical_json_hash({"provider": "typesafe", "model": model})
