"""Normalize prospective EODHD and SEC records for immutable persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_common.hashing import canonical_json_hash
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence

from .sec_edgar import SecAcceptedFact

EODHD_PAPER_ENTITLEMENT = "eodhd-personal-use-paper-only"
SEC_PUBLIC_ENTITLEMENT = "public-sec-edgar"
_WRITER_VERSION = "vynmatrix-quality-compounder-evidence-v1"


class ProspectiveEquityEvidenceError(ValueError):
    """A prospective provider record cannot be normalized without guessing."""


def _invalid(message: str) -> NoReturn:
    raise ProspectiveEquityEvidenceError(message)


def build_eodhd_daily_bar_submissions(
    *,
    evidence: EODHDJsonEvidence,
    instrument_id: int,
    symbol: str,
    currency: str,
    session_closes: Mapping[date, datetime],
    entitlement_owner_user_id: str,
) -> tuple[EquityObservationSubmission, ...]:
    """Normalize one exact EOD response against required official sessions."""

    canonical_symbol = _symbol(symbol)
    canonical_currency = _upper(currency, field_name="currency")
    owner = _text(entitlement_owner_user_id, field_name="entitlement_owner_user_id")
    closes = dict(session_closes)
    if not closes:
        _invalid("daily-bar evidence requires at least one official session")
    for session_date, closes_at in closes.items():
        if not isinstance(session_date, date):
            _invalid("official session key must be a date")
        _utc(closes_at, field_name="official session close")
    rows = _object_rows(evidence.payload, field_name="EODHD daily bars")
    by_date: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        session_date = _date(row.get("date"), field_name="EODHD daily date")
        if session_date in by_date:
            _invalid("EODHD daily evidence contains a duplicate session")
        by_date[session_date] = row
    if set(by_date) != set(closes):
        _invalid("EODHD daily evidence does not exactly cover required official sessions")
    return tuple(
        _daily_submission(
            evidence=evidence,
            instrument_id=instrument_id,
            symbol=canonical_symbol,
            currency=canonical_currency,
            session_date=session_date,
            closes_at=closes[session_date],
            row=by_date[session_date],
            owner=owner,
        )
        for session_date in sorted(closes)
    )


def build_sec_fact_submissions(
    *,
    facts: Sequence[SecAcceptedFact],
    instrument_id: int,
) -> tuple[EquityObservationSubmission, ...]:
    """Normalize accession-linked SEC facts using first-local-retrieval availability."""

    submissions = tuple(
        _sec_fact_submission(fact=fact, instrument_id=instrument_id) for fact in facts
    )
    identities = tuple(item.source_record_identity for item in submissions)
    if len(identities) != len(set(identities)):
        _invalid("SEC accepted facts contain duplicate source record identities")
    return tuple(sorted(submissions, key=lambda item: item.source_record_identity))


def build_eodhd_corporate_action_submissions(
    *,
    evidence: EODHDJsonEvidence,
    instrument_id: int,
    symbol: str,
    action_type: str,
    start: date,
    end: date,
    entitlement_owner_user_id: str,
) -> tuple[EquityObservationSubmission, ...]:
    """Normalize a complete split or dividend response, including empty coverage."""

    canonical_symbol = _symbol(symbol)
    owner = _text(entitlement_owner_user_id, field_name="entitlement_owner_user_id")
    if action_type not in {"split", "dividend"}:
        _invalid("action_type must be split or dividend")
    if end < start:
        _invalid("corporate-action end cannot precede start")
    rows = _object_rows(evidence.payload, field_name="EODHD corporate actions")
    action_submissions: list[EquityObservationSubmission] = []
    seen_dates: set[date] = set()
    for row in rows:
        ex_date = _date(row.get("date"), field_name="EODHD corporate-action date")
        if not start <= ex_date <= end:
            _invalid("EODHD corporate action falls outside requested coverage")
        if ex_date in seen_dates:
            _invalid("EODHD corporate-action evidence contains a duplicate ex-date")
        seen_dates.add(ex_date)
        action_submissions.append(
            _corporate_action_submission(
                evidence=evidence,
                instrument_id=instrument_id,
                symbol=canonical_symbol,
                action_type=action_type,
                ex_date=ex_date,
                row=row,
                owner=owner,
            )
        )
    manifest_values = tuple(
        sorted(
            (
                EquityObservationValueInput(
                    field_name="action_count",
                    value_type="integer",
                    value=len(action_submissions),
                ),
                EquityObservationValueInput(
                    field_name="action_type",
                    value_type="text",
                    value=action_type,
                ),
                EquityObservationValueInput(
                    field_name="coverage_end",
                    value_type="date",
                    value=end,
                ),
                EquityObservationValueInput(
                    field_name="coverage_start",
                    value_type="date",
                    value=start,
                ),
            ),
            key=lambda item: (item.field_name, item.ordinal),
        )
    )
    manifest_identity = f"{canonical_symbol}.US:{action_type}:{start.isoformat()}:{end.isoformat()}"
    manifest_sha256 = canonical_json_hash(
        {
            "schema": "eodhd-corporate-action-coverage-v1",
            "source_record_identity": manifest_identity,
            "artifact_content_sha256": evidence.content_sha256,
            "values": [item.payload() for item in manifest_values],
        }
    )
    manifest = EquityObservationSubmission(
        **_eodhd_action_source(
            evidence=evidence,
            symbol=canonical_symbol,
            action_type=action_type,
            owner=owner,
        ),
        instrument_id=instrument_id,
        observation_kind="corporate_action",
        source_record_identity=manifest_identity,
        event_at=datetime.combine(end, time.min, tzinfo=UTC),
        available_at=evidence.retrieved_at,
        disposition="observed",
        normalized_content_sha256=manifest_sha256,
        values=manifest_values,
    )
    return (manifest, *sorted(action_submissions, key=lambda item: item.source_record_identity))


def persist_equity_evidence_batch(
    session: Session,
    submissions: Sequence[EquityObservationSubmission],
) -> tuple[EquityObservation, ...]:
    """Persist one caller-owned atomic batch in canonical record order."""

    canonical = tuple(
        sorted(
            submissions,
            key=lambda item: (
                item.provider,
                item.instrument_id or 0,
                item.observation_kind,
                item.source_record_identity,
            ),
        )
    )
    if not canonical:
        _invalid("equity evidence batch cannot be empty")
    return tuple(persist_equity_observation(session, item) for item in canonical)


def _daily_submission(
    *,
    evidence: EODHDJsonEvidence,
    instrument_id: int,
    symbol: str,
    currency: str,
    session_date: date,
    closes_at: datetime,
    row: Mapping[str, Any],
    owner: str,
) -> EquityObservationSubmission:
    prices = {
        name: _positive_decimal(row.get(name), field_name=f"EODHD daily {name}")
        for name in ("open", "high", "low", "close")
    }
    if prices["high"] < max(prices["open"], prices["low"], prices["close"]):
        _invalid("EODHD daily high is inconsistent")
    if prices["low"] > min(prices["open"], prices["high"], prices["close"]):
        _invalid("EODHD daily low is inconsistent")
    volume = _nonnegative_decimal(row.get("volume"), field_name="EODHD daily volume")
    values = tuple(
        sorted(
            (
                *(
                    EquityObservationValueInput(
                        field_name=name,
                        value_type="decimal",
                        value=value,
                        unit=currency,
                    )
                    for name, value in prices.items()
                ),
                EquityObservationValueInput(
                    field_name="session_date",
                    value_type="date",
                    value=session_date,
                ),
                EquityObservationValueInput(
                    field_name="split_adjusted_volume",
                    value_type="decimal",
                    value=volume,
                    unit="provider_split_adjusted_shares",
                ),
            ),
            key=lambda item: (item.field_name, item.ordinal),
        )
    )
    normalized_sha256 = canonical_json_hash(
        {
            "schema": "eodhd-daily-price-v1",
            "symbol": symbol,
            "session_date": session_date.isoformat(),
            "values": [item.payload() for item in values],
        }
    )
    return EquityObservationSubmission(
        provider="eodhd",
        product="historical-eod",
        endpoint=evidence.endpoint,
        dataset_version="prospective-eod-v1",
        tool_version=_WRITER_VERSION,
        source_identity=f"eodhd:eod:{symbol}.US",
        source_revision=evidence.content_sha256,
        retrieved_at=evidence.retrieved_at,
        timestamp_semantics={
            "event_at": "persisted official exchange session close",
            "available_at": "first successful local HTTP retrieval",
            "volume": "EODHD split-adjusted integer volume",
        },
        adjustment_policy="raw-ohlc-provider-split-adjusted-volume",
        entitlement_scope=EODHD_PAPER_ENTITLEMENT,
        entitlement_owner_user_id=owner,
        missing_data_policy="exact-required-session-coverage-fail-closed",
        artifact_content_sha256=evidence.content_sha256,
        instrument_id=instrument_id,
        observation_kind="price",
        source_record_identity=f"{symbol}.US:{session_date.isoformat()}",
        event_at=closes_at,
        available_at=evidence.retrieved_at,
        disposition="observed",
        normalized_content_sha256=normalized_sha256,
        values=values,
    )


def _sec_fact_submission(
    *,
    fact: SecAcceptedFact,
    instrument_id: int,
) -> EquityObservationSubmission:
    raw = fact.fact
    sources = [raw.source.retrieved_at, fact.filing_source.retrieved_at]
    if fact.historical_sic_source is not None:
        sources.append(fact.historical_sic_source.retrieved_at)
    available_at = max(sources)
    values = [
        EquityObservationValueInput(
            field_name="acceptance_time_raw",
            value_type="text",
            value=fact.acceptance_time_raw,
        ),
        EquityObservationValueInput(field_name="cik", value_type="text", value=raw.cik),
        EquityObservationValueInput(
            field_name="end",
            value_type="date",
            value=raw.end,
        ),
        EquityObservationValueInput(
            field_name="filed",
            value_type="date",
            value=raw.filed,
        ),
        EquityObservationValueInput(field_name="form", value_type="text", value=raw.form),
        EquityObservationValueInput(field_name="tag", value_type="text", value=raw.tag),
        EquityObservationValueInput(
            field_name="taxonomy",
            value_type="text",
            value=raw.taxonomy,
        ),
        EquityObservationValueInput(field_name="unit", value_type="text", value=raw.unit),
        EquityObservationValueInput(
            field_name="value",
            value_type=_sec_value_type(raw.value),
            value=raw.value,
            unit=raw.unit,
            context_identity=raw.frame,
            fiscal_year=raw.fiscal_year,
            fiscal_period=raw.fiscal_period,
            period_start=raw.start,
            period_end=raw.end,
        ),
    ]
    if raw.start is not None:
        values.append(
            EquityObservationValueInput(
                field_name="start",
                value_type="date",
                value=raw.start,
            )
        )
    if fact.historical_sic is not None:
        values.append(
            EquityObservationValueInput(
                field_name="historical_sic",
                value_type="integer",
                value=fact.historical_sic,
            )
        )
    canonical_values = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    identity = ":".join(
        (
            raw.cik,
            raw.accession,
            raw.taxonomy,
            raw.tag,
            raw.unit,
            raw.start.isoformat() if raw.start else "instant",
            raw.end.isoformat(),
            raw.frame or "no-frame",
        )
    )
    normalized_sha256 = canonical_json_hash(
        {
            "schema": "sec-accepted-xbrl-fact-v1",
            "source_record_identity": identity,
            "values": [item.payload() for item in canonical_values],
        }
    )
    return EquityObservationSubmission(
        provider="sec",
        product="edgar-companyfacts",
        endpoint=raw.source.endpoint,
        dataset_version="prospective-companyfacts-v1",
        tool_version=_WRITER_VERSION,
        source_identity=f"sec:CIK{raw.cik}:companyfacts",
        source_revision=raw.source.content_sha256,
        retrieved_at=raw.source.retrieved_at,
        timestamp_semantics={
            "event_at": "SEC submissions acceptance time reconciled to filing header",
            "available_at": "latest first-local retrieval across fact, filing, and SIC sources",
            "filing_source_sha256": fact.filing_source.content_sha256,
            "historical_sic_source_sha256": (
                fact.historical_sic_source.content_sha256
                if fact.historical_sic_source is not None
                else None
            ),
        },
        adjustment_policy="not-applicable",
        entitlement_scope=SEC_PUBLIC_ENTITLEMENT,
        entitlement_owner_user_id=None,
        missing_data_policy="unmatched-accession-or-sic-fails-closed",
        artifact_content_sha256=raw.source.content_sha256,
        instrument_id=instrument_id,
        observation_kind="xbrl_fact",
        source_record_identity=identity,
        event_at=fact.acceptance_time,
        available_at=available_at,
        disposition="observed",
        normalized_content_sha256=normalized_sha256,
        values=canonical_values,
        accession_number=raw.accession,
        filing_form=raw.form,
        sic_code=str(fact.historical_sic) if fact.historical_sic is not None else None,
    )


def _corporate_action_submission(
    *,
    evidence: EODHDJsonEvidence,
    instrument_id: int,
    symbol: str,
    action_type: str,
    ex_date: date,
    row: Mapping[str, Any],
    owner: str,
) -> EquityObservationSubmission:
    values = [
        EquityObservationValueInput(
            field_name="action_type",
            value_type="text",
            value=action_type,
        ),
        EquityObservationValueInput(
            field_name="ex_date",
            value_type="date",
            value=ex_date,
        ),
    ]
    if action_type == "split":
        ratio_text = _text(row.get("split"), field_name="EODHD split ratio")
        numerator, separator, denominator = ratio_text.partition("/")
        if separator != "/":
            _invalid("EODHD split ratio must use numerator/denominator form")
        ratio = _positive_decimal(numerator, field_name="EODHD split numerator") / (
            _positive_decimal(denominator, field_name="EODHD split denominator")
        )
        values.append(
            EquityObservationValueInput(
                field_name="ratio",
                value_type="decimal",
                value=ratio,
                unit="new_shares_per_old_share",
            )
        )
    else:
        amount = _positive_decimal(row.get("value"), field_name="EODHD dividend amount")
        currency = _upper(row.get("currency"), field_name="EODHD dividend currency")
        values.append(
            EquityObservationValueInput(
                field_name="amount",
                value_type="decimal",
                value=amount,
                unit=currency,
            )
        )
    canonical_values = tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))
    identity = f"{symbol}.US:{action_type}:{ex_date.isoformat()}"
    normalized_sha256 = canonical_json_hash(
        {
            "schema": "eodhd-corporate-action-v1",
            "source_record_identity": identity,
            "values": [item.payload() for item in canonical_values],
        }
    )
    return EquityObservationSubmission(
        **_eodhd_action_source(
            evidence=evidence,
            symbol=symbol,
            action_type=action_type,
            owner=owner,
        ),
        instrument_id=instrument_id,
        observation_kind="corporate_action",
        source_record_identity=identity,
        event_at=datetime.combine(ex_date, time.min, tzinfo=UTC),
        available_at=evidence.retrieved_at,
        disposition="observed",
        normalized_content_sha256=normalized_sha256,
        values=canonical_values,
    )


def _eodhd_action_source(
    *,
    evidence: EODHDJsonEvidence,
    symbol: str,
    action_type: str,
    owner: str,
) -> dict[str, Any]:
    return {
        "provider": "eodhd",
        "product": "splits" if action_type == "split" else "dividends",
        "endpoint": evidence.endpoint,
        "dataset_version": "prospective-corporate-actions-v1",
        "tool_version": _WRITER_VERSION,
        "source_identity": f"eodhd:{action_type}:{symbol}.US",
        "source_revision": evidence.content_sha256,
        "retrieved_at": evidence.retrieved_at,
        "timestamp_semantics": {
            "event_at": "provider ex-date at 00:00 UTC",
            "available_at": "first successful local HTTP retrieval",
        },
        "adjustment_policy": "raw-provider-corporate-action",
        "entitlement_scope": EODHD_PAPER_ENTITLEMENT,
        "entitlement_owner_user_id": owner,
        "missing_data_policy": "complete-request-window-including-empty-fail-closed",
        "artifact_content_sha256": evidence.content_sha256,
    }


def _sec_value_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, str):
        return "text"
    _invalid("SEC fact value has an unsupported type")


def _object_rows(value: object, *, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        _invalid(f"{field_name} must be an array of objects")
    return tuple(value)


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    converted = _decimal(value, field_name=field_name)
    if converted <= 0:
        _invalid(f"{field_name} must be positive")
    return converted


def _nonnegative_decimal(value: object, *, field_name: str) -> Decimal:
    converted = _decimal(value, field_name=field_name)
    if converted < 0:
        _invalid(f"{field_name} cannot be negative")
    return converted


def _decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        _invalid(f"{field_name} must be numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        message = f"{field_name} must be numeric"
        raise ProspectiveEquityEvidenceError(message) from exc
    if not converted.is_finite():
        _invalid(f"{field_name} must be finite")
    return converted


def _date(value: object, *, field_name: str) -> date:
    if not isinstance(value, str):
        _invalid(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        message = f"{field_name} must be an ISO date"
        raise ProspectiveEquityEvidenceError(message) from exc


def _symbol(value: object) -> str:
    symbol = _upper(value, field_name="symbol")
    if "." in symbol or any(character.isspace() for character in symbol):
        _invalid("symbol must be a canonical bare US ticker")
    return symbol


def _upper(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if text != text.upper():
        _invalid(f"{field_name} must be uppercase")
    return text


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be non-blank canonical text")
    return value


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "EODHD_PAPER_ENTITLEMENT",
    "SEC_PUBLIC_ENTITLEMENT",
    "ProspectiveEquityEvidenceError",
    "build_eodhd_corporate_action_submissions",
    "build_eodhd_daily_bar_submissions",
    "build_sec_fact_submissions",
    "persist_equity_evidence_batch",
]
