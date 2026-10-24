from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_QUESTIONS,
    MAX_BODY_CHARACTERS,
    AdverseNewsAnswerError,
    AdverseNewsAnswers,
    AdverseNewsArticle,
    AdverseNewsCompany,
    adverse_news_request_body,
    article_skip_reason,
    build_adverse_news_state,
    inference_configuration_sha256,
    model_artifact_sha256,
    output_schema_sha256,
    prompt_schema_sha256,
)

_RESPONSE: dict[str, object] = {
    "model": "jev-1.14.0",
    "answers": {
        "about_company": {"type": "noul", "noul": 0.9},
        "price_recap": {"type": "noul", "noul": 0.1},
        "new_development": {"type": "noul", "noul": 0.8},
        "negative_development": {"type": "noul", "noul": 0.75},
        "negative_confirmed": {"type": "noul", "noul": 0.6},
        "negative_severity": {
            "type": "score",
            "score": 1.7,
            "confidence": 0.8,
            "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
            "probabilities": {"0": 0.1, "1": 0.5, "2": 0.3, "3": 0.1},
        },
    },
    "usage": {"input_tokens": 100},
}
_COMPANY = AdverseNewsCompany("Example Corp", "EXMP", "Health Care", "Medical Devices")


def _article(**overrides: object) -> AdverseNewsArticle:
    ts = datetime(2026, 11, 3, 13, 5, tzinfo=UTC)
    fields: dict[str, object] = {
        "provider_article_id": "a1",
        "headline": "h",
        "source": "s",
        "body": "Some body.",
        "language": "en",
        "document_type": "news",
        "published_at": ts,
        "source_available_at": ts,
    }
    fields.update(overrides)
    return AdverseNewsArticle(**fields)  # type: ignore[arg-type]


def _response(**answer_overrides: object) -> dict[str, object]:
    answers = {**_RESPONSE["answers"], **answer_overrides}  # type: ignore[dict-item]
    return {**_RESPONSE, "answers": answers}


def test_questions_match_spec_ids_and_types() -> None:
    assert {k: v["type"] for k, v in ADVERSE_NEWS_QUESTIONS.items()} == {
        "about_company": "noul",
        "price_recap": "noul",
        "new_development": "noul",
        "negative_development": "noul",
        "negative_confirmed": "noul",
        "negative_severity": "score",
    }
    assert len(ADVERSE_NEWS_QUESTIONS["negative_severity"]["criteria"]) == 4  # type: ignore[arg-type]


def test_request_body_pins_model() -> None:
    body = adverse_news_request_body({"x": 1})
    assert body["model"] == "jev-1.14.0"
    assert body["state"] == {"x": 1}
    assert body["questions"] == {k: dict(v) for k, v in ADVERSE_NEWS_QUESTIONS.items()}


def test_state_omits_empty_other_names() -> None:
    state, truncated = build_adverse_news_state(_COMPANY, _article())
    assert "other_names" not in state["company"]  # type: ignore[operator]
    assert truncated is False
    named = AdverseNewsCompany("E", "E", "s", "i", other_names=("Alias",))
    state, _ = build_adverse_news_state(named, _article())
    assert state["company"]["other_names"] == ["Alias"]  # type: ignore[index]


def test_state_truncates_on_paragraph_boundary() -> None:
    para = "x" * 1000
    body = "\n\n".join([para] * 100)
    state, truncated = build_adverse_news_state(_COMPANY, _article(body=body))
    out = state["article"]["body"]  # type: ignore[index]
    assert truncated is True
    assert len(out) <= MAX_BODY_CHARACTERS
    assert out.split("\n\n")[-1] == para
    assert not out.endswith("\n")


def test_state_normalizes_published_at_to_utc() -> None:
    local = datetime(2026, 11, 3, 15, 5, tzinfo=timezone(timedelta(hours=2)))
    state, _ = build_adverse_news_state(_COMPANY, _article(published_at=local))
    assert state["article"]["published_at"] == "2026-11-03T13:05:00Z"  # type: ignore[index]


def test_article_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _article(published_at=datetime(2026, 11, 3))


def test_skip_reasons() -> None:
    assert article_skip_reason(_article(body="  ")) == "empty_body"
    assert article_skip_reason(_article(language="de")) == "language_not_en"
    assert article_skip_reason(_article(document_type="blog")) == "document_type_not_allowed"
    assert article_skip_reason(_article()) is None


def test_from_response_parses() -> None:
    answers = AdverseNewsAnswers.from_response(_RESPONSE)
    assert answers.about_company == 0.9
    assert answers.negative_severity == (0.1, 0.5, 0.3, 0.1)


def test_from_response_rejects_wrong_model() -> None:
    with pytest.raises(AdverseNewsAnswerError, match="model"):
        AdverseNewsAnswers.from_response({**_RESPONSE, "model": "jev-1.13.0"})


