from __future__ import annotations

import pytest

from lib_strategy.equity_optional_factors import (
    ADVERSE_NEWS_GATE_CONTRACT,
    NEWS_COVERAGE_GATE_CONTRACT,
    OPTIONAL_FACTOR_SLEEVES,
    OPTIONAL_GATE_SOURCE_CONTRACTS,
    SEC_FILING_COVERAGE_GATE_CONTRACT,
    SEC_FILING_EVENT_GATE_CONTRACT,
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


def test_gate_registry_holds_exactly_four_contracts() -> None:
    assert [c.sleeve.value for c in OPTIONAL_GATE_SOURCE_CONTRACTS] == [
        "adverse_news",
        "adverse_news_coverage",
        "sec_filing_coverage",
        "sec_filing_events",
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


def test_sec_gate_contracts_registered() -> None:
    kinds = {c.observation_kind.value for c in OPTIONAL_GATE_SOURCE_CONTRACTS}
    assert {"filing", "filing_coverage"} <= kinds
    assert optional_gate_source_contract("sec_filing_events") is SEC_FILING_EVENT_GATE_CONTRACT
    assert optional_gate_source_contract("sec_filing_coverage") is SEC_FILING_COVERAGE_GATE_CONTRACT
    for contract in (SEC_FILING_EVENT_GATE_CONTRACT, SEC_FILING_COVERAGE_GATE_CONTRACT):
        assert not contract.activation.requires_model_transform
        assert contract.activation.maximum_source_age_days == 1
        assert contract.activation.signal_half_life_days == 1
        assert contract.availability_value_field == "source_available_at"
    for name in ("sec_filing_events", "sec_filing_coverage"):
        with pytest.raises(OptionalFactorContractError):
            optional_factor_source_contract(name)


def test_sec_event_contract_values() -> None:
    contract = SEC_FILING_EVENT_GATE_CONTRACT
    assert contract.event_at_semantic.value == "filing_acceptance"
    assert contract.event_value_field == "acceptance_time"
    assert contract.activation.aggregation.value == "sec_filing_event_rules"
    assert contract.activation.direction.value == "lower_is_better"
    assert {i.field_name: i.value_type.value for i in contract.required_values} == {
        "acceptance_time": "timestamp",
        "acceptance_time_raw": "text",
        "cik": "text",
        "filing_date": "date",
        "source_available_at": "timestamp",
    }


def test_sec_coverage_contract_values() -> None:
    contract = SEC_FILING_COVERAGE_GATE_CONTRACT
    assert contract.event_at_semantic.value == "measurement_as_of"
    assert contract.event_value_field == "window_end"
    assert contract.activation.aggregation.value == "coverage_attestation"
    assert contract.activation.direction.value == "higher_is_better"
    assert {i.field_name: i.value_type.value for i in contract.required_values} == {
        "has_older_page": "boolean",
        "listed_accession": "text",
        "source_available_at": "timestamp",
        "submission_page_sha256": "text",
        "window_end": "timestamp",
        "window_start": "timestamp",
    }
