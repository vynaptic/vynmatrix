"""Tests for prospective EODHD and SEC evidence normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from market_data_ingestor.equity_evidence import (
    ProspectiveEquityEvidenceError,
    build_eodhd_corporate_action_submissions,
    build_eodhd_daily_bar_submissions,
    build_sec_fact_submissions,
)
from market_data_ingestor.sec_edgar import (
    SecAcceptedFact,
    SecSourceDocument,
    SecXbrlFact,
)

_RETRIEVED = datetime(2026, 8, 14, 1, 0, tzinfo=UTC)


def _eodhd_evidence() -> EODHDJsonEvidence:
    payload = [
        {
            "date": "2026-08-12",
            "open": 99,
            "high": 102,
            "low": 98,
            "close": 101,
            "adjusted_close": 100.5,
            "volume": 1_000_000,
        },
        {
            "date": "2026-08-13",
            "open": 101,
            "high": 104,
            "low": 100,
            "close": 103,
            "adjusted_close": 102.5,
            "volume": 1_100_000,
        },
    ]
    content = json.dumps(payload).encode()
    return EODHDJsonEvidence(
        endpoint=("/api/eod/AAA.US?from=2026-08-12&to=2026-08-13&period=d&fmt=json"),
        retrieved_at=_RETRIEVED,
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_daily_normalizer_requires_exact_official_sessions_and_ignores_adjusted_close() -> None:
    closes = {
        date(2026, 8, 12): datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
        date(2026, 8, 13): datetime(2026, 8, 13, 20, 0, tzinfo=UTC),
    }
    submissions = build_eodhd_daily_bar_submissions(
        evidence=_eodhd_evidence(),
        instrument_id=1,
        symbol="AAA",
        currency="USD",
        session_closes=closes,
        entitlement_owner_user_id="owner-1",
    )

    assert len(submissions) == 2
    assert submissions[-1].event_at == closes[date(2026, 8, 13)]
    assert {item.field_name for item in submissions[-1].values} == {
        "close",
        "high",
        "low",
        "open",
        "session_date",
        "split_adjusted_volume",
    }

    with pytest.raises(ProspectiveEquityEvidenceError, match="exactly cover"):
        build_eodhd_daily_bar_submissions(
            evidence=_eodhd_evidence(),
            instrument_id=1,
            symbol="AAA",
            currency="USD",
            session_closes={date(2026, 8, 13): closes[date(2026, 8, 13)]},
            entitlement_owner_user_id="owner-1",
        )


def test_sec_normalizer_uses_first_local_retrieval_not_historical_acceptance() -> None:
    company_source = SecSourceDocument(
        endpoint="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        retrieved_at=_RETRIEVED,
        content_sha256="1" * 64,
    )
    filing_source = SecSourceDocument(
        endpoint="https://data.sec.gov/submissions/CIK0000000001.json",
        retrieved_at=_RETRIEVED - timedelta(minutes=2),
        content_sha256="2" * 64,
    )
    sic_source = SecSourceDocument(
        endpoint=(
            "https://www.sec.gov/Archives/edgar/data/1/"
            "000000000126000001/0000000001-26-000001-index-headers.html"
        ),
        retrieved_at=_RETRIEVED - timedelta(minutes=1),
        content_sha256="3" * 64,
    )
    accepted = datetime(2026, 2, 3, 22, 0, tzinfo=UTC)
    fact = SecAcceptedFact(
        fact=SecXbrlFact(
            cik="0000000001",
            taxonomy="us-gaap",
            tag="Assets",
            label="Assets",
            description=None,
            unit="USD",
            value=Decimal("1000000"),
            start=None,
            end=date(2025, 12, 31),
            accession="0000000001-26-000001",
            fiscal_year=2025,
            fiscal_period="FY",
            form="10-K",
            filed=date(2026, 2, 3),
            frame="CY2025Q4I",
            source=company_source,
        ),
        acceptance_time_raw="2026-02-03T17:00:00Z",
        acceptance_time=accepted,
        historical_sic=3571,
        filing_source=filing_source,
        historical_sic_source=sic_source,
    )

    submission = build_sec_fact_submissions(facts=(fact,), instrument_id=1)[0]

    assert submission.event_at == accepted
    assert submission.available_at == _RETRIEVED
    assert submission.accession_number == "0000000001-26-000001"
    assert submission.sic_code == "3571"


def test_corporate_action_normalizer_persists_empty_coverage_manifest() -> None:
    content = b"[]"
    evidence = EODHDJsonEvidence(
        endpoint="/api/splits/AAA.US?from=2026-01-01&to=2026-08-13&fmt=json",
        retrieved_at=_RETRIEVED,
        payload=[],
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )

    submissions = build_eodhd_corporate_action_submissions(
        evidence=evidence,
        instrument_id=1,
        symbol="AAA",
        action_type="split",
        start=date(2026, 1, 1),
        end=date(2026, 8, 13),
        entitlement_owner_user_id="owner-1",
    )

    assert len(submissions) == 1
    assert submissions[0].source_record_identity.endswith("2026-01-01:2026-08-13")
    assert (
        next(item.value for item in submissions[0].values if item.field_name == "action_count") == 0
    )