def test_from_response_rejects_missing_answer() -> None:
    answers = dict(_RESPONSE["answers"])  # type: ignore[call-overload]
    del answers["price_recap"]
    with pytest.raises(AdverseNewsAnswerError, match="price_recap"):
        AdverseNewsAnswers.from_response({**_RESPONSE, "answers": answers})


def test_from_response_rejects_wrong_type() -> None:
    with pytest.raises(AdverseNewsAnswerError, match="type"):
        AdverseNewsAnswers.from_response(_response(price_recap={"type": "score", "noul": 0.1}))


def test_from_response_rejects_probability_out_of_range() -> None:
    with pytest.raises(AdverseNewsAnswerError, match="within"):
        AdverseNewsAnswers.from_response(_response(about_company={"type": "noul", "noul": 1.2}))


def _severity(probabilities: dict[str, float]) -> dict[str, object]:
    return {
        "type": "score",
        "score": 1.0,
        "confidence": 0.5,
        "legend": {},
        "probabilities": probabilities,
    }


def test_from_response_rejects_severity_not_summing_to_one() -> None:
    bad = _severity({"0": 0.5, "1": 0.5, "2": 0.5, "3": 0.0})
    with pytest.raises(AdverseNewsAnswerError, match="sum"):
        AdverseNewsAnswers.from_response(_response(negative_severity=bad))


def test_from_response_rejects_wrong_severity_keys() -> None:
    bad = _severity({"0": 0.5, "1": 0.5, "2": 0.0})
    with pytest.raises(AdverseNewsAnswerError, match="keys"):
        AdverseNewsAnswers.from_response(_response(negative_severity=bad))


def test_from_values_round_trips_from_response() -> None:
    parsed = AdverseNewsAnswers.from_response(_RESPONSE)
    values = {
        "about_company_probability": [(0, Decimal("0.9"))],
        "price_recap_probability": [(0, Decimal("0.1"))],
        "new_development_probability": [(0, Decimal("0.8"))],
        "negative_development_probability": [(0, Decimal("0.75"))],
        "negative_confirmed_probability": [(0, Decimal("0.6"))],
        "negative_severity_probability": [
            (3, Decimal("0.1")),
            (0, Decimal("0.1")),
            (1, Decimal("0.5")),
            (2, Decimal("0.3")),
        ],
    }
    assert AdverseNewsAnswers.from_values(values) == parsed


def test_from_values_rejects_missing_severity_ordinal() -> None:
    values = {
        "about_company_probability": [(0, Decimal("0.9"))],
        "price_recap_probability": [(0, Decimal("0.1"))],
        "new_development_probability": [(0, Decimal("0.8"))],
        "negative_development_probability": [(0, Decimal("0.75"))],
        "negative_confirmed_probability": [(0, Decimal("0.6"))],
        "negative_severity_probability": [(0, Decimal("1"))],
    }
    with pytest.raises(AdverseNewsAnswerError, match="ordinals"):
        AdverseNewsAnswers.from_values(values)


def test_digests_are_stable_and_exclude_secrets() -> None:
    assert prompt_schema_sha256() == prompt_schema_sha256()
    assert prompt_schema_sha256() != output_schema_sha256()
    assert inference_configuration_sha256() == inference_configuration_sha256()
    assert model_artifact_sha256("jev-1.14.0") != model_artifact_sha256("jev-1.13.0")
    assert "Bearer" not in json.dumps(adverse_news_request_body({}))


def test_state_truncates_oversized_first_paragraph_at_sentence() -> None:
    body = "Alpha beta gamma. " * 10_000
    state, truncated = build_adverse_news_state(_COMPANY, _article(body=body))
    out = state["article"]["body"]  # type: ignore[index]
    assert truncated is True
    assert out
    assert len(out) <= MAX_BODY_CHARACTERS
    assert out.endswith("gamma.")


def test_state_truncates_oversized_unpunctuated_paragraph_at_whitespace() -> None:
    body = "word " * 30_000
    state, truncated = build_adverse_news_state(_COMPANY, _article(body=body))
    out = state["article"]["body"]  # type: ignore[index]
    assert truncated is True
    assert out.endswith("word")
    assert len(out) <= MAX_BODY_CHARACTERS


def test_state_normalizes_crlf_paragraphs() -> None:
    para = "x" * 1000
    body = "\r\n\r\n".join([para] * 100)
    state, truncated = build_adverse_news_state(_COMPANY, _article(body=body))
    out = state["article"]["body"]  # type: ignore[index]
    assert truncated is True
    assert "\r" not in out
    assert out.split("\n\n")[-1] == para


def test_request_body_mutation_does_not_change_digest() -> None:
    before = prompt_schema_sha256()
    body = adverse_news_request_body({})
    body["questions"]["about_company"]["criteria"]["true"] = "changed"  # type: ignore[index]
    body["questions"]["negative_severity"]["criteria"][0] = "changed"  # type: ignore[index]
    assert prompt_schema_sha256() == before
