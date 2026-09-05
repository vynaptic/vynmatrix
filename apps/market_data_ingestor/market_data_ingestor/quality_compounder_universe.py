"""Prospective S&P 500 membership and identity evidence for paper trading."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquitySecurityIdentity,
    EquitySourceLineage,
    IndexMembership,
    Instrument,
    MarketCalendar,
)
from lib_application.services.equity_lineage import equity_observation_semantic_sha256
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_application.services.instrument_resolution import (
    resolve_broker_instrument_identity,
    resolve_instrument,
)
from lib_common.hashing import canonical_json_hash
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from lib_strategy.panels import EffectivePanelMember

from .equity_evidence import EODHD_PAPER_ENTITLEMENT

_MIN_MEMBER_COUNT = 500
_MAX_MEMBER_COUNT = 510
_MAX_CIK_DIGITS = 10
_US_EQUITY_EXCHANGES = frozenset({"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA"})
_EXCHANGE_ALIASES = {
    "NASDAQ GLOBAL MARKET": "NASDAQ",
    "NASDAQ GLOBAL SELECT MARKET": "NASDAQ",
    "NASDAQ STOCK MARKET": "NASDAQ",
    "NEW YORK STOCK EXCHANGE": "NYSE",
    "NYSE MKT": "NYSE AMERICAN",
}
_NAME_TOKEN_ALIASES = {
    "cl": "class",
    "company": "co",
    "corporation": "corp",
    "incorporated": "inc",
}
_US_COUNTRY_NAMES = frozenset({"united states", "united states of america", "usa"})
_BENCHMARK_SYMBOL = "SPY"
_WRITER_VERSION = "vynmatrix-quality-compounder-universe-v1"


class QualityCompounderUniverseError(RuntimeError):
    """A prospective universe checkpoint cannot be established exactly."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderUniverseError(message)


@dataclass(frozen=True, slots=True)
class QualityCompounderUniverseComponent:
    """One member in the exact current and historical checkpoint intersection."""

    symbol: str
    name: str
    exchange: str
    sector: str
    industry: str
    weight: Decimal


@dataclass(frozen=True, slots=True)
class QualityCompounderIdentityEvidence:
    """The two exact EODHD artifacts required to resolve one share class."""

    mapping: EODHDJsonEvidence
    general: EODHDJsonEvidence


@dataclass(frozen=True, slots=True)
class QualityCompounderSecurityIdentity:
    """Current permanent share-class and issuer identity."""

    symbol: str
    security_id: str
    issuer_id: str
    name: str
    exchange: str
    sector: str
    industry: str
    listing_date: date | None
    source: QualityCompounderIdentityEvidence


@dataclass(frozen=True, slots=True)
class PersistedQualityCompounderUniverse:
    """Exact members plus source material needed to construct the generic panel."""

    members: tuple[EffectivePanelMember, ...]
    membership_observation_sha256_by_instrument: Mapping[int, str]
    identity_observation_sha256_by_instrument: Mapping[int, str]


def require_quality_compounder_catalogue(
    session: Session,
    components: Sequence[QualityCompounderUniverseComponent],
) -> Mapping[str, int]:
    """Preflight full catalogue/conid coverage before per-security acquisition."""

    exchanges = {item.symbol: item.exchange for item in components}
    if not exchanges or len(exchanges) != len(tuple(components)):
        _invalid("catalogue preflight requires unique non-empty components")
    instruments = _qualified_catalogue_by_exchange(session, exchanges)
    return {symbol: int(instrument.instr_id) for symbol, instrument in instruments.items()}


