"""Prospective SEC evidence acquisition for the US Quality Compounder.

Network acquisition and database persistence are deliberately separate.  One
issuer graph is fetched per exact CIK, then its non-share facts are attached to
each qualified listed share class during the caller-owned persistence unit of
work.  Ambiguous issuer-level share counts remain issuer-scoped for multi-class
issuers so they cannot be mistaken for per-class market-cap evidence.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import NoReturn

from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, Instrument
from lib_application.services.equity_observation_writer import EquityObservationSubmission
from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.hashing import canonical_json_hash

from .equity_evidence import build_sec_fact_submissions, persist_equity_evidence_batch
from .quality_compounder_universe import QualityCompounderSecurityIdentity
from .sec_edgar import (
    SecAcceptedFact,
    SecCompanyFactsDataset,
    SecEdgarClient,
    SecFilingRecord,
    SecSubmissionDataset,
    apply_filing_header,
    attach_filing_lineage,
)

_ELIGIBLE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_ISSUER_LEVEL_SHARE_FACTS = frozenset(
    {
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    }
)
_DEFAULT_LOOKBACK_YEARS = 3
_MAX_LOOKBACK_YEARS = 3
_CIK_WIDTH = 10


class QualityCompounderSecError(RuntimeError):
    """Prospective SEC evidence cannot be acquired or persisted exactly."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderSecError(message)


@dataclass(frozen=True, slots=True)
class QualityCompounderIssuerSecGraph:
    """One acquired SEC source graph shared by every listed class of an issuer."""

    cik: str
    identities: tuple[QualityCompounderSecurityIdentity, ...]
    lookback_start: date
    cutoff: datetime
    filings: tuple[SecFilingRecord, ...]
    accepted_facts: tuple[SecAcceptedFact, ...]

    def __post_init__(self) -> None:
        cutoff = _utc(self.cutoff, field_name="cutoff")
        object.__setattr__(self, "cutoff", cutoff)
        if not self.cik.isdigit() or len(self.cik) != _CIK_WIDTH:
            _invalid("issuer graph CIK must be the exact ten-digit SEC identity")
        if not self.identities:
            _invalid("issuer graph must contain at least one listed share class")
        symbols = tuple(item.symbol for item in self.identities)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            _invalid("issuer graph share classes must be symbol-unique and canonical")
        if any(_identity_cik(item) != self.cik for item in self.identities):
            _invalid("issuer graph contains a share class from another exact CIK")
        accessions = {item.accession for item in self.filings}
        if not accessions or len(accessions) != len(self.filings):
            _invalid("issuer graph must contain unique eligible filings")
        if any(
            item.cik != self.cik
            or item.form not in _ELIGIBLE_FORMS
            or item.filing_date < self.lookback_start
            or item.filing_date > cutoff.date()
            or item.acceptance_time > cutoff
            or item.historical_sic is None
            or item.historical_sic_source is None
            for item in self.filings
        ):
            _invalid("issuer graph contains an ineligible or unreconciled filing")
        if not self.accepted_facts:
            _invalid("issuer graph has no eligible accession-linked Company Facts")
        if any(
            item.fact.cik != self.cik
            or item.fact.accession not in accessions
            or item.acceptance_time > cutoff
            or item.historical_sic is None
            or item.historical_sic_source is None
            for item in self.accepted_facts
        ):
            _invalid("issuer graph contains an ineligible or incomplete accepted fact")


def acquire_quality_compounder_sec_graphs(
    client: SecEdgarClient,
    *,
    identities: Sequence[QualityCompounderSecurityIdentity],
    cutoff: datetime,
    lookback_years: int = _DEFAULT_LOOKBACK_YEARS,
) -> tuple[QualityCompounderIssuerSecGraph, ...]:
    """Fetch one bounded, cutoff-safe SEC graph per exact issuer CIK.

    This boundary accepts no database session.  All submissions, Company Facts,
    and filing-header HTTP requests therefore complete before persistence starts.
    """

    cutoff_at = _utc(cutoff, field_name="cutoff")
    if (
        isinstance(lookback_years, bool)
        or not isinstance(lookback_years, int)
        or not 1 <= lookback_years <= _MAX_LOOKBACK_YEARS
    ):
        _invalid("SEC lookback_years must be between one and three")
    grouped = _group_identities(identities)
    lookback_start = _subtract_years(cutoff_at.date(), lookback_years)
    return tuple(
        _acquire_issuer_graph(
            client,
            cik=cik,
            identities=grouped[cik],
            lookback_start=lookback_start,
            cutoff=cutoff_at,
        )
        for cik in sorted(grouped)
    )


