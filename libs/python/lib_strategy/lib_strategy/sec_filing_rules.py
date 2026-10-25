"""Pure SEC filing event rules for the quality-compounder quarter-end gate.

Code-only layer: no I/O and no database access. A member is excluded when a
recorded 8-K item code or NT late-filing notice falls inside a rule window
ending at the panel cutoff. Coverage and evidence problems fail closed.

Any change to ``SEC_FILING_RULES`` or the evidence constants must bump
``SEC_FILING_RULES_VERSION``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

SEC_FILING_RULES_VERSION = "sec-filing-rules-v1"


class SecFilingGateReason(StrEnum):
    """Exclusion reasons, declared in precedence order (first wins)."""

    COVERAGE_UNAVAILABLE = "sec_filing_coverage_unavailable"
    EVIDENCE_INVALID = "sec_filing_evidence_invalid"
    BANKRUPTCY_OR_RECEIVERSHIP = "sec_bankruptcy_or_receivership"
    NON_RELIANCE = "sec_non_reliance"
    LATE_PERIODIC_REPORT = "sec_late_periodic_report"
    MATERIAL_IMPAIRMENT = "sec_material_impairment"


_PRECEDENCE = {reason: rank for rank, reason in enumerate(SecFilingGateReason)}


@dataclass(frozen=True)
class SecFilingRule:
    """A form/item pair that fires when accepted inside ``(T - window, T]``."""

    forms: frozenset[str]
    item: str | None
    window: timedelta
    reason: SecFilingGateReason


_EIGHT_K_FORMS = frozenset({"8-K", "8-K/A"})
_NT_FORMS = frozenset({"NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A"})

SEC_FILING_RULES: tuple[SecFilingRule, ...] = (
    SecFilingRule(_EIGHT_K_FORMS, "4.02", timedelta(days=365), SecFilingGateReason.NON_RELIANCE),
    SecFilingRule(
        _EIGHT_K_FORMS, "1.03", timedelta(days=730), SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP
    ),
    SecFilingRule(_NT_FORMS, None, timedelta(days=180), SecFilingGateReason.LATE_PERIODIC_REPORT),
    SecFilingRule(
        _EIGHT_K_FORMS, "2.06", timedelta(days=120), SecFilingGateReason.MATERIAL_IMPAIRMENT
    ),
)

SEC_FILING_EVIDENCE_SCOPE = timedelta(days=730)
SEC_FILING_COVERAGE_TOLERANCE = timedelta(hours=6)
SEC_FILING_FETCH_LOOKBACK = timedelta(days=760)
GATE_RELEVANT_FORMS: frozenset[str] = _EIGHT_K_FORMS | _NT_FORMS

_ITEM_PATTERN = re.compile(r"[0-9]\.[0-9]{2}")


def parse_sec_items(raw: Sequence[str]) -> tuple[str, ...] | None:
    """Return stripped item codes, or ``None`` when empty or any item is malformed."""
    if not raw:
        return None
    parsed: list[str] = []
    for value in raw:
        item = value.strip()
        if _ITEM_PATTERN.fullmatch(item) is None:
            return None
        parsed.append(item)
    return tuple(parsed)


@dataclass(frozen=True)
class SecFilingEvent:
    accession: str
    form: str
    items: tuple[str, ...]
    acceptance_time: datetime
    observation_id: str


@dataclass(frozen=True)
class SecFilingCoverage:
    window_end: datetime
    oldest_listed_filing_date: date | None
    has_older_page: bool
    listed_accessions: frozenset[str]
    observation_id: str


@dataclass(frozen=True)
class SecFilingGateResult:
    reason: SecFilingGateReason | None
    fired: tuple[SecFilingGateReason, ...]
    accessions: tuple[str, ...]
    observation_ids: tuple[str, ...]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        message = f"{name} must be timezone-aware"
        raise ValueError(message)


def _coverage_unavailable(coverage: SecFilingCoverage | None, cutoff: datetime) -> bool:
    if coverage is None:
        return True
    _require_aware(coverage.window_end, "coverage.window_end")
    if coverage.window_end > cutoff:
        return True
    if coverage.window_end < cutoff - SEC_FILING_COVERAGE_TOLERANCE:
        return True
    if coverage.has_older_page:
        scope_start = (cutoff - SEC_FILING_EVIDENCE_SCOPE).date()
        oldest = coverage.oldest_listed_filing_date
        if oldest is None or oldest > scope_start:
            return True
    return False


def _result(
    fired: Iterable[SecFilingGateReason],
    accessions: Iterable[str],
    observation_ids: Iterable[str],
) -> SecFilingGateResult:
    ordered = tuple(sorted(set(fired), key=_PRECEDENCE.__getitem__))
    return SecFilingGateResult(
        reason=ordered[0] if ordered else None,
        fired=ordered,
        accessions=tuple(sorted(set(accessions))),
        observation_ids=tuple(sorted(set(observation_ids))),
    )


def sec_filing_gate_reason(
    *,
    events: Sequence[SecFilingEvent],
    coverage: SecFilingCoverage | None,
    cutoff: datetime,
) -> SecFilingGateResult:
    """Resolve the gate for one issuer at ``cutoff``; first reason by precedence wins."""
    _require_aware(cutoff, "cutoff")
    for event in events:
        _require_aware(event.acceptance_time, "event.acceptance_time")

    observation_ids = [event.observation_id for event in events]
    if coverage is not None:
        observation_ids.append(coverage.observation_id)

    if _coverage_unavailable(coverage, cutoff):
        return _result((SecFilingGateReason.COVERAGE_UNAVAILABLE,), (), observation_ids)
    assert coverage is not None

    fired: set[SecFilingGateReason] = set()
    accessions: set[str] = set()

    known = {event.accession for event in events}
    if coverage.listed_accessions - known:
        fired.add(SecFilingGateReason.EVIDENCE_INVALID)

    scope_start = cutoff - SEC_FILING_EVIDENCE_SCOPE
    for event in events:
        if event.form not in _EIGHT_K_FORMS:
            continue
        if scope_start < event.acceptance_time <= cutoff:
            parsed = parse_sec_items(event.items)
            if parsed is None or len(parsed) != len(event.items):
                fired.add(SecFilingGateReason.EVIDENCE_INVALID)
                accessions.add(event.accession)

    for event in events:
        if event.form not in GATE_RELEVANT_FORMS:
            continue
        parsed_items = set(parse_sec_items(event.items) or ())
        for rule in SEC_FILING_RULES:
            if event.form not in rule.forms:
                continue
            if rule.item is not None and rule.item not in parsed_items:
                continue
            if cutoff - rule.window < event.acceptance_time <= cutoff:
                fired.add(rule.reason)
                accessions.add(event.accession)

    return _result(fired, accessions, observation_ids)