def parse_quality_compounder_components(
    *,
    current: EODHDJsonEvidence,
    historical: EODHDJsonEvidence,
    ticker_history: EODHDJsonEvidence,
    decision_session: date,
) -> tuple[QualityCompounderUniverseComponent, ...]:
    """Require three EODHD membership views to describe one coherent current set."""

    current_rows = _mapping(current.payload, field_name="current Components")
    if "Components" in current_rows:
        current_rows = _mapping(current_rows["Components"], field_name="Components")
    parsed: dict[str, QualityCompounderUniverseComponent] = {}
    total_weight = Decimal(0)
    for key in sorted(current_rows):
        row = _mapping(current_rows[key], field_name=f"Components[{key}]")
        symbol = _symbol(row.get("Code"), field_name="component Code")
        if symbol in parsed:
            _invalid("current Components contains duplicate symbols")
        weight = _decimal(row.get("Weight"), field_name=f"{symbol} Weight")
        if not Decimal(0) <= weight <= Decimal(1):
            _invalid(f"current component weight is outside [0, 1] for {symbol}")
        parsed[symbol] = QualityCompounderUniverseComponent(
            symbol=symbol,
            name=_text(row.get("Name"), field_name=f"{symbol} Name"),
            exchange=_exchange(row.get("Exchange"), field_name=f"{symbol} Exchange"),
            sector=_text(row.get("Sector"), field_name=f"{symbol} Sector"),
            industry=_text(row.get("Industry"), field_name=f"{symbol} Industry"),
            weight=weight,
        )
        total_weight += weight
    if not _MIN_MEMBER_COUNT <= len(parsed) <= _MAX_MEMBER_COUNT:
        _invalid(f"current Components is incomplete: count={len(parsed)}")
    if not Decimal("0.95") <= total_weight <= Decimal("1.05"):
        _invalid(f"current component weights do not sum to one: {total_weight}")

    root = _mapping(historical.payload, field_name="historical Components")
    snapshots = _mapping(root.get("HistoricalComponents"), field_name="HistoricalComponents")
    eligible_dates = [
        _iso_date(raw_date, field_name="HistoricalComponents date")
        for raw_date in snapshots
        if _iso_date(raw_date, field_name="HistoricalComponents date") <= decision_session
    ]
    if not eligible_dates:
        _invalid("historical Components has no checkpoint at or before the decision session")
    checkpoint_date = max(eligible_dates)
    checkpoint = _mapping(
        snapshots[checkpoint_date.isoformat()],
        field_name=f"HistoricalComponents[{checkpoint_date.isoformat()}]",
    )
    historical_by_symbol: dict[str, tuple[str, str]] = {}
    for key in sorted(checkpoint):
        row = _mapping(checkpoint[key], field_name=f"historical component {key}")
        symbol = _symbol(row.get("Code"), field_name="historical component Code")
        if symbol in historical_by_symbol:
            _invalid("historical checkpoint contains duplicate symbols")
        if _iso_date(row.get("Date"), field_name=f"{symbol} Date") != checkpoint_date:
            _invalid("historical component date differs from its checkpoint")
        historical_by_symbol[symbol] = (
            _text(row.get("Name"), field_name=f"{symbol} historical Name"),
            _exchange(row.get("Exchange"), field_name=f"{symbol} historical Exchange"),
        )
    if set(historical_by_symbol) != set(parsed):
        _invalid("current Components differs from the latest historical full-state checkpoint")
    for symbol, component in parsed.items():
        historical_name, historical_exchange = historical_by_symbol[symbol]
        if not _names_match(component.name, historical_name):
            _invalid(f"current and historical names differ for {symbol}")
        if component.exchange != historical_exchange:
            _invalid(f"current and historical exchanges differ for {symbol}")

    _validate_ticker_history(parsed, ticker_history=ticker_history)
    return tuple(parsed[symbol] for symbol in sorted(parsed))


def _validate_ticker_history(
    components: Mapping[str, QualityCompounderUniverseComponent],
    *,
    ticker_history: EODHDJsonEvidence,
) -> None:
    ticker_rows = _mapping(ticker_history.payload, field_name="HistoricalTickerComponents")
    active_names: dict[str, list[str]] = {}
    for key in sorted(ticker_rows):
        row = _mapping(ticker_rows[key], field_name=f"ticker history {key}")
        symbol = _symbol(row.get("Code"), field_name="ticker history Code")
        active = row.get("IsActiveNow")
        delisted = row.get("IsDelisted")
        if active not in (0, 1, False, True) or delisted not in (0, 1, False, True):
            _invalid("ticker history status flags must be zero or one")
        if bool(active) and not bool(delisted):
            active_names.setdefault(symbol, []).append(
                _text(row.get("Name"), field_name=f"{symbol} ticker history Name")
            )
    missing_active = sorted(set(components) - set(active_names))
    if missing_active:
        _invalid("current Components contains securities not active in ticker history")
    for symbol, component in components.items():
        if not any(_names_match(component.name, name) for name in active_names[symbol]):
            _invalid(f"current and ticker-history names differ for {symbol}")


def parse_quality_compounder_security_identity(
    component: QualityCompounderUniverseComponent,
    *,
    evidence: QualityCompounderIdentityEvidence,
) -> QualityCompounderSecurityIdentity:
    """Require one exact ID mapping joined to one compatible General block."""

    return _parse_quality_compounder_identity(
        component,
        evidence=evidence,
        expected_type="common stock",
    )