def persist_quality_compounder_sec_graphs(
    session: Session,
    graphs: Sequence[QualityCompounderIssuerSecGraph],
) -> tuple[EquityObservation, ...]:
    """Persist acquired graphs in one caller-owned transaction without HTTP."""

    canonical_graphs = tuple(sorted(graphs, key=lambda item: item.cik))
    if not canonical_graphs or len({item.cik for item in canonical_graphs}) != len(
        canonical_graphs
    ):
        _invalid("SEC persistence requires unique, non-empty issuer graphs")

    submissions: list[EquityObservationSubmission] = []
    seen_symbols: set[str] = set()
    for graph in canonical_graphs:
        instruments = _resolve_catalogue_instruments(session, graph.identities)
        overlapping = seen_symbols.intersection(instruments)
        if overlapping:
            _invalid(
                f"listed share classes appear in multiple issuer graphs: {sorted(overlapping)}"
            )
        seen_symbols.update(instruments)
        share_facts, issuer_facts = _partition_share_facts(graph.accepted_facts)
        multi_class = len(graph.identities) > 1
        for identity in graph.identities:
            facts = issuer_facts if multi_class else graph.accepted_facts
            built = build_sec_fact_submissions(
                facts=facts,
                instrument_id=int(instruments[identity.symbol].instr_id),
            )
            submissions.extend(_bind_to_share_class(built, identity=identity))
        if multi_class and share_facts:
            representative = int(instruments[graph.identities[0].symbol].instr_id)
            submissions.extend(
                replace(item, instrument_id=None)
                for item in build_sec_fact_submissions(
                    facts=share_facts,
                    instrument_id=representative,
                )
            )
    if not submissions:
        _invalid("SEC issuer graphs produced no persistable evidence")
    return persist_equity_evidence_batch(session, submissions)


def _acquire_issuer_graph(
    client: SecEdgarClient,
    *,
    cik: str,
    identities: tuple[QualityCompounderSecurityIdentity, ...],
    lookback_start: date,
    cutoff: datetime,
) -> QualityCompounderIssuerSecGraph:
    submissions = client.fetch_submissions(cik, since=lookback_start)
    if submissions.cik != cik:
        _invalid(f"SEC submissions returned another CIK for {cik}")
    eligible = tuple(
        sorted(
            (
                filing
                for filing in submissions.filings
                if filing.form in _ELIGIBLE_FORMS
                and filing.filing_date >= lookback_start
                and filing.filing_date <= cutoff.date()
                and filing.acceptance_time <= cutoff
            ),
            key=lambda item: (item.acceptance_time, item.accession),
        )
    )
    if not eligible:
        _invalid(f"CIK {cik} has no eligible filing in the bounded lookback")

    reconciled = tuple(
        apply_filing_header(filing, client.fetch_filing_header(filing)) for filing in eligible
    )
    filtered_submissions = SecSubmissionDataset(
        cik=submissions.cik,
        entity_name=submissions.entity_name,
        current_sic=submissions.current_sic,
        filings=reconciled,
        sources=submissions.sources,
    )
    company_facts = client.fetch_company_facts(cik)
    if company_facts.cik != cik:
        _invalid(f"SEC Company Facts returned another CIK for {cik}")
    accessions = {item.accession for item in reconciled}
    filtered_company_facts = SecCompanyFactsDataset(
        cik=company_facts.cik,
        entity_name=company_facts.entity_name,
        facts=tuple(item for item in company_facts.facts if item.accession in accessions),
        source=company_facts.source,
    )
    accepted = attach_filing_lineage(filtered_company_facts, filtered_submissions)
    return QualityCompounderIssuerSecGraph(
        cik=cik,
        identities=identities,
        lookback_start=lookback_start,
        cutoff=cutoff,
        filings=reconciled,
        accepted_facts=accepted,
    )


