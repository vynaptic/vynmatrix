"""Resolve the inert adverse-news exclusion gate at a quarter-end cutoff.

The resolver reads persisted ``news_coverage`` and ``news_event`` observations,
admits them through the optional-factor evidence validator, recomputes every
article verdict from the stored probabilities, and returns one gate result per
instrument. It never calls TypeSafe and never writes. Nothing wires it into the
quality-compounder panel yet, so the released configuration identity is
unchanged.

Rules (design spec sections 5.2, 7 and 8):

* Coverage: records whose window reaches ``T`` and that completed within
  ``[T + coverage_min_delay, T + deadline]`` must, without gaps, span
  ``(T - lookback, T]``. They are validated at ``T + deadline``; coverage is
  the one kind exempt from the ``available_at <= T`` rule.
* Selection: classifications with the policy's question-set and model version,
  ``source_available_at <= T`` and ``original_published_at > T - lookback``.
  Anything else is skipped, which is not an error.
* Admissibility: each selected classification passes the validator at ``T``,
  the pinned model was released on or before ``T``, and it was classified no
  later than ``T + deadline``.
* Completeness: every article a covering record returned is skipped or has a
  selected, validated classification under the policy versions.

A validation failure blocks only that instrument, with ``news_evidence_invalid``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation, EquityObservationValue
from lib_application.services.adverse_news_observations import NEWS_COVERAGE_FACTOR_VERSION
from lib_application.services.equity_lineage import EquityObservationAuthorityError
from lib_application.services.equity_optional_factor_sources import (
    OptionalFactorEvidenceValidationError,
    validate_optional_factor_evidence,
)
from lib_common.logging import get_logger
from lib_strategy.adverse_news_questions import (
    ADVERSE_NEWS_MODEL_RELEASED_ON,
    ADVERSE_NEWS_QUESTION_SET_VERSION,
    LOOKBACK_DAYS,
    UNSETTLED_VERDICTS,
    AdverseNewsAnswerError,
    AdverseNewsAnswers,
    ArticleVerdict,
    GateReason,
    classify_article,
    gate_reason,
)
from lib_strategy.data_authority import ProviderAuthorityPolicy
from lib_strategy.equity_optional_factors import (
    ADVERSE_NEWS_GATE_CONTRACT,
    NEWS_COVERAGE_GATE_CONTRACT,
)

logger = get_logger(__name__)

_NEWS_EVENT_KIND: Final = ADVERSE_NEWS_GATE_CONTRACT.observation_kind.value
_NEWS_COVERAGE_KIND: Final = NEWS_COVERAGE_GATE_CONTRACT.observation_kind.value
_OBSERVED: Final = "observed"
_IDENTITY_SEPARATOR: Final = "|"
_IDENTITY_PARTS: Final = 4
_PROBABILITY_FIELDS: Final = frozenset(
    {
        "about_company_probability",
        "negative_confirmed_probability",
        "negative_development_probability",
        "negative_severity_probability",
        "new_development_probability",
        "price_recap_probability",
    }
)
_SELECTION_FIELDS: Final = frozenset(
    {
        "classified_at",
        "model_version",
        "original_published_at",
        "question_set_version",
        "source_available_at",
        *_PROBABILITY_FIELDS,
    }
)
_COVERAGE_FIELDS: Final = frozenset(
    {
        "query_window_end",
        "query_window_start",
        "returned_article_count",
        "returned_article_identity",
        "skipped_article_identity",
    }
)
_VALIDATION_ERRORS: Final = (
    OptionalFactorEvidenceValidationError,
    EquityObservationAuthorityError,
    AdverseNewsAnswerError,
)


class AdverseNewsGateError(ValueError):
    """The gate was asked to resolve with invalid arguments."""


def _reject(message: str) -> NoReturn:
    raise AdverseNewsGateError(message)


@dataclass(frozen=True, slots=True)
class AdverseNewsGatePolicy:
    """Frozen gate identity: which classifications count, and the timing bounds."""

    question_set_version: str = ADVERSE_NEWS_QUESTION_SET_VERSION
    model_version: str = "1.14.0"
    model_released_on: date = ADVERSE_NEWS_MODEL_RELEASED_ON
    lookback_days: int = LOOKBACK_DAYS
    coverage_min_delay: timedelta = timedelta(hours=6)
    deadline: timedelta = timedelta(hours=72)

    def __post_init__(self) -> None:
        for name in ("question_set_version", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                _reject(f"{name} must be non-blank canonical text")
        if not isinstance(self.model_released_on, date) or isinstance(
            self.model_released_on, datetime
        ):
            _reject("model_released_on must be a date")
        if (
            isinstance(self.lookback_days, bool)
            or not isinstance(self.lookback_days, int)
            or self.lookback_days <= 0
        ):
            _reject("lookback_days must be a positive integer")
        if self.coverage_min_delay < timedelta(0):
            _reject("coverage_min_delay cannot be negative")
        if self.deadline <= self.coverage_min_delay:
            _reject("deadline must be later than coverage_min_delay")


@dataclass(frozen=True, slots=True)
class AdverseNewsGateResult:
    """One instrument's gate outcome; ``reason`` is ``None`` when the gate is clear."""

    reason: GateReason | None
    observation_ids: tuple[str, ...]
    unsettled_count: int