def parse_quality_compounder_benchmark_identity(
    *,
    evidence: QualityCompounderIdentityEvidence,
) -> QualityCompounderSecurityIdentity:
    """Resolve the registered SPY ETF benchmark from mapping and General evidence."""

    general = _mapping(evidence.general.payload, field_name="SPY.US General")
    if _symbol(general.get("Code"), field_name="SPY.US General Code") != _BENCHMARK_SYMBOL:
        _invalid("SPY.US General returned another benchmark")
    component = QualityCompounderUniverseComponent(
        symbol=_BENCHMARK_SYMBOL,
        name=_text(general.get("Name"), field_name="SPY.US General Name"),
        exchange=_exchange(general.get("Exchange"), field_name="SPY.US General Exchange"),
        sector="Benchmark",
        industry="US Broad Market",
        weight=Decimal(1),
    )
    return _parse_quality_compounder_identity(
        component,
        evidence=evidence,
        expected_type="etf",
    )


def _parse_quality_compounder_identity(
    component: QualityCompounderUniverseComponent,
    *,
    evidence: QualityCompounderIdentityEvidence,
    expected_type: str,
) -> QualityCompounderSecurityIdentity:
    """Join one provider mapping to one exact security type."""

    qualified = f"{component.symbol}.US"
    mapping_root = _mapping(evidence.mapping.payload, field_name=f"{qualified} ID mapping")
    meta = _mapping(mapping_root.get("meta"), field_name=f"{qualified} ID mapping meta")
    links = _mapping(mapping_root.get("links"), field_name=f"{qualified} ID mapping links")
    rows = _sequence(mapping_root.get("data"), field_name=f"{qualified} ID mapping data")
    if links.get("next") is not None or meta.get("total") != len(rows) or len(rows) != 1:
        _invalid(f"{qualified} ID mapping must resolve exactly one unpaginated security")
    mapping = _mapping(rows[0], field_name=f"{qualified} ID mapping row")
    if _text(mapping.get("symbol"), field_name="mapped symbol").upper() != qualified:
        _invalid(f"{qualified} ID mapping returned another symbol")

    general = _mapping(evidence.general.payload, field_name=f"{qualified} General")
    if _symbol(general.get("Code"), field_name="General Code") != component.symbol:
        _invalid(f"{qualified} General returned another symbol")
    name = _text(general.get("Name"), field_name=f"{qualified} General Name")
    if not _names_match(component.name, name):
        _invalid(f"{qualified} component and General names differ")
    if _text(general.get("Type"), field_name=f"{qualified} Type").casefold() != expected_type:
        _invalid(f"{qualified} is not an exact {expected_type}")
    exchange = _exchange(general.get("Exchange"), field_name=f"{qualified} General Exchange")
    if exchange != component.exchange:
        _invalid(f"{qualified} component and General exchanges differ")
    if _text(general.get("CurrencyCode"), field_name=f"{qualified} CurrencyCode").upper() != "USD":
        _invalid(f"{qualified} is not quoted in USD")
    country_iso = _text(general.get("CountryISO"), field_name=f"{qualified} CountryISO").upper()
    country_name = _text(
        general.get("CountryName"), field_name=f"{qualified} CountryName"
    ).casefold()
    if country_iso != "US" or country_name not in _US_COUNTRY_NAMES:
        _invalid(f"{qualified} is not a US listing")
    if general.get("IsDelisted") is not False:
        _invalid(f"{qualified} is marked delisted")

    identifiers = {
        name: _optional_text(mapping.get(name), field_name=f"{qualified} {name}")
        for name in ("figi", "isin", "cusip")
    }
    permanent = next(
        ((name, value) for name, value in identifiers.items() if value is not None),
        None,
    )
    if permanent is None:
        _invalid(f"{qualified} lacks FIGI, ISIN, and CUSIP")
    mapping_cik = _cik(mapping.get("cik"), field_name=f"{qualified} mapping CIK")
    general_cik = _cik(general.get("CIK"), field_name=f"{qualified} General CIK")
    if mapping_cik is not None and general_cik is not None and mapping_cik != general_cik:
        _invalid(f"{qualified} mapping and General CIK differ")
    cik = mapping_cik or general_cik
    if cik is None:
        _invalid(f"{qualified} lacks a positive SEC CIK")
    general_isin = _optional_text(general.get("ISIN"), field_name=f"{qualified} General ISIN")
    if identifiers["isin"] is not None and general_isin is not None:
        if identifiers["isin"].upper() != general_isin.upper():
            _invalid(f"{qualified} mapping and General ISIN differ")
    elif mapping_cik is None or general_cik is None:
        _invalid(f"{qualified} mapping and General lack a shared permanent identifier")
    identity_type, identity_value = permanent
    assert identity_value is not None
    return QualityCompounderSecurityIdentity(
        symbol=component.symbol,
        security_id=f"{identity_type}:{identity_value.upper()}",
        issuer_id=f"cik:{cik}",
        name=name,
        exchange=exchange,
        sector=component.sector,
        industry=component.industry,
        listing_date=_optional_date(general.get("IPODate"), field_name=f"{qualified} IPODate"),
        source=evidence,
    )