def _group_identities(
    identities: Sequence[QualityCompounderSecurityIdentity],
) -> Mapping[str, tuple[QualityCompounderSecurityIdentity, ...]]:
    if not identities:
        _invalid("SEC acquisition requires at least one qualified security identity")
    grouped: dict[str, list[QualityCompounderSecurityIdentity]] = defaultdict(list)
    symbols: set[str] = set()
    security_ids: set[str] = set()
    for identity in identities:
        if not isinstance(identity, QualityCompounderSecurityIdentity):
            _invalid("SEC acquisition received an incompatible security identity")
        if identity.symbol in symbols or identity.security_id in security_ids:
            _invalid("SEC acquisition identities must have unique symbols and security IDs")
        symbols.add(identity.symbol)
        security_ids.add(identity.security_id)
        grouped[_identity_cik(identity)].append(identity)
    return {
        cik: tuple(sorted(items, key=lambda item: item.symbol))
        for cik, items in sorted(grouped.items())
    }


def _resolve_catalogue_instruments(
    session: Session,
    identities: Sequence[QualityCompounderSecurityIdentity],
) -> dict[str, Instrument]:
    result: dict[str, Instrument] = {}
    instrument_ids: set[int] = set()
    for identity in identities:
        instrument = resolve_instrument(
            session,
            identity.symbol,
            asset_class="equity",
            allow_create=False,
        )
        if (
            instrument is None
            or str(instrument.canonical) != identity.symbol
            or str(instrument.asset_class) != "equity"
            or str(instrument.settlement_currency) != "USD"
            or str(instrument.exchange) != identity.exchange
            or not bool(instrument.is_tradable)
        ):
            _invalid(f"{identity.symbol} lacks exact qualified catalogue authority")
        instrument_id = int(instrument.instr_id)
        if instrument_id < 1 or instrument_id in instrument_ids:
            _invalid("listed share classes must resolve to distinct positive instrument IDs")
        instrument_ids.add(instrument_id)
        result[identity.symbol] = instrument
    return result


def _bind_to_share_class(
    submissions: Sequence[EquityObservationSubmission],
    *,
    identity: QualityCompounderSecurityIdentity,
) -> tuple[EquityObservationSubmission, ...]:
    bound: list[EquityObservationSubmission] = []
    for submission in submissions:
        issuer_identity = submission.source_record_identity
        scoped_identity = f"{issuer_identity}:security:{identity.security_id}"
        normalized_sha256 = canonical_json_hash(
            {
                "schema": "sec-listed-share-class-fact-v1",
                "issuer_source_record_identity": issuer_identity,
                "security_id": identity.security_id,
                "source_record_identity": scoped_identity,
                "values": [item.payload() for item in submission.values],
            }
        )
        bound.append(
            replace(
                submission,
                source_record_identity=scoped_identity,
                normalized_content_sha256=normalized_sha256,
            )
        )
    return tuple(bound)


def _partition_share_facts(
    facts: Sequence[SecAcceptedFact],
) -> tuple[tuple[SecAcceptedFact, ...], tuple[SecAcceptedFact, ...]]:
    shares: list[SecAcceptedFact] = []
    issuer: list[SecAcceptedFact] = []
    for fact in facts:
        target = (
            shares if (fact.fact.taxonomy, fact.fact.tag) in _ISSUER_LEVEL_SHARE_FACTS else issuer
        )
        target.append(fact)
    return tuple(shares), tuple(issuer)


def _identity_cik(identity: QualityCompounderSecurityIdentity) -> str:
    prefix, separator, cik = identity.issuer_id.partition(":")
    if prefix != "cik" or separator != ":" or not cik.isdigit() or len(cik) != _CIK_WIDTH:
        _invalid(f"{identity.symbol} lacks an exact ten-digit CIK issuer identity")
    return cik


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "QualityCompounderIssuerSecGraph",
    "QualityCompounderSecError",
    "acquire_quality_compounder_sec_graphs",
    "persist_quality_compounder_sec_graphs",
]
