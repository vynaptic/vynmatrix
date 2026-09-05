"""Tests for prospective US Quality Compounder SEC acquisition."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import Base, EquityObservation, Instrument
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from market_data_ingestor.quality_compounder_sec import (
    QualityCompounderSecError,
    acquire_quality_compounder_sec_graphs,
    persist_quality_compounder_sec_graphs,
)
from market_data_ingestor.quality_compounder_universe import (
    QualityCompounderIdentityEvidence,
    QualityCompounderSecurityIdentity,
)
from market_data_ingestor.sec_edgar import (
    SecCompanyFactsDataset,
    SecFilerIdentity,
    SecFilingHeader,
    SecFilingRecord,
    SecSourceDocument,
    SecSubmissionDataset,
    SecXbrlFact,
    parse_submissions_acceptance_time,
)

_CIK = "0001652044"
_CUTOFF = datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
_FILING_SOURCE = SecSourceDocument(
    endpoint=f"https://data.sec.gov/submissions/CIK{_CIK}.json",
    retrieved_at=datetime(2026, 6, 30, 20, 5, tzinfo=UTC),
    content_sha256="1" * 64,
)
_FACT_SOURCE = SecSourceDocument(
    endpoint=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_CIK}.json",
    retrieved_at=datetime(2026, 6, 30, 20, 10, tzinfo=UTC),
    content_sha256="2" * 64,
)


def _identity(symbol: str, security_id: str) -> QualityCompounderSecurityIdentity:
    content = b"{}"
    evidence = EODHDJsonEvidence(
        endpoint=f"/api/id-mapping?symbol={symbol}.US",
        retrieved_at=datetime(2026, 6, 30, 20, 0, tzinfo=UTC),
        payload={},
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return QualityCompounderSecurityIdentity(
        symbol=symbol,
        security_id=security_id,
        issuer_id=f"cik:{_CIK}",
        name="Alphabet Inc.",
        exchange="NASDAQ",
        sector="Communication Services",
        industry="Internet Content & Information",
        listing_date=date(2004, 8, 19),
        source=QualityCompounderIdentityEvidence(mapping=evidence, general=evidence),
    )


def _filing(
    accession: str,
    *,
    form: str,
    filed: date,
    accepted_raw: str,
) -> SecFilingRecord:
    return SecFilingRecord(
        cik=_CIK,
        accession=accession,
        acceptance_time_raw=accepted_raw,
        acceptance_time=parse_submissions_acceptance_time(accepted_raw),
        filing_date=filed,
        form=form,
        report_date=filed,
        primary_document="filing.htm",
        items=(),
        is_xbrl=True,
        is_inline_xbrl=True,
        source=_FILING_SOURCE,
    )


def _fact(filing: SecFilingRecord, *, tag: str, taxonomy: str = "us-gaap") -> SecXbrlFact:
    return SecXbrlFact(
        cik=_CIK,
        taxonomy=taxonomy,
        tag=tag,
        label=tag,
        description=None,
        unit="shares" if "SharesOutstanding" in tag else "USD",
        value=Decimal("1000000"),
        start=None,
        end=filing.report_date or filing.filing_date,
        accession=filing.accession,
        fiscal_year=filing.filing_date.year,
        fiscal_period="FY" if filing.form.startswith("10-K") else "Q2",
        form=filing.form,
        filed=filing.filing_date,
        frame=None,
        source=_FACT_SOURCE,
    )


class _FakeSecClient:
    def __init__(
        self,
        *,
        filings: tuple[SecFilingRecord, ...],
        facts: tuple[SecXbrlFact, ...],
    ) -> None:
        self.filings = filings
        self.facts = facts
        self.submission_calls: list[tuple[str, date | None]] = []
        self.fact_calls: list[str] = []
        self.header_calls: list[str] = []

    def fetch_submissions(self, cik: str, *, since: date | None = None) -> SecSubmissionDataset:
        self.submission_calls.append((cik, since))
        return SecSubmissionDataset(
            cik=cik,
            entity_name="Alphabet Inc.",
            current_sic=7370,
            filings=self.filings,
            sources=(_FILING_SOURCE,),
        )

    def fetch_company_facts(self, cik: str) -> SecCompanyFactsDataset:
        self.fact_calls.append(cik)
        return SecCompanyFactsDataset(
            cik=cik,
            entity_name="Alphabet Inc.",
            facts=self.facts,
            source=_FACT_SOURCE,
        )

    def fetch_filing_header(self, filing: SecFilingRecord) -> SecFilingHeader:
        self.header_calls.append(filing.accession)
        source = SecSourceDocument(
            endpoint=(
                "https://www.sec.gov/Archives/edgar/data/1652044/"
                f"{filing.accession.replace('-', '')}/"
                f"{filing.accession}-index-headers.html"
            ),
            retrieved_at=datetime(2026, 6, 30, 20, 8, tzinfo=UTC),
            content_sha256=hashlib.sha256(filing.accession.encode()).hexdigest(),
        )
        return SecFilingHeader(
            accession=filing.accession,
            acceptance_time=filing.acceptance_time,
            filing_date=filing.filing_date,
            form=filing.form,
            report_date=filing.report_date,
            filers=(SecFilerIdentity(cik=_CIK, company_name="Alphabet Inc.", sic=7370),),
            source=source,
        )


def _acquired_graphs():
    annual = _filing(
        "0001652044-26-000001",
        form="10-K",
        filed=date(2026, 2, 4),
        accepted_raw="2026-02-04T17:00:00Z",
    )
    quarterly_amendment = _filing(
        "0001652044-25-000002",
        form="10-Q/A",
        filed=date(2025, 8, 1),
        accepted_raw="2025-08-01T12:00:00Z",
    )
    wrong_form = _filing(
        "0001652044-26-000003",
        form="8-K",
        filed=date(2026, 5, 1),
        accepted_raw="2026-05-01T17:00:00Z",
    )
    too_old = _filing(
        "0001652044-23-000004",
        form="10-K",
        filed=date(2023, 6, 29),
        accepted_raw="2023-06-29T17:00:00Z",
    )
    after_cutoff = _filing(
        "0001652044-26-000005",
        form="10-Q",
        filed=date(2026, 6, 30),
        accepted_raw="2026-06-30T18:30:00Z",
    )
    client = _FakeSecClient(
        filings=(annual, quarterly_amendment, wrong_form, too_old, after_cutoff),
        facts=(
            _fact(annual, tag="Assets"),
            _fact(annual, taxonomy="dei", tag="EntityCommonStockSharesOutstanding"),
            _fact(quarterly_amendment, tag="NetIncomeLoss"),
            _fact(wrong_form, tag="Revenues"),
            _fact(after_cutoff, tag="StockholdersEquity"),
        ),
    )
    identities = (_identity("GOOG", "figi:GOOG"), _identity("GOOGL", "figi:GOOGL"))
    graphs = acquire_quality_compounder_sec_graphs(
        client,  # type: ignore[arg-type]
        identities=identities,
        cutoff=_CUTOFF,
    )
    return client, graphs


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_acquisition_fetches_one_cutoff_filtered_graph_for_shared_cik() -> None:
    client, graphs = _acquired_graphs()

    assert client.submission_calls == [(_CIK, date(2023, 6, 30))]
    assert client.fact_calls == [_CIK]
    assert set(client.header_calls) == {
        "0001652044-25-000002",
        "0001652044-26-000001",
    }
    assert len(graphs) == 1
    graph = graphs[0]
    assert tuple(item.symbol for item in graph.identities) == ("GOOG", "GOOGL")
    assert {item.accession for item in graph.filings} == set(client.header_calls)
    assert {item.fact.accession for item in graph.accepted_facts} == set(client.header_calls)
    assert {item.historical_sic for item in graph.accepted_facts} == {7370}


def test_acquisition_rejects_unbounded_lookback() -> None:
    client, _graphs = _acquired_graphs()
    with pytest.raises(QualityCompounderSecError, match="between one and three"):
        acquire_quality_compounder_sec_graphs(
            client,  # type: ignore[arg-type]
            identities=(_identity("GOOG", "figi:GOOG"),),
            cutoff=_CUTOFF,
            lookback_years=4,
        )


def test_persistence_fans_out_non_share_facts_and_keeps_multiclass_shares_issuer_scoped() -> None:
    _client, graphs = _acquired_graphs()
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            (
                Instrument(
                    instr_id=101,
                    asset_class="equity",
                    canonical="GOOG",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                    is_tradable=True,
                ),
                Instrument(
                    instr_id=102,
                    asset_class="equity",
                    canonical="GOOGL",
                    exchange="NASDAQ",
                    settlement_currency="USD",
                    is_tradable=True,
                ),
            )
        )
        session.flush()

        first = persist_quality_compounder_sec_graphs(session, graphs)
        replay = persist_quality_compounder_sec_graphs(session, graphs)

        assert tuple(item.observation_id for item in first) == tuple(
            item.observation_id for item in replay
        )
        rows = list(
            session.scalars(
                select(EquityObservation).where(EquityObservation.observation_kind == "xbrl_fact")
            )
        )
        assert len(rows) == 5
        instrument_rows = [item for item in rows if item.instr_id is not None]
        assert {item.instr_id for item in instrument_rows} == {101, 102}
        assert {item.accession_number for item in instrument_rows} == {
            "0001652044-25-000002",
            "0001652044-26-000001",
        }
        issuer_share_rows = [item for item in rows if item.instr_id is None]
        assert len(issuer_share_rows) == 1
        assert issuer_share_rows[0].accession_number == "0001652044-26-000001"


def test_persistence_requires_exact_canonical_catalogue_symbols() -> None:
    _client, graphs = _acquired_graphs()
    with Session(_engine()) as session:
        session.add(
            Instrument(
                instr_id=101,
                asset_class="equity",
                canonical="GOOG",
                exchange="NASDAQ",
                settlement_currency="USD",
                is_tradable=True,
            )
        )
        session.flush()
        with pytest.raises(QualityCompounderSecError, match="GOOGL"):
            persist_quality_compounder_sec_graphs(session, graphs)