def persist_quality_compounder_universe(
    session: Session,
    *,
    components: Sequence[QualityCompounderUniverseComponent],
    identities: Mapping[str, QualityCompounderSecurityIdentity],
    current_evidence: EODHDJsonEvidence,
    historical_evidence: EODHDJsonEvidence,
    ticker_history_evidence: EODHDJsonEvidence,
    decision_session: date,
    decision_close: datetime,
    cutoff: datetime,
    entitlement_owner_user_id: str,
) -> PersistedQualityCompounderUniverse:
    """Persist an exact-date checkpoint after all catalogue and source gates pass."""

    close = _utc(decision_close, field_name="decision_close")
    cutoff_at = _utc(cutoff, field_name="cutoff")
    if close.date() != decision_session or cutoff_at < close:
        _invalid("decision session, close, and cutoff are inconsistent")
    component_by_symbol = {item.symbol: item for item in components}
    if not component_by_symbol or len(component_by_symbol) != len(tuple(components)):
        _invalid("universe components must be non-empty and symbol-unique")
    if set(identities) != set(component_by_symbol):
        _invalid("every universe component requires one exact identity")
    source_evidence = [current_evidence, historical_evidence, ticker_history_evidence]
    source_evidence.extend(
        item
        for identity in identities.values()
        for item in (identity.source.mapping, identity.source.general)
    )
    if any(not close <= item.retrieved_at <= cutoff_at for item in source_evidence):
        _invalid("all current universe evidence must be retrieved after close and by cutoff")

    instruments = _qualified_catalogue(session, identities)
    instrument_ids = {int(item.instr_id) for item in instruments.values()}
    _require_replayable_checkpoint(
        session,
        decision_session=decision_session,
        instrument_ids=instrument_ids,
    )
    membership_graph_sha256 = canonical_json_hash(
        {
            "schema": "eodhd-sp500-membership-source-graph-v1",
            "current": current_evidence.content_sha256,
            "historical": historical_evidence.content_sha256,
            "ticker_history": ticker_history_evidence.content_sha256,
        }
    )
    membership_available_at = max(
        current_evidence.retrieved_at,
        historical_evidence.retrieved_at,
        ticker_history_evidence.retrieved_at,
    )
    members: list[EffectivePanelMember] = []
    membership_sha: dict[int, str] = {}
    identity_sha: dict[int, str] = {}
    for symbol in sorted(component_by_symbol):
        identity = identities[symbol]
        instrument_id = int(instruments[symbol].instr_id)
        if identity.symbol != symbol:
            _invalid(f"identity symbol differs from component for {symbol}")
        membership_values = _values(
            {
                "index_code": ("text", "SP500"),
                "member": ("boolean", True),
                "snapshot_date": ("date", decision_session),
                "source_graph_sha256": ("text", membership_graph_sha256),
                "symbol": ("text", symbol),
            }
        )
        membership_record = f"SP500:{decision_session.isoformat()}:{symbol}"
        membership_observation = persist_equity_observation(
            session,
            EquityObservationSubmission(
                provider="eodhd",
                product="sp500-current-and-historical-components",
                endpoint=(
                    f"{current_evidence.endpoint} + {historical_evidence.endpoint} + "
                    f"{ticker_history_evidence.endpoint}"
                ),
                dataset_version="prospective-checkpoint-v1",
                tool_version=_WRITER_VERSION,
                source_identity=(
                    "eodhd:GSPC.INDX:Components+HistoricalComponents+HistoricalTickerComponents"
                ),
                source_revision=membership_graph_sha256,
                retrieved_at=membership_available_at,
                timestamp_semantics={
                    "event_at": "completed decision-session close",
                    "available_at": "later of exact source retrieval timestamps",
                },
                adjustment_policy="none",
                entitlement_scope=EODHD_PAPER_ENTITLEMENT,
                entitlement_owner_user_id=entitlement_owner_user_id,
                missing_data_policy="three membership views must agree exactly",
                artifact_content_sha256=membership_graph_sha256,
                instrument_id=instrument_id,
                observation_kind="membership",
                source_record_identity=membership_record,
                event_at=close,
                available_at=membership_available_at,
                disposition="observed",
                normalized_content_sha256=_record_sha256(membership_record, membership_values),
                values=membership_values,
            ),
        )
        identity_graph_sha256 = canonical_json_hash(
            {
                "schema": "eodhd-security-identity-source-graph-v1",
                "mapping": identity.source.mapping.content_sha256,
                "general": identity.source.general.content_sha256,
            }
        )
        identity_values_map: dict[str, tuple[str, object]] = {
            "industry": ("text", identity.industry),
            "issuer_id": ("text", identity.issuer_id),
            "quote_currency": ("text", "USD"),
            "sector": ("text", identity.sector),
            "security_id": ("text", identity.security_id),
            "source_graph_sha256": ("text", identity_graph_sha256),
            "symbol": ("text", symbol),
            "tradable": ("boolean", True),
        }
        if identity.listing_date is not None:
            identity_values_map["listing_date"] = ("date", identity.listing_date)
        identity_values = _values(identity_values_map)
        identity_record = f"{symbol}.US:{decision_session.isoformat()}:security-identity"
        identity_available_at = max(
            identity.source.mapping.retrieved_at,
            identity.source.general.retrieved_at,
        )
        identity_observation = persist_equity_observation(
            session,
            EquityObservationSubmission(
                provider="eodhd",
                product="security-id-mapping-and-general",
                endpoint=f"{identity.source.mapping.endpoint} + {identity.source.general.endpoint}",
                dataset_version="prospective-checkpoint-v1",
                tool_version=_WRITER_VERSION,
                source_identity=f"eodhd:{symbol}.US:identity",
                source_revision=identity_graph_sha256,
                retrieved_at=identity_available_at,
                timestamp_semantics={
                    "event_at": "completed decision-session close",
                    "available_at": "later of exact source retrieval timestamps",
                },
                adjustment_policy="none",
                entitlement_scope=EODHD_PAPER_ENTITLEMENT,
                entitlement_owner_user_id=entitlement_owner_user_id,
                missing_data_policy="mapping and General must resolve one common stock exactly",
                artifact_content_sha256=identity_graph_sha256,
                instrument_id=instrument_id,
                observation_kind="security_identity",
                source_record_identity=identity_record,
                event_at=close,
                available_at=identity_available_at,
                disposition="observed",
                normalized_content_sha256=_record_sha256(identity_record, identity_values),
                values=identity_values,
            ),
        )
        _persist_checkpoint_rows(
            session,
            instrument_id=instrument_id,
            identity=identity,
            effective_on=decision_session,
            membership_observation_id=str(membership_observation.observation_id),
            membership_source_ref=membership_record,
            identity_observation_id=str(identity_observation.observation_id),
            identity_source_ref=identity_record,
        )
        membership_lineage = session.get(
            EquitySourceLineage,
            str(membership_observation.lineage_id),
        )
        identity_lineage = session.get(
            EquitySourceLineage,
            str(identity_observation.lineage_id),
        )
        if membership_lineage is None or identity_lineage is None:
            _invalid("persisted universe observation lacks immutable lineage")
        membership_sha[instrument_id] = equity_observation_semantic_sha256(
            membership_observation,
            membership_lineage,
        )
        identity_sha[instrument_id] = equity_observation_semantic_sha256(
            identity_observation,
            identity_lineage,
        )
        members.append(
            EffectivePanelMember(
                security_id=identity.security_id,
                issuer_id=identity.issuer_id,
                instrument_id=instrument_id,
                canonical_symbol=symbol,
            )
        )
    session.flush()
    return PersistedQualityCompounderUniverse(
        members=tuple(sorted(members, key=lambda item: item.security_id)),
        membership_observation_sha256_by_instrument=dict(sorted(membership_sha.items())),
        identity_observation_sha256_by_instrument=dict(sorted(identity_sha.items())),
    )