@dataclass(frozen=True, slots=True)
class _Coverage:
    observation_id: str
    window_start: datetime
    window_end: datetime
    returned: frozenset[str]
    skipped: frozenset[str]


@dataclass(slots=True)
class _InstrumentLedger:
    """Mutable per-instrument accumulator; frozen into a result at the end."""

    instrument_id: int
    evidence_valid: bool = True
    coverage_complete: bool = False
    observation_ids: set[str] = field(default_factory=set)
    verdicts: list[ArticleVerdict] = field(default_factory=list)

    def invalidate(self, observation_id: str | None, detail: str) -> None:
        self.evidence_valid = False
        logger.warning(
            "adverse_news_gate.evidence_invalid",
            instrument_id=self.instrument_id,
            observation_id=observation_id,
            detail=detail,
        )

    def result(self) -> AdverseNewsGateResult:
        return AdverseNewsGateResult(
            reason=gate_reason(
                coverage_complete=self.coverage_complete,
                evidence_valid=self.evidence_valid,
                verdicts=self.verdicts,
            ),
            observation_ids=tuple(sorted(self.observation_ids)),
            unsettled_count=sum(verdict in UNSETTLED_VERDICTS for verdict in self.verdicts),
        )


def resolve_quality_compounder_adverse_news_gate(
    session: Session,
    *,
    cutoff: datetime,
    instrument_ids: Sequence[int],
    policy: AdverseNewsGatePolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> Mapping[int, AdverseNewsGateResult]:
    """Return the adverse-news gate result for each instrument at cutoff ``T``."""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        _reject("cutoff must be timezone-aware")
    if not isinstance(policy, AdverseNewsGatePolicy):
        _reject("policy must be an AdverseNewsGatePolicy")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in instrument_ids):
        _reject("instrument_ids must be integers")
    cutoff = cutoff.astimezone(UTC)
    results: dict[int, AdverseNewsGateResult] = {}
    for instrument_id in sorted(set(instrument_ids)):
        ledger = _InstrumentLedger(instrument_id)
        coverage = _admitted_coverage(
            session,
            ledger=ledger,
            cutoff=cutoff,
            policy=policy,
            provider_authority_policy=provider_authority_policy,
        )
        ledger.coverage_complete = _covers(
            coverage, start=cutoff - timedelta(days=policy.lookback_days), end=cutoff
        )
        selected = _admitted_classifications(
            session,
            ledger=ledger,
            cutoff=cutoff,
            policy=policy,
            provider_authority_policy=provider_authority_policy,
        )
        _check_completeness(ledger=ledger, coverage=coverage, selected_articles=selected)
        results[instrument_id] = ledger.result()
        logger.info(
            "adverse_news_gate.resolved",
            instrument_id=instrument_id,
            cutoff=cutoff.isoformat(),
            reason=results[instrument_id].reason,
            unsettled_count=results[instrument_id].unsettled_count,
        )
    return results


