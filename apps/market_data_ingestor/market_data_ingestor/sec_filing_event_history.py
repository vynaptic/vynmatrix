"""Acceptance-time historical diagnostic for the SEC filing gate rules.

Design spec section 8. For each quarter-end cutoff ``T`` the pure rules are
replayed with EDGAR ``acceptance_time <= T`` standing in for local
``available_at <= T``. The proxy is a diagnostic only: it never enters a panel,
and every report carries ``label = "acceptance-time diagnostic"``.

Coverage is synthesised as complete (the recorded submissions are the whole
history), so ``sec_filing_coverage_unavailable`` never appears. Membership
without a recorded point-in-time checkpoint falls back to the latest recorded
checkpoint and marks the report survivorship-biased; such a report can argue
only for dropping a rule. The repository has no reader of forward 63-session
returns, so the command reports ``None`` for every return statistic.

``python -m market_data_ingestor.sec_filing_event_history --submissions-dir DIR``
reads recorded EDGAR submissions JSON (roots ``CIK##########.json`` plus every
history page each root declares) and read-only membership/sector rows, then
prints the report as JSON. It never writes to the database and never calls EDGAR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservationValue,
    EquitySecurityIdentity,
    IndexMembership,
)
from lib_strategy.sec_filing_rules import (
    GATE_RELEVANT_FORMS,
    SEC_FILING_FETCH_LOOKBACK,
    SEC_FILING_RULES,
    SecFilingCoverage,
    SecFilingEvent,
    SecFilingGateReason,
    sec_filing_gate_reason,
)
from market_data_ingestor.sec_edgar import (
    SecFilingRecord,
    SecSourceDocument,
    SecSubmissionDataset,
    _merge_submission_datasets,
    _submission_page_names,
    parse_submissions_payload,
)

DIAGNOSTIC_LABEL: Final = "acceptance-time diagnostic"
SAFETY_RULE_FLAG_THRESHOLD: Final = 30
LEFT_TAIL_RETURN: Final = -0.20
_RULE_REASONS: Final = tuple(rule.reason.value for rule in SEC_FILING_RULES)
_ROOT_FILE: Final = re.compile(r"^CIK(\d{10})\.json$")
_SUBMISSIONS_URL: Final = "https://data.sec.gov/submissions"
_UNIVERSE: Final = "SP500"
_CIK_PREFIX: Final = "cik:"
_CIK_WIDTH: Final = 10
_DEFAULT_START: Final = date(2010, 1, 1)


@dataclass(frozen=True)
class SecFilingDiagnosticQuarter:
    """Gate outcome for one quarter-end cutoff."""

    cutoff: datetime
    members: int
    removed_by_reason: Mapping[str, int]
    removed_by_sector: Mapping[str, int]
    invalid_items: int


@dataclass(frozen=True)
class SecFilingDiagnosticReport:
    """Per-quarter removals and per-rule forward-return statistics.

    ``per_rule_safety_rule`` marks rules with fewer than 30 flags in the study:
    they keep their numbers but are retained on prior grounds (spec bar 2).
    """

    quarters: tuple[SecFilingDiagnosticQuarter, ...]
    mean_removal_rate: float
    per_rule_flags: Mapping[str, int]
    per_rule_excess_return_63: Mapping[str, float | None]
    per_rule_left_tail_share: Mapping[str, float | None]
    per_rule_safety_rule: Mapping[str, bool]
    unflagged_excess_return_63: float | None
    unflagged_left_tail_share: float | None
    survivorship_biased: bool
    label: Literal["acceptance-time diagnostic"] = DIAGNOSTIC_LABEL


def _events_at(records: Sequence[SecFilingRecord], cutoff: datetime) -> tuple[SecFilingEvent, ...]:
    earliest = cutoff - SEC_FILING_FETCH_LOOKBACK
    return tuple(
        SecFilingEvent(
            accession=record.accession,
            form=record.form,
            items=record.items,
            acceptance_time=record.acceptance_time,
            observation_id=f"replay:{record.accession}",
        )
        for record in records
        if record.form in GATE_RELEVANT_FORMS and earliest < record.acceptance_time <= cutoff
    )


def _complete_coverage(events: Sequence[SecFilingEvent], cutoff: datetime) -> SecFilingCoverage:
    return SecFilingCoverage(
        window_end=cutoff,
        oldest_listed_filing_date=None,
        has_older_page=False,
        listed_accessions=frozenset(event.accession for event in events),
        observation_id="replay:coverage",
    )


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _tail_share(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value < LEFT_TAIL_RETURN) / len(values)


def replay_sec_filing_rules(
    *,
    filings_by_cik: Mapping[str, Sequence[SecFilingRecord]],
    members_by_cutoff: Mapping[datetime, Mapping[int, str]],
    sector_by_instrument: Mapping[int, str],
    returns_63: Mapping[tuple[datetime, int], float],
    cutoffs: Sequence[datetime],
    point_in_time_membership: bool,
) -> SecFilingDiagnosticReport:
    """Replay the code-only rules at each cutoff; pure, no I/O.

    ``members_by_cutoff`` maps each cutoff to ``instrument_id -> CIK``;
    ``returns_63`` holds the next 63-session total return per
    ``(cutoff, instrument_id)`` where recorded.
    """
    if not cutoffs:
        msg = "at least one cutoff is required"
        raise ValueError(msg)
    quarters: list[SecFilingDiagnosticQuarter] = []
    rates: list[float] = []
    flags: Counter[str] = Counter()
    flagged_excess: dict[str, list[float]] = defaultdict(list)
    flagged_returns: dict[str, list[float]] = defaultdict(list)
    unflagged_excess: list[float] = []
    unflagged_returns: list[float] = []
    for cutoff in sorted(cutoffs):
        members = members_by_cutoff.get(cutoff)
        if members is None:
            msg = f"membership is missing for cutoff {cutoff.isoformat()}"
            raise ValueError(msg)
        missing_sector = sorted(set(members) - set(sector_by_instrument))
        if missing_sector:
            msg = f"sector is missing for instruments {missing_sector}"
            raise ValueError(msg)

        sector_returns: dict[str, list[float]] = defaultdict(list)
        for instrument_id in members:
            value = returns_63.get((cutoff, instrument_id))
            if value is not None:
                sector_returns[sector_by_instrument[instrument_id]].append(value)
        sector_median = {sector: statistics.median(vals) for sector, vals in sector_returns.items()}

        by_reason: Counter[str] = Counter()
        by_sector: Counter[str] = Counter()
        invalid = 0
        for instrument_id, cik in sorted(members.items()):
            events = _events_at(filings_by_cik.get(cik, ()), cutoff)
            result = sec_filing_gate_reason(
                events=events, coverage=_complete_coverage(events, cutoff), cutoff=cutoff
            )
            if result.reason is SecFilingGateReason.COVERAGE_UNAVAILABLE:
                msg = "synthetic complete coverage was reported unavailable"
                raise RuntimeError(msg)
            sector = sector_by_instrument[instrument_id]
            fired = {reason.value for reason in result.fired}
            if result.reason is not None:
                by_reason[result.reason.value] += 1
                by_sector[sector] += 1
            if SecFilingGateReason.EVIDENCE_INVALID.value in fired:
                invalid += 1
            total = returns_63.get((cutoff, instrument_id))
            excess = None if total is None else total - sector_median[sector]
            rule_hits = [reason for reason in _RULE_REASONS if reason in fired]
            for reason in rule_hits:
                flags[reason] += 1
                if total is not None and excess is not None:
                    flagged_excess[reason].append(excess)
                    flagged_returns[reason].append(total)
            if result.reason is None and total is not None and excess is not None:
                unflagged_excess.append(excess)
                unflagged_returns.append(total)

        removed = sum(by_reason.values())
        if members:
            rates.append(removed / len(members))
        quarters.append(
            SecFilingDiagnosticQuarter(
                cutoff=cutoff,
                members=len(members),
                removed_by_reason=dict(sorted(by_reason.items())),
                removed_by_sector=dict(sorted(by_sector.items())),
                invalid_items=invalid,
            )
        )

    return SecFilingDiagnosticReport(
        quarters=tuple(quarters),
        mean_removal_rate=statistics.fmean(rates) if rates else 0.0,
        per_rule_flags={reason: flags[reason] for reason in _RULE_REASONS},
        per_rule_excess_return_63={
            reason: _mean(flagged_excess[reason]) for reason in _RULE_REASONS
        },
        per_rule_left_tail_share={
            reason: _tail_share(flagged_returns[reason]) for reason in _RULE_REASONS
        },
        per_rule_safety_rule={
            reason: flags[reason] < SAFETY_RULE_FLAG_THRESHOLD for reason in _RULE_REASONS
        },
        unflagged_excess_return_63=_mean(unflagged_excess),
        unflagged_left_tail_share=_tail_share(unflagged_returns),
        survivorship_biased=not point_in_time_membership,
    )


def quarter_end_cutoffs(*, start: date, end: date) -> tuple[datetime, ...]:
    """Calendar quarter ends in ``[start, end]``, each at the last instant of the UTC day."""
    days = (
        date(year, month, 1) + timedelta(days=31)
        for year in range(start.year, end.year + 1)
        for month in (3, 6, 9, 12)
    )
    quarter_ends = (day - timedelta(days=day.day) for day in days)
    return tuple(
        datetime.combine(day, time.max, tzinfo=UTC) for day in quarter_ends if start <= day <= end
    )


def _recorded_dataset(path: Path, *, cik: str) -> tuple[dict[str, Any], SecSubmissionDataset]:
    content = path.read_bytes()
    payload = json.loads(content)
    if not isinstance(payload, dict):
        msg = f"recorded submissions file {path.name} is not a JSON object"
        raise TypeError(msg)
    source = SecSourceDocument(
        endpoint=f"{_SUBMISSIONS_URL}/{path.name}",
        retrieved_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return payload, parse_submissions_payload(payload, cik=cik, source=source)


def load_recorded_submissions(directory: Path) -> dict[str, tuple[SecFilingRecord, ...]]:
    """Parse every recorded root and its declared history pages, keyed by CIK.

    A history page the root declares but the directory lacks fails closed.
    """
    filings: dict[str, tuple[SecFilingRecord, ...]] = {}
    for root_path in sorted(directory.iterdir()):
        match = _ROOT_FILE.fullmatch(root_path.name)
        if match is None:
            continue
        cik = match.group(1)
        root_payload, root_dataset = _recorded_dataset(root_path, cik=cik)
        datasets = [root_dataset]
        for page_name in _submission_page_names(root_payload):
            page_path = directory / page_name
            if not page_path.is_file():
                msg = f"recorded submissions page {page_name} is missing"
                raise FileNotFoundError(msg)
            datasets.append(_recorded_dataset(page_path, cik=cik)[1])
        filings[cik] = _merge_submission_datasets(datasets).filings
    return filings


def _effective_on(effective_from: date, effective_to: date | None, day: date) -> bool:
    return effective_from <= day and (effective_to is None or day <= effective_to)


def _cik(issuer_id: str, *, instrument_id: int) -> str:
    cik = issuer_id.removeprefix(_CIK_PREFIX)
    if not issuer_id.startswith(_CIK_PREFIX) or not cik.isdigit() or len(cik) != _CIK_WIDTH:
        msg = f"instrument {instrument_id} lacks an exact ten-digit CIK issuer identity"
        raise ValueError(msg)
    return cik


def read_membership_inputs(
    session: Session,
    *,
    cutoffs: Sequence[datetime],
) -> tuple[dict[datetime, dict[int, str]], dict[int, str], bool]:
    """Read recorded S&P 500 checkpoints, CIKs and sectors; read-only.

    Returns ``(members_by_cutoff, sector_by_instrument, point_in_time)``. A
    cutoff with no checkpoint effective on its date uses the latest recorded
    checkpoint, and ``point_in_time`` is then false.
    """
    memberships = tuple(
        session.scalars(select(IndexMembership).where(IndexMembership.index_code == _UNIVERSE))
    )
    if not memberships:
        msg = "no recorded S&P 500 membership checkpoint exists"
        raise ValueError(msg)
    identities = tuple(
        session.scalars(
            select(EquitySecurityIdentity).where(
                EquitySecurityIdentity.instr_id.in_({int(row.instr_id) for row in memberships})
            )
        )
    )
    latest_day = max(row.effective_from for row in memberships)
    identities_by_instrument: dict[int, list[EquitySecurityIdentity]] = defaultdict(list)
    for item in identities:
        identities_by_instrument[int(item.instr_id)].append(item)

    def members_on(day: date) -> dict[int, str]:
        members: dict[int, str] = {}
        for row in memberships:
            if not _effective_on(row.effective_from, row.effective_to, day):
                continue
            instrument_id = int(row.instr_id)
            matches = [
                item
                for item in identities_by_instrument[instrument_id]
                if _effective_on(item.effective_from, item.effective_to, day)
            ]
            if len(matches) != 1 or instrument_id in members:
                msg = f"membership on {day.isoformat()} lacks one identity for {instrument_id}"
                raise ValueError(msg)
            members[instrument_id] = _cik(str(matches[0].issuer_id), instrument_id=instrument_id)
        return members

    point_in_time = True
    members_by_cutoff: dict[datetime, dict[int, str]] = {}
    for cutoff in cutoffs:
        members = members_on(cutoff.date())
        if not members:
            point_in_time = False
            members = members_on(latest_day)
        members_by_cutoff[cutoff] = members
    return members_by_cutoff, _latest_sectors(session, identities), point_in_time


def _latest_sectors(
    session: Session,
    identities: Sequence[EquitySecurityIdentity],
) -> dict[int, str]:
    latest: dict[int, EquitySecurityIdentity] = {}
    for item in identities:
        current = latest.get(int(item.instr_id))
        if current is None or item.effective_from > current.effective_from:
            latest[int(item.instr_id)] = item
    rows = tuple(
        session.scalars(
            select(EquityObservationValue).where(
                EquityObservationValue.observation_id.in_(
                    {str(item.observation_id) for item in latest.values()}
                ),
                EquityObservationValue.field_name == "sector",
                EquityObservationValue.ordinal == 0,
            )
        )
    )
    by_observation: dict[str, str] = {}
    for row in rows:
        sector = str(row.text_value or "").strip()
        if not sector or str(row.observation_id) in by_observation:
            msg = "recorded member sector evidence is missing or ambiguous"
            raise ValueError(msg)
        by_observation[str(row.observation_id)] = sector
    return {
        instrument_id: by_observation[str(item.observation_id)]
        for instrument_id, item in sorted(latest.items())
        if str(item.observation_id) in by_observation
    }


def report_json(report: SecFilingDiagnosticReport) -> dict[str, Any]:
    """JSON-ready form of the report with ISO-8601 cutoffs."""
    return {
        "label": report.label,
        "survivorship_biased": report.survivorship_biased,
        "mean_removal_rate": report.mean_removal_rate,
        "per_rule_flags": dict(report.per_rule_flags),
        "per_rule_safety_rule": dict(report.per_rule_safety_rule),
        "per_rule_excess_return_63": dict(report.per_rule_excess_return_63),
        "per_rule_left_tail_share": dict(report.per_rule_left_tail_share),
        "unflagged_excess_return_63": report.unflagged_excess_return_63,
        "unflagged_left_tail_share": report.unflagged_left_tail_share,
        "quarters": [
            {
                "cutoff": quarter.cutoff.isoformat(),
                "members": quarter.members,
                "removed_by_reason": dict(quarter.removed_by_reason),
                "removed_by_sector": dict(quarter.removed_by_sector),
                "invalid_items": quarter.invalid_items,
            }
            for quarter in report.quarters
        ],
    }


def _session_factory() -> Callable[[], Session]:
    from lib_application.db.session import (  # noqa: PLC0415
        create_engine_for_env,
        get_session_factory,
    )
    from lib_common.env_utils import build_database_url  # noqa: PLC0415

    engine = create_engine_for_env(db_url=build_database_url())
    return get_session_factory(engine=engine)


def _latest_complete_quarter_end(today: date) -> date:
    first_month = 3 * ((today.month - 1) // 3) + 1
    return date(today.year, first_month, 1) - timedelta(days=1)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the acceptance-time diagnostic report as JSON to stdout."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--submissions-dir",
        type=Path,
        required=True,
        help="Directory of recorded EDGAR submissions roots and history pages.",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=_DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    if not args.submissions_dir.is_dir():
        parser.error(f"--submissions-dir is not a directory: {args.submissions_dir}")
    end = args.end or _latest_complete_quarter_end(datetime.now(UTC).date())
    cutoffs = quarter_end_cutoffs(start=args.start, end=end)
    if not cutoffs:
        parser.error("no quarter end falls between --start and --end")

    filings = load_recorded_submissions(args.submissions_dir)
    with _session_factory()() as session:
        members_by_cutoff, sectors, point_in_time = read_membership_inputs(session, cutoffs=cutoffs)
        session.rollback()
    report = replay_sec_filing_rules(
        filings_by_cik=filings,
        members_by_cutoff=members_by_cutoff,
        sector_by_instrument=sectors,
        returns_63={},
        cutoffs=cutoffs,
        point_in_time_membership=point_in_time,
    )
    json.dump(report_json(report), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