def persist_quality_compounder_benchmark_identity(
    session: Session,
    *,
    identity: QualityCompounderSecurityIdentity,
    decision_session: date,
    decision_close: datetime,
    cutoff: datetime,
    entitlement_owner_user_id: str,
) -> EffectivePanelMember:
    """Persist one exact-date SPY ETF identity on the shared XNYS calendar."""

    if identity.symbol != _BENCHMARK_SYMBOL:
        _invalid("quality-compounder benchmark must be SPY")
    close = _utc(decision_close, field_name="decision_close")
    cutoff_at = _utc(cutoff, field_name="cutoff")
    if close.date() != decision_session or cutoff_at < close:
        _invalid("benchmark decision session, close, and cutoff are inconsistent")
    if any(
        not close <= item.retrieved_at <= cutoff_at
        for item in (identity.source.mapping, identity.source.general)
    ):
        _invalid("benchmark evidence must be retrieved after close and by cutoff")
    instrument = resolve_instrument(
        session,
        _BENCHMARK_SYMBOL,
        asset_class="etf",
        allow_create=False,
    )
    if (
        instrument is None
        or not bool(instrument.is_tradable)
        or str(instrument.settlement_currency) != "USD"
        or str(instrument.market_session_policy) != "scheduled"
        or instrument.market_calendar_id is None
    ):
        _invalid("SPY lacks exact tradable ETF catalogue authority")
    calendar = session.get(MarketCalendar, int(instrument.market_calendar_id))
    if calendar is None or str(calendar.code) != "XNYS" or str(calendar.source_kind) != "exchange":
        _invalid("SPY is not bound to the shared official XNYS calendar")
    instrument_id = int(instrument.instr_id)
    graph_sha256 = canonical_json_hash(
        {
            "schema": "eodhd-security-identity-source-graph-v1",
            "mapping": identity.source.mapping.content_sha256,
            "general": identity.source.general.content_sha256,
        }
    )
    values = _values(
        {
            "industry": ("text", identity.industry),
            "issuer_id": ("text", identity.issuer_id),
            "quote_currency": ("text", "USD"),
            "sector": ("text", identity.sector),
            "security_id": ("text", identity.security_id),
            "source_graph_sha256": ("text", graph_sha256),
            "symbol": ("text", _BENCHMARK_SYMBOL),
            "tradable": ("boolean", True),
        }
    )
    source_record = f"SPY.US:{decision_session.isoformat()}:security-identity"
    available_at = max(
        identity.source.mapping.retrieved_at,
        identity.source.general.retrieved_at,
    )
    observation = persist_equity_observation(
        session,
        EquityObservationSubmission(
            provider="eodhd",
            product="security-id-mapping-and-general",
            endpoint=(f"{identity.source.mapping.endpoint} + {identity.source.general.endpoint}"),
            dataset_version="prospective-checkpoint-v1",
            tool_version=_WRITER_VERSION,
            source_identity="eodhd:SPY.US:identity",
            source_revision=graph_sha256,
            retrieved_at=available_at,
            timestamp_semantics={
                "event_at": "completed decision-session close",
                "available_at": "later of exact source retrieval timestamps",
            },
            adjustment_policy="none",
            entitlement_scope=EODHD_PAPER_ENTITLEMENT,
            entitlement_owner_user_id=entitlement_owner_user_id,
            missing_data_policy="mapping and General must resolve SPY ETF exactly",
            artifact_content_sha256=graph_sha256,
            instrument_id=instrument_id,
            observation_kind="security_identity",
            source_record_identity=source_record,
            event_at=close,
            available_at=available_at,
            disposition="observed",
            normalized_content_sha256=_record_sha256(source_record, values),
            values=values,
        ),
    )
    existing = session.scalar(
        select(EquitySecurityIdentity).where(
            EquitySecurityIdentity.instr_id == instrument_id,
            EquitySecurityIdentity.effective_from == decision_session,
        )
    )
    expected = {
        "security_id": identity.security_id,
        "issuer_id": identity.issuer_id,
        "canonical_symbol": _BENCHMARK_SYMBOL,
        "effective_to": decision_session,
        "source_ref": source_record,
        "observation_id": str(observation.observation_id),
    }
    if existing is None:
        session.add(
            EquitySecurityIdentity(
                instr_id=instrument_id,
                effective_from=decision_session,
                **expected,
            )
        )
    elif any(str(getattr(existing, key)) != str(value) for key, value in expected.items()):
        _invalid("same-date SPY identity replay differs from immutable evidence")
    session.flush()
    return EffectivePanelMember(
        security_id=identity.security_id,
        issuer_id=identity.issuer_id,
        instrument_id=instrument_id,
        canonical_symbol=_BENCHMARK_SYMBOL,
    )


