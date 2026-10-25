"""Resolve the inert SEC filing event gate at a quarter-end panel cutoff.

The resolver reads persisted ``filing_coverage`` and ``filing`` observations,
admits each selected row through the optional-factor evidence validator, and
hands the admitted evidence to the pure ``sec_filing_gate_reason`` rule
function. It never writes. Nothing wires it into the quality-compounder panel
yet, so the released configuration identity is unchanged.

Rules (design spec sections 3 and 5):

* Coverage: among ``filing_coverage`` rows with ``available_at <= T``, the one
  whose ``window_end`` (the fetch cutoff) is latest within
  ``[T - 6 h, T]``. None means ``coverage=None`` for the rule function.
* Filings: only rows of that same fetch, identified by the fetch cutoff in
  their source identity, with ``event_at`` in
  ``(window_end - 730 d, window_end]``. Every other fetch is ignored.
* Admissibility: every selected row passes the validator at ``T`` and carries
  well-typed values the contracts do not enforce (``item`` text rows, one
  boolean ``has_older_page``, at most one ``oldest_listed_filing_date``, and a
  ``listed_accession_count`` equal to the ``listed_accession`` rows).

An evidence problem blocks only that instrument with
``sec_filing_evidence_invalid``; it never raises. Coverage that is selected but
invalid is reported as invalid evidence, not as unavailable coverage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, EquityObservationValue
from lib_application.db.session import bind_utc_instant
from lib_application.services.equity_lineage import EquityObservationAuthorityError
from lib_application.services.equity_optional_factor_sources import (
    OptionalFactorEvidenceValidationError,
    validate_optional_factor_evidence,
)
from lib_common.logging import get_logger
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.equity_optional_factors import (
    SEC_FILING_COVERAGE_GATE_CONTRACT,
    SEC_FILING_EVENT_GATE_CONTRACT,
)
from lib_strategy.sec_filing_rules import (
    SEC_FILING_COVERAGE_TOLERANCE,
    SEC_FILING_EVIDENCE_SCOPE,
    SEC_FILING_RULES_VERSION,
    SecFilingCoverage,
    SecFilingEvent,
    SecFilingGateReason,
    SecFilingGateResult,
    sec_filing_gate_reason,
)

logger = get_logger(__name__)

_FILING_KIND: Final = SEC_FILING_EVENT_GATE_CONTRACT.observation_kind.value
_COVERAGE_KIND: Final = SEC_FILING_COVERAGE_GATE_CONTRACT.observation_kind.value
_OBSERVED: Final = "observed"
_IDENTITY_SEPARATOR: Final = "|"
_IDENTITY_PARTS: Final = 3
_PRECEDENCE: Final = tuple(SecFilingGateReason)
_VALIDATION_ERRORS: Final = (
    OptionalFactorEvidenceValidationError,
    EquityObservationAuthorityError,
)


class _SecFilingEvidenceError(ValueError):
    """One stored SEC filing gate row is malformed; blocks only its instrument."""


@dataclass(slots=True)
class _InstrumentLedger:
    """Mutable per-instrument accumulator; frozen into a result at the end."""

    instrument_id: int
    evidence_valid: bool = True
    coverage_invalid: bool = False
    events: list[SecFilingEvent] = field(default_factory=list)

    def invalidate(self, observation_id: str | None, detail: str) -> None:
        self.evidence_valid = False
        logger.warning(
            "sec_filing_gate.evidence_invalid",
            instrument_id=self.instrument_id,
            observation_id=observation_id,
            detail=detail,
        )


def resolve_quality_compounder_sec_filing_gate(
    session: Session,
    *,
    cutoff: datetime,
    instrument_ids: Sequence[int],
    provider_authority_policy: ProviderAuthorityPolicy,
) -> Mapping[int, SecFilingGateResult]:
    """Return the SEC filing gate result for each instrument at cutoff ``T``."""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        message = "cutoff must be timezone-aware"
        raise ValueError(message)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in instrument_ids):
        message = "instrument_ids must be integers"
        raise ValueError(message)
    cutoff = cutoff.astimezone(UTC)
    results: dict[int, SecFilingGateResult] = {}
    for instrument_id in sorted(set(instrument_ids)):
        ledger = _InstrumentLedger(instrument_id)
        coverage_row = _selected_coverage(session, ledger=ledger, cutoff=cutoff)
        coverage: SecFilingCoverage | None = None
        if coverage_row is not None:
            coverage = _admitted_coverage(
                session,
                ledger=ledger,
                observation=coverage_row,
                cutoff=cutoff,
                provider_authority_policy=provider_authority_policy,
            )
            ledger.coverage_invalid = coverage is None
            _admit_filings(
                session,
                ledger=ledger,
                coverage_row=coverage_row,
                cutoff=cutoff,
                provider_authority_policy=provider_authority_policy,
            )
        result = sec_filing_gate_reason(events=ledger.events, coverage=coverage, cutoff=cutoff)
        results[instrument_id] = _with_evidence_state(result, ledger)
        logger.info(
            "sec_filing_gate.resolved",
            instrument_id=instrument_id,
            cutoff=cutoff.isoformat(),
            reason=results[instrument_id].reason,
            fired=[reason.value for reason in results[instrument_id].fired],
            accessions=list(results[instrument_id].accessions),
        )
    return results


def _selected_coverage(
    session: Session, *, ledger: _InstrumentLedger, cutoff: datetime
) -> EquityObservation | None:
    """Pick the coverage row available by ``T`` whose window ends latest in tolerance."""
    candidates = _latest_revisions(
        session.scalars(
            select(EquityObservation).where(
                EquityObservation.observation_kind == _COVERAGE_KIND,
                EquityObservation.instr_id == ledger.instrument_id,
                EquityObservation.disposition == _OBSERVED,
                EquityObservation.available_at <= bind_utc_instant(session, cutoff),
                EquityObservation.event_at
                >= bind_utc_instant(session, cutoff - SEC_FILING_COVERAGE_TOLERANCE),
                EquityObservation.event_at <= bind_utc_instant(session, cutoff),
            )
        )
    )
    if not candidates:
        return None
    latest_end = max(_utc(item.event_at) for item in candidates)
    latest = [item for item in candidates if _utc(item.event_at) == latest_end]
    if len(latest) > 1:
        # Two issuers attesting one instrument at one fetch is ambiguous authority.
        for item in latest:
            ledger.invalidate(str(item.observation_id), "several coverage rows share one fetch")
        ledger.coverage_invalid = True
    return latest[0]


def _admitted_coverage(
    session: Session,
    *,
    ledger: _InstrumentLedger,
    observation: EquityObservation,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> SecFilingCoverage | None:
    observation_id = str(observation.observation_id)
    try:
        validate_optional_factor_evidence(
            session,
            contract=SEC_FILING_COVERAGE_GATE_CONTRACT,
            factor_version=SEC_FILING_RULES_VERSION,
            observation_id=observation_id,
            cutoff=cutoff,
            provider_authority_policy=provider_authority_policy,
            expected_instrument_id=ledger.instrument_id,
        )
        coverage = _coverage_record(observation, _values(session, observation_id))
    except (*_VALIDATION_ERRORS, _SecFilingEvidenceError) as exc:
        ledger.invalidate(observation_id, str(exc))
        return None
    if ledger.coverage_invalid:
        return None
    return coverage


def _coverage_record(
    observation: EquityObservation, rows: Mapping[str, list[EquityObservationValue]]
) -> SecFilingCoverage:
    window_end = _single(rows, "window_end", "timestamp").timestamp_value
    if window_end is None or _utc(window_end) != _utc(observation.event_at):
        _reject("window_end must equal the coverage event time")
    if _fetch_cutoff(str(observation.source_record_identity)) != _utc(window_end).isoformat():
        _reject("coverage identity does not carry its window_end")
    older = _single(rows, "has_older_page", "boolean").boolean_value
    if not isinstance(older, bool):
        _reject("has_older_page must be one boolean")
    oldest_rows = rows.get("oldest_listed_filing_date", [])
    oldest: date | None = None
    if oldest_rows:
        oldest = _single(rows, "oldest_listed_filing_date", "date").date_value
        if not isinstance(oldest, date):
            _reject("oldest_listed_filing_date must be at most one date")
    listed = [_text(row, "listed_accession") for row in rows.get("listed_accession", [])]
    count = _single(rows, "listed_accession_count", "integer").integer_value
    if count != len(listed) or len(set(listed)) != len(listed):
        _reject("listed_accession_count does not match the listed accessions")
    return SecFilingCoverage(
        window_end=_utc(window_end),
        oldest_listed_filing_date=oldest,
        has_older_page=older,
        listed_accessions=frozenset(listed),
        observation_id=str(observation.observation_id),
    )


def _admit_filings(
    session: Session,
    *,
    ledger: _InstrumentLedger,
    coverage_row: EquityObservation,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> None:
    """Validate the filing rows of the selected fetch and append their events."""
    window_end = _utc(coverage_row.event_at)
    suffix = f"{_IDENTITY_SEPARATOR}{ledger.instrument_id}{_IDENTITY_SEPARATOR}"
    suffix += window_end.isoformat()
    candidates = _latest_revisions(
        session.scalars(
            select(EquityObservation).where(
                EquityObservation.observation_kind == _FILING_KIND,
                EquityObservation.instr_id == ledger.instrument_id,
                EquityObservation.disposition == _OBSERVED,
                EquityObservation.event_at
                > bind_utc_instant(session, window_end - SEC_FILING_EVIDENCE_SCOPE),
                EquityObservation.event_at <= bind_utc_instant(session, window_end),
            )
        )
    )
    seen: set[str] = set()
    for observation in candidates:
        if not str(observation.source_record_identity).endswith(suffix):
            continue
        observation_id = str(observation.observation_id)
        try:
            validate_optional_factor_evidence(
                session,
                contract=SEC_FILING_EVENT_GATE_CONTRACT,
                factor_version=SEC_FILING_RULES_VERSION,
                observation_id=observation_id,
                cutoff=cutoff,
                provider_authority_policy=provider_authority_policy,
                expected_instrument_id=ledger.instrument_id,
            )
            event = _filing_event(observation, _values(session, observation_id))
        except (*_VALIDATION_ERRORS, _SecFilingEvidenceError) as exc:
            ledger.invalidate(observation_id, str(exc))
            continue
        if event.accession in seen:
            ledger.invalidate(observation_id, f"accession {event.accession!r} is listed twice")
            continue
        seen.add(event.accession)
        ledger.events.append(event)


def _filing_event(
    observation: EquityObservation, rows: Mapping[str, list[EquityObservationValue]]
) -> SecFilingEvent:
    identity = str(observation.source_record_identity)
    accession = identity.split(_IDENTITY_SEPARATOR, 1)[0]
    if not accession or observation.accession_number != accession:
        _reject("filing identity does not carry its accession number")
    form = observation.filing_form
    if not isinstance(form, str) or not form:
        _reject("filing form is missing")
    accepted = _single(rows, "acceptance_time", "timestamp").timestamp_value
    if accepted is None or _utc(accepted) != _utc(observation.event_at):
        _reject("acceptance_time must equal the filing event time")
    item_rows = rows.get("item", [])
    if [int(row.ordinal) for row in item_rows] != list(range(len(item_rows))):
        _reject("item ordinals must be contiguous from zero")
    return SecFilingEvent(
        accession=accession,
        form=form,
        items=tuple(_text(row, "item") for row in item_rows),
        acceptance_time=_utc(accepted),
        observation_id=str(observation.observation_id),
    )


def _with_evidence_state(
    result: SecFilingGateResult, ledger: _InstrumentLedger
) -> SecFilingGateResult:
    """Add ``sec_filing_evidence_invalid`` for problems the resolver itself found.

    Invalid selected coverage is invalid evidence, so the ``coverage=None``
    unavailability it produced in the rule function is replaced, not kept.
    """
    if ledger.evidence_valid and not ledger.coverage_invalid:
        return result
    fired = set(result.fired) | {SecFilingGateReason.EVIDENCE_INVALID}
    if ledger.coverage_invalid:
        fired.discard(SecFilingGateReason.COVERAGE_UNAVAILABLE)
    ordered = tuple(reason for reason in _PRECEDENCE if reason in fired)
    return SecFilingGateResult(
        reason=ordered[0],
        fired=ordered,
        accessions=result.accessions,
        observation_ids=result.observation_ids,
    )


def _reject(message: str) -> NoReturn:
    raise _SecFilingEvidenceError(message)


def _fetch_cutoff(source_record_identity: str) -> str | None:
    parts = source_record_identity.split(_IDENTITY_SEPARATOR)
    return parts[-1] if len(parts) == _IDENTITY_PARTS else None


def _latest_revisions(observations: Iterable[EquityObservation]) -> list[EquityObservation]:
    """Keep only the highest revision of each source record."""
    latest: dict[str, EquityObservation] = {}
    for observation in observations:
        identity = str(observation.source_record_identity)
        current = latest.get(identity)
        if current is None or int(observation.revision) > int(current.revision):
            latest[identity] = observation
    return sorted(latest.values(), key=lambda item: str(item.observation_id))


def _values(session: Session, observation_id: str) -> dict[str, list[EquityObservationValue]]:
    by_field: dict[str, list[EquityObservationValue]] = {}
    rows = session.scalars(
        select(EquityObservationValue)
        .where(EquityObservationValue.observation_id == observation_id)
        .order_by(EquityObservationValue.field_name, EquityObservationValue.ordinal)
    )
    for row in rows:
        by_field.setdefault(str(row.field_name), []).append(row)
    return by_field


def _single(
    rows: Mapping[str, list[EquityObservationValue]], name: str, value_type: str
) -> EquityObservationValue:
    values = rows.get(name, [])
    if len(values) != 1 or str(values[0].value_type) != value_type:
        _reject(f"{name} must be exactly one {value_type} value")
    return values[0]


def _text(row: EquityObservationValue, name: str) -> str:
    if str(row.value_type) != "text" or not isinstance(row.text_value, str):
        _reject(f"{name} rows must be text")
    return str(row.text_value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["resolve_quality_compounder_sec_filing_gate"]