def _admitted_coverage(
    session: Session,
    *,
    ledger: _InstrumentLedger,
    cutoff: datetime,
    policy: AdverseNewsGatePolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> tuple[_Coverage, ...]:
    """Validate every coverage record that can attest the window ending at ``T``."""
    candidates = session.scalars(
        select(EquityObservation)
        .where(
            EquityObservation.observation_kind == _NEWS_COVERAGE_KIND,
            EquityObservation.instr_id == ledger.instrument_id,
            EquityObservation.disposition == _OBSERVED,
            EquityObservation.event_at >= _stored(session, cutoff),
            EquityObservation.available_at >= _stored(session, cutoff + policy.coverage_min_delay),
            EquityObservation.available_at <= _stored(session, cutoff + policy.deadline),
        )
        .order_by(EquityObservation.observation_id)
    ).all()
    admitted: list[_Coverage] = []
    for observation in _latest_revisions(candidates):
        observation_id = str(observation.observation_id)
        ledger.observation_ids.add(observation_id)
        try:
            validate_optional_factor_evidence(
                session,
                contract=NEWS_COVERAGE_GATE_CONTRACT,
                factor_version=NEWS_COVERAGE_FACTOR_VERSION,
                observation_id=observation_id,
                cutoff=cutoff + policy.deadline,
                provider_authority_policy=provider_authority_policy,
                expected_instrument_id=ledger.instrument_id,
            )
        except _VALIDATION_ERRORS as exc:
            ledger.invalidate(observation_id, str(exc))
            continue
        rows = _values(session, (observation_id,), _COVERAGE_FIELDS).get(observation_id, {})
        record = _coverage_record(observation_id, rows)
        if record is None:
            ledger.invalidate(observation_id, "coverage values are inconsistent")
            continue
        admitted.append(record)
    return tuple(admitted)


def _coverage_record(
    observation_id: str, rows: Mapping[str, list[EquityObservationValue]]
) -> _Coverage | None:
    starts = rows.get("query_window_start", [])
    ends = rows.get("query_window_end", [])
    counts = rows.get("returned_article_count", [])
    if len(starts) != 1 or len(ends) != 1 or len(counts) != 1:
        return None
    start, end, count = starts[0].timestamp_value, ends[0].timestamp_value, counts[0].integer_value
    returned = [row.text_value for row in rows.get("returned_article_identity", [])]
    skipped = [row.text_value for row in rows.get("skipped_article_identity", [])]
    if start is None or end is None or count is None:
        return None
    returned_ids = frozenset(item for item in returned if item)
    skipped_ids = frozenset(item for item in skipped if item)
    if (
        len(returned_ids) != len(returned)
        or len(skipped_ids) != len(skipped)
        or count != len(returned_ids)
        or not skipped_ids <= returned_ids
        or _utc(end) <= _utc(start)
    ):
        return None
    return _Coverage(observation_id, _utc(start), _utc(end), returned_ids, skipped_ids)


def _covers(records: Iterable[_Coverage], *, start: datetime, end: datetime) -> bool:
    """Return whether the half-open windows ``(start_i, end_i]`` span ``(start, end]``."""
    reached = start
    for record in sorted(records, key=lambda item: item.window_start):
        if record.window_start > reached:
            break
        reached = max(reached, record.window_end)
        if reached >= end:
            return True
    return False


def _admitted_classifications(
    session: Session,
    *,
    ledger: _InstrumentLedger,
    cutoff: datetime,
    policy: AdverseNewsGatePolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> frozenset[str]:
    """Select, validate and classify; return the provider article ids that were selected."""
    window_start = cutoff - timedelta(days=policy.lookback_days)
    candidates = session.scalars(
        select(EquityObservation)
        .where(
            EquityObservation.observation_kind == _NEWS_EVENT_KIND,
            EquityObservation.instr_id == ledger.instrument_id,
            EquityObservation.disposition == _OBSERVED,
            EquityObservation.event_at > _stored(session, window_start),
            EquityObservation.available_at <= _stored(session, cutoff),
        )
        .order_by(EquityObservation.observation_id)
    ).all()
    latest = _latest_revisions(candidates)
    rows_by_id = _values(
        session, tuple(str(item.observation_id) for item in latest), _SELECTION_FIELDS
    )
    selected_articles: set[str] = set()
    for observation in latest:
        observation_id = str(observation.observation_id)
        rows = rows_by_id.get(observation_id, {})
        if not _selected(rows, cutoff=cutoff, window_start=window_start, policy=policy):
            continue
        ledger.observation_ids.add(observation_id)
        article_id = _article_id(str(observation.source_record_identity))
        if article_id is not None:
            selected_articles.add(article_id)
        verdict = _admitted_verdict(
            session,
            ledger=ledger,
            observation_id=observation_id,
            rows=rows,
            cutoff=cutoff,
            policy=policy,
            provider_authority_policy=provider_authority_policy,
        )
        if verdict is not None:
            ledger.verdicts.append(verdict)
    return frozenset(selected_articles)


def _selected(
    rows: Mapping[str, list[EquityObservationValue]],
    *,
    cutoff: datetime,
    window_start: datetime,
    policy: AdverseNewsGatePolicy,
) -> bool:
    """Apply spec section 8 selection; a mismatch is a skip, never an error."""
    if _single_text(rows, "question_set_version") != policy.question_set_version:
        return False
    if _single_text(rows, "model_version") != policy.model_version:
        return False
    available = _single_timestamp(rows, "source_available_at")
    published = _single_timestamp(rows, "original_published_at")
    # A missing timestamp value is selected so the validator rejects it, not skipped.
    if available is not None and available > cutoff:
        return False
    return published is None or published > window_start


def _admitted_verdict(
    session: Session,
    *,
    ledger: _InstrumentLedger,
    observation_id: str,
    rows: Mapping[str, list[EquityObservationValue]],
    cutoff: datetime,
    policy: AdverseNewsGatePolicy,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> ArticleVerdict | None:
    """Run admissibility checks 1-3 and return the verdict, or ``None`` when invalid."""
    try:
        validate_optional_factor_evidence(
            session,
            contract=ADVERSE_NEWS_GATE_CONTRACT,
            factor_version=policy.question_set_version,
            observation_id=observation_id,
            cutoff=cutoff,
            provider_authority_policy=provider_authority_policy,
            expected_instrument_id=ledger.instrument_id,
        )
        answers = AdverseNewsAnswers.from_values(
            {
                name: [(int(row.ordinal), _decimal(row)) for row in rows.get(name, [])]
                for name in _PROBABILITY_FIELDS
                if name in rows
            }
        )
    except _VALIDATION_ERRORS as exc:
        ledger.invalidate(observation_id, str(exc))
        return None
    if policy.model_released_on > cutoff.date():
        ledger.invalidate(observation_id, "pinned model was released after the cutoff")
        return None
    classified_at = _single_timestamp(rows, "classified_at")
    if classified_at is None or classified_at > cutoff + policy.deadline:
        ledger.invalidate(observation_id, "classified after the panel-build deadline")
        return None
    return classify_article(answers)


def _check_completeness(
    *,
    ledger: _InstrumentLedger,
    coverage: Sequence[_Coverage],
    selected_articles: frozenset[str],
) -> None:
    """Require a skip or a selected classification for every returned article.

    Coverage counts only articles available by ``T`` (spec section 7), so the
    provider adapter must not list later ones. Nothing excuses a listed article:
    another version, a later correction, or a column outside the window all
    leave it missing, which fails the instrument closed.
    """
    for record in coverage:
        for article_id in sorted(record.returned - record.skipped - selected_articles):
            ledger.invalidate(
                record.observation_id,
                f"coverage counts article {article_id!r} without a selected classification",
            )


def _latest_revisions(observations: Iterable[EquityObservation]) -> tuple[EquityObservation, ...]:
    """Keep only the highest revision of each source record."""
    latest: dict[str, EquityObservation] = {}
    for observation in observations:
        identity = str(observation.source_record_identity)
        current = latest.get(identity)
        if current is None or int(observation.revision) > int(current.revision):
            latest[identity] = observation
    return tuple(sorted(latest.values(), key=lambda item: str(item.observation_id)))


def _values(
    session: Session, observation_ids: Sequence[str], fields: frozenset[str]
) -> dict[str, dict[str, list[EquityObservationValue]]]:
    by_observation: dict[str, dict[str, list[EquityObservationValue]]] = {}
    if not observation_ids:
        return by_observation
    rows = session.scalars(
        select(EquityObservationValue)
        .where(
            EquityObservationValue.observation_id.in_(observation_ids),
            EquityObservationValue.field_name.in_(sorted(fields)),
        )
        .order_by(
            EquityObservationValue.observation_id,
            EquityObservationValue.field_name,
            EquityObservationValue.ordinal,
        )
    )
    for row in rows:
        by_observation.setdefault(str(row.observation_id), {}).setdefault(
            str(row.field_name), []
        ).append(row)
    return by_observation


def _article_id(source_record_identity: str) -> str | None:
    """Recover the provider article id from ``article|instrument|questions|model``."""
    parts = source_record_identity.rsplit(_IDENTITY_SEPARATOR, _IDENTITY_PARTS - 1)
    return parts[0] if len(parts) == _IDENTITY_PARTS and parts[0] else None


def _single_text(rows: Mapping[str, list[EquityObservationValue]], field: str) -> str | None:
    values = rows.get(field, [])
    return values[0].text_value if len(values) == 1 else None


def _single_timestamp(
    rows: Mapping[str, list[EquityObservationValue]], field: str
) -> datetime | None:
    values = rows.get(field, [])
    if len(values) != 1 or values[0].timestamp_value is None:
        return None
    return _utc(values[0].timestamp_value)


def _decimal(row: EquityObservationValue) -> Decimal:
    if row.decimal_value is None:
        message = f"{row.field_name} must be a stored decimal"
        raise AdverseNewsAnswerError(message)
    return Decimal(row.decimal_value)


def _stored(session: Session, value: datetime) -> datetime:
    """Bind a UTC instant the way the dialect stores ``DateTime(timezone=True)``."""
    utc_value = value.astimezone(UTC)
    if session.get_bind().dialect.name == "sqlite":
        return utc_value.replace(tzinfo=None)
    return utc_value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AdverseNewsGateError",
    "AdverseNewsGatePolicy",
    "AdverseNewsGateResult",
    "resolve_quality_compounder_adverse_news_gate",
]