def _qualified_catalogue(
    session: Session,
    identities: Mapping[str, QualityCompounderSecurityIdentity],
) -> dict[str, Instrument]:
    return _qualified_catalogue_by_exchange(
        session,
        {symbol: identity.exchange for symbol, identity in identities.items()},
    )


def _qualified_catalogue_by_exchange(
    session: Session,
    exchange_by_symbol: Mapping[str, str],
) -> dict[str, Instrument]:
    result: dict[str, Instrument] = {}
    for symbol in sorted(exchange_by_symbol):
        instrument = resolve_instrument(
            session,
            symbol,
            asset_class="equity",
            allow_create=False,
        )
        if instrument is None or str(instrument.canonical) != symbol:
            _invalid(f"{symbol} is absent from the exact equity catalogue")
        if (
            not bool(instrument.is_tradable)
            or str(instrument.settlement_currency) != "USD"
            or str(instrument.market_session_policy) != "scheduled"
            or instrument.market_calendar_id is None
            or _exchange(instrument.exchange, field_name=f"{symbol} catalogue exchange")
            != exchange_by_symbol[symbol]
        ):
            _invalid(f"{symbol} has incompatible catalogue authority")
        calendar = session.get(MarketCalendar, int(instrument.market_calendar_id))
        if (
            calendar is None
            or str(calendar.code) != "XNYS"
            or str(calendar.source_kind) != "exchange"
        ):
            _invalid(f"{symbol} is not bound to the shared official XNYS calendar")
        broker_identity = resolve_broker_instrument_identity(
            session,
            instrument_id=int(instrument.instr_id),
            broker_code="ibkr",
        )
        if (
            broker_identity is None
            or broker_identity.broker_instrument_id is None
            or not broker_identity.broker_instrument_id.isdecimal()
            or int(broker_identity.broker_instrument_id) <= 0
            or broker_identity.lot_size is None
            or broker_identity.lot_size != broker_identity.lot_size.to_integral_value()
            or (
                broker_identity.broker_instrument_type is not None
                and broker_identity.broker_instrument_type.upper() != "STK"
            )
        ):
            _invalid(f"{symbol} lacks an exact IBKR stock conid and whole-share lot size")
        result[symbol] = instrument
    return result


