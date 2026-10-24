from __future__ import annotations

import pytest

from lib_strategy.equity_optional_factors import (
    ADVERSE_NEWS_GATE_CONTRACT,
    NEWS_COVERAGE_GATE_CONTRACT,
    OPTIONAL_FACTOR_SLEEVES,
    OPTIONAL_GATE_SOURCE_CONTRACTS,
    OptionalFactorContractError,
    optional_factor_source_contract,
    optional_factor_source_registry_sha256,
    optional_gate_source_contract,
    optional_gate_source_registry_sha256,
)

# Factor registry digest captured on main before the gate contracts were added.
_REGISTRY_SHA256_BEFORE_GATES = "9eb5899f62939d2e1ac9cca52602afcc35fbbf0c16d59a561758cbebec88fa4d"


def test_factor_registry_digest_is_unchanged() -> None:
    assert optional_factor_source_registry_sha256() == _REGISTRY_SHA256_BEFORE_GATES
    assert "adverse_news" not in OPTIONAL_FACTOR_SLEEVES
    assert "adverse_news_coverage" not in OPTIONAL_FACTOR_SLEEVES


def test_gate_registry_holds_exactly_two_contracts() -> None:
    assert [c.sleeve.value for c in OPTIONAL_GATE_SOURCE_CONTRACTS] == [
        "adverse_news",
        "adverse_news_coverage",
    ]
    assert ADVERSE_NEWS_GATE_CONTRACT.observation_kind.value == "news_event"
    assert ADVERSE_NEWS_GATE_CONTRACT.activation.maximum_source_age_days == 90
    assert ADVERSE_NEWS_GATE_CONTRACT.activation.requires_model_transform is True
    assert NEWS_COVERAGE_GATE_CONTRACT.observation_kind.value == "news_coverage"
    assert NEWS_COVERAGE_GATE_CONTRACT.activation.requires_model_transform is False
    assert NEWS_COVERAGE_GATE_CONTRACT.activation.maximum_source_age_days == 3
    assert len(optional_gate_source_registry_sha256()) == 64


def test_adverse_news_contract_matches_spec_required_values() -> None:
    fields = {
        item.field_name: item.value_type.value
        for item in ADVERSE_NEWS_GATE_CONTRACT.required_values
    }
    assert len(fields) == 23
    assert fields["negative_severity_probability"] == "decimal"
    assert fields["body_truncated"] == "boolean"
    assert fields["classified_at"] == "timestamp"
    assert fields["original_published_at"] == "timestamp"
    assert ADVERSE_NEWS_GATE_CONTRACT.event_value_field == "original_published_at"
    assert ADVERSE_NEWS_GATE_CONTRACT.activation.expected_holding_sessions == (40, 63)


def test_coverage_contract_required_values() -> None:
    fields = {
        item.field_name: item.value_type.value
        for item in NEWS_COVERAGE_GATE_CONTRACT.required_values
    }
    assert fields == {
        "provider_query_identity": "text",
        "query_window_end": "timestamp",
        "query_window_start": "timestamp",
        "returned_article_count": "integer",
        "source_available_at": "timestamp",
    }
    assert NEWS_COVERAGE_GATE_CONTRACT.event_value_field == "query_window_end"


def test_gate_sleeve_is_not_a_factor_sleeve() -> None:
    with pytest.raises(OptionalFactorContractError):
        optional_factor_source_contract("adverse_news")
    assert optional_gate_source_contract("adverse_news") is ADVERSE_NEWS_GATE_CONTRACT
    assert optional_gate_source_contract("adverse_news_coverage") is NEWS_COVERAGE_GATE_CONTRACT
    with pytest.raises(OptionalFactorContractError):
        optional_gate_source_contract("news_sentiment")