def _require_replayable_checkpoint(
    session: Session,
    *,
    decision_session: date,
    instrument_ids: set[int],
) -> None:
    membership_rows = list(
        session.scalars(
            select(IndexMembership).where(
                IndexMembership.index_code == "SP500",
                IndexMembership.effective_from <= decision_session,
                or_(
                    IndexMembership.effective_to.is_(None),
                    IndexMembership.effective_to >= decision_session,
                ),
            )
        )
    )
    if membership_rows and (
        {int(item.instr_id) for item in membership_rows} != instrument_ids
        or any(
            item.effective_from != decision_session or item.effective_to != decision_session
            for item in membership_rows
        )
    ):
        _invalid("existing S&P 500 membership overlaps the exact checkpoint")
    identity_rows = list(
        session.scalars(
            select(EquitySecurityIdentity).where(
                EquitySecurityIdentity.instr_id.in_(tuple(instrument_ids)),
                EquitySecurityIdentity.effective_from <= decision_session,
                or_(
                    EquitySecurityIdentity.effective_to.is_(None),
                    EquitySecurityIdentity.effective_to >= decision_session,
                ),
            )
        )
    )
    if identity_rows and (
        {int(item.instr_id) for item in identity_rows} != instrument_ids
        or any(
            item.effective_from != decision_session or item.effective_to != decision_session
            for item in identity_rows
        )
    ):
        _invalid("existing security identity intervals overlap the exact checkpoint")


def _persist_checkpoint_rows(
    session: Session,
    *,
    instrument_id: int,
    identity: QualityCompounderSecurityIdentity,
    effective_on: date,
    membership_observation_id: str,
    membership_source_ref: str,
    identity_observation_id: str,
    identity_source_ref: str,
) -> None:
    membership = session.scalar(
        select(IndexMembership).where(
            IndexMembership.index_code == "SP500",
            IndexMembership.instr_id == instrument_id,
            IndexMembership.effective_from == effective_on,
        )
    )
    membership_values = {
        "effective_to": effective_on,
        "source_ref": membership_source_ref,
        "observation_id": membership_observation_id,
    }
    if membership is None:
        session.add(
            IndexMembership(
                index_code="SP500",
                instr_id=instrument_id,
                effective_from=effective_on,
                **membership_values,
            )
        )
    elif any(
        str(getattr(membership, key)) != str(value) for key, value in membership_values.items()
    ):
        _invalid("same-date membership replay differs from immutable evidence")

    security_identity = session.scalar(
        select(EquitySecurityIdentity).where(
            EquitySecurityIdentity.instr_id == instrument_id,
            EquitySecurityIdentity.effective_from == effective_on,
        )
    )
    identity_values = {
        "security_id": identity.security_id,
        "issuer_id": identity.issuer_id,
        "canonical_symbol": identity.symbol,
        "effective_to": effective_on,
        "source_ref": identity_source_ref,
        "observation_id": identity_observation_id,
    }
    if security_identity is None:
        session.add(
            EquitySecurityIdentity(
                instr_id=instrument_id,
                effective_from=effective_on,
                **identity_values,
            )
        )
    elif any(
        str(getattr(security_identity, key)) != str(value) for key, value in identity_values.items()
    ):
        _invalid("same-date security identity replay differs from immutable evidence")


def _values(values: Mapping[str, tuple[str, object]]) -> tuple[EquityObservationValueInput, ...]:
    return tuple(
        EquityObservationValueInput(field_name=name, value_type=value_type, value=value)  # type: ignore[arg-type]
        for name, (value_type, value) in sorted(values.items())
    )


def _record_sha256(identity: str, values: Sequence[EquityObservationValueInput]) -> str:
    return canonical_json_hash(
        {
            "schema": "quality-compounder-universe-record-v1",
            "source_record_identity": identity,
            "values": [item.payload() for item in values],
        }
    )


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _invalid(f"{field_name} must be an object with string keys")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _invalid(f"{field_name} must be an array")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be canonical non-blank text")
    return value


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name=field_name)


def _symbol(value: object, *, field_name: str) -> str:
    symbol = _text(value, field_name=field_name).upper().removesuffix(".US")
    if any(character.isspace() for character in symbol):
        _invalid(f"{field_name} must not contain whitespace")
    return symbol


def _decimal(value: object, *, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        _invalid(f"{field_name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        message = f"{field_name} must be a finite decimal"
        raise QualityCompounderUniverseError(message) from exc
    if not result.is_finite():
        _invalid(f"{field_name} must be a finite decimal")
    return result


def _iso_date(value: object, *, field_name: str) -> date:
    try:
        return date.fromisoformat(_text(value, field_name=field_name))
    except ValueError as exc:
        message = f"{field_name} must be an ISO date"
        raise QualityCompounderUniverseError(message) from exc


def _optional_date(value: object, *, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    return _iso_date(value, field_name=field_name)


def _exchange(value: object, *, field_name: str) -> str:
    normalized = " ".join(_text(value, field_name=field_name).upper().split())
    normalized = _EXCHANGE_ALIASES.get(normalized, normalized)
    if normalized not in _US_EQUITY_EXCHANGES:
        _invalid(f"{field_name} is not a supported US equity exchange")
    return normalized


def _names_match(left: str, right: str) -> bool:
    def normalized(value: str) -> str:
        text = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
        result: list[str] = []
        for token in text.split():
            canonical = "".join(character for character in token if character.isalnum())
            if canonical:
                result.append(_NAME_TOKEN_ALIASES.get(canonical, canonical))
        return " ".join(result)

    return normalized(left) == normalized(right)


def _cik(value: object, *, field_name: str) -> str | None:
    raw = _optional_text(value, field_name=field_name)
    if raw is None:
        return None
    if not raw.isdecimal() or int(raw) <= 0 or len(raw) > _MAX_CIK_DIGITS:
        _invalid(f"{field_name} must be a positive SEC CIK")
    return f"{int(raw):010d}"


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "PersistedQualityCompounderUniverse",
    "QualityCompounderIdentityEvidence",
    "QualityCompounderSecurityIdentity",
    "QualityCompounderUniverseComponent",
    "QualityCompounderUniverseError",
    "parse_quality_compounder_benchmark_identity",
    "parse_quality_compounder_components",
    "parse_quality_compounder_security_identity",
    "persist_quality_compounder_benchmark_identity",
    "persist_quality_compounder_universe",
    "require_quality_compounder_catalogue",
]
