"""Prospective daily-market evidence for the US Quality Compounder.

The acquisition boundary performs only EODHD HTTP work and pure normalization.
Persistence receives the completed bundle later in a caller-owned transaction.
Raw provider OHLC and corporate actions remain immutable source observations;
split and total-return coordinates and transaction costs are derived in-house.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import NoReturn, Protocol

from sqlalchemy.orm import Session

from lib_application.db.models import EquityObservation
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
)
from lib_common.hashing import canonical_json_hash
from lib_data.adjustments import AdjustmentEvent, cumulative_factors_asof
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from lib_strategy.equity_transaction_costs import (
    DailyBarCostModelPolicy,
    DailyBarCostObservation,
    ModeledDailyTransactionCost,
    estimate_daily_bar_transaction_costs,
)

from .equity_evidence import (
    EODHD_PAPER_ENTITLEMENT,
    build_eodhd_corporate_action_submissions,
    build_eodhd_daily_bar_submissions,
    persist_equity_evidence_batch,
)
from .quality_compounder_universe import QualityCompounderSecurityIdentity

QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY = "in-house-split-and-dividend-total-return-v1"
_WRITER_VERSION = "vynmatrix-quality-compounder-market-v1"
_MINIMUM_HISTORY_SESSIONS = 2
_PERSISTED_DECIMAL_QUANTUM = Decimal("0.000000000000000001")


class QualityCompounderMarketError(RuntimeError):
    """Market evidence is outside its authority window or structurally incomplete."""


def _invalid(message: str) -> NoReturn:
    raise QualityCompounderMarketError(message)


class QualityCompounderMarketClient(Protocol):
    """Existing EODHD methods required by the isolated market acquisition."""

    def fetch_daily_bar_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence: ...

    def fetch_split_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence: ...

    def fetch_dividend_evidence(
        self,
        *,
        product_id: str,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence: ...


@dataclass(frozen=True, slots=True)
class QualityCompounderDailyMarketRecord:
    """One official session in raw, split, and total-return coordinates."""

    session_date: date
    closes_at: datetime
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    split_adjusted_volume: Decimal
    split_adjustment_factor: Decimal
    split_adjusted_open: Decimal
    split_adjusted_high: Decimal
    split_adjusted_low: Decimal
    split_adjusted_close: Decimal
    total_return_close: Decimal
    source_observation_identity: str
    source_content_sha256: str
    cost: ModeledDailyTransactionCost | None


@dataclass(frozen=True, slots=True)
class AcquiredQualityCompounderMarketSeries:
    """One complete network-acquired source graph ready for atomic persistence."""

    identity: QualityCompounderSecurityIdentity
    instrument_id: int
    decision_session: date
    decision_close: datetime
    next_open: datetime
    available_at: datetime
    required_session_dates: tuple[date, ...]
    missing_prelisting_session_dates: tuple[date, ...]
    daily_evidence: EODHDJsonEvidence
    split_evidence: EODHDJsonEvidence
    dividend_evidence: EODHDJsonEvidence
    records: tuple[QualityCompounderDailyMarketRecord, ...]
    submissions: tuple[EquityObservationSubmission, ...]
    cost_policy: DailyBarCostModelPolicy


def acquire_quality_compounder_market_series(
    *,
    client: QualityCompounderMarketClient,
    identity: QualityCompounderSecurityIdentity,
    instrument_id: int,
    official_sessions: Sequence[tuple[datetime, datetime]],
    decision_session: date,
    required_history_sessions: int,
    cost_policy: DailyBarCostModelPolicy,
    entitlement_owner_user_id: str,
) -> AcquiredQualityCompounderMarketSeries:
    """Acquire and normalize one EOD/action graph without opening a DB session."""

    if not isinstance(identity, QualityCompounderSecurityIdentity):
        _invalid("market acquisition requires a quality-compounder security identity")
    if not isinstance(cost_policy, DailyBarCostModelPolicy):
        _invalid("market acquisition requires a frozen daily-bar cost policy")
    if isinstance(instrument_id, bool) or instrument_id < 1:
        _invalid("market acquisition requires a positive exact instrument_id")
    if (
        isinstance(required_history_sessions, bool)
        or required_history_sessions < _MINIMUM_HISTORY_SESSIONS
    ):
        _invalid("market acquisition requires at least two history sessions")
    owner = _text(entitlement_owner_user_id, field_name="entitlement_owner_user_id")
    selected, decision_close, next_open = _select_official_window(
        official_sessions,
        decision_session=decision_session,
        required_history_sessions=required_history_sessions,
    )
    selected_dates = tuple(close_at.date() for _open_at, close_at in selected)
    start = selected_dates[0]
    daily = client.fetch_daily_bar_evidence(
        product_id=identity.symbol,
        start=start,
        end=decision_session,
    )
    splits = client.fetch_split_evidence(
        product_id=identity.symbol,
        start=start,
        end=decision_session,
    )
    dividends = client.fetch_dividend_evidence(
        product_id=identity.symbol,
        start=start,
        end=decision_session,
    )
    _require_acquisition_window(
        (daily, splits, dividends),
        decision_close=decision_close,
        next_open=next_open,
    )
    return _normalize_market_series(
        identity=identity,
        instrument_id=instrument_id,
        selected_sessions=selected,
        decision_session=decision_session,
        next_open=next_open,
        daily_evidence=daily,
        split_evidence=splits,
        dividend_evidence=dividends,
        cost_policy=cost_policy,
        owner=owner,
    )


def persist_quality_compounder_market_series(
    session: Session,
    acquired: AcquiredQualityCompounderMarketSeries,
) -> tuple[EquityObservation, ...]:
    """Persist a completed market graph without performing network work."""

    if not isinstance(acquired, AcquiredQualityCompounderMarketSeries):
        _invalid("market persistence requires an acquired quality-compounder series")
    return persist_equity_evidence_batch(session, acquired.submissions)


def _normalize_market_series(
    *,
    identity: QualityCompounderSecurityIdentity,
    instrument_id: int,
    selected_sessions: tuple[tuple[datetime, datetime], ...],
    decision_session: date,
    next_open: datetime,
    daily_evidence: EODHDJsonEvidence,
    split_evidence: EODHDJsonEvidence,
    dividend_evidence: EODHDJsonEvidence,
    cost_policy: DailyBarCostModelPolicy,
    owner: str,
) -> AcquiredQualityCompounderMarketSeries:
    selected_closes = {close_at.date(): close_at for _open_at, close_at in selected_sessions}
    selected_dates = tuple(selected_closes)
    required_dates = selected_dates[1:]
    observed_dates = _daily_dates(daily_evidence)
    unexpected = tuple(sorted(set(observed_dates) - set(selected_dates)))
    if unexpected:
        _invalid(
            f"EODHD {identity.symbol} daily sessions differ from the official calendar: "
            f"unexpected={unexpected!r}"
        )
    missing = tuple(item for item in selected_dates if item not in set(observed_dates))
    _require_prelisting_prefix(identity, selected_dates=selected_dates, missing=missing)
    actual_closes = {session_date: selected_closes[session_date] for session_date in observed_dates}
    raw_submissions = build_eodhd_daily_bar_submissions(
        evidence=daily_evidence,
        instrument_id=instrument_id,
        symbol=identity.symbol,
        currency="USD",
        session_closes=actual_closes,
        entitlement_owner_user_id=owner,
    )
    action_start = selected_dates[0]
    split_submissions = build_eodhd_corporate_action_submissions(
        evidence=split_evidence,
        instrument_id=instrument_id,
        symbol=identity.symbol,
        action_type="split",
        start=action_start,
        end=decision_session,
        entitlement_owner_user_id=owner,
    )
    dividend_submissions = build_eodhd_corporate_action_submissions(
        evidence=dividend_evidence,
        instrument_id=instrument_id,
        symbol=identity.symbol,
        action_type="dividend",
        start=action_start,
        end=decision_session,
        entitlement_owner_user_id=owner,
    )
    adjustments = _adjustment_events((*split_submissions, *dividend_submissions))
    available_at = max(
        daily_evidence.retrieved_at,
        split_evidence.retrieved_at,
        dividend_evidence.retrieved_at,
    )
    preliminary = _adjust_records(
        identity=identity,
        raw_submissions=raw_submissions,
        adjustments=adjustments,
        available_at=available_at,
    )
    costs = _modeled_costs(
        preliminary,
        identity=identity,
        available_at=available_at,
        policy=cost_policy,
    )
    records = tuple(
        QualityCompounderDailyMarketRecord(
            session_date=item.session_date,
            closes_at=item.closes_at,
            raw_open=item.raw_open,
            raw_high=item.raw_high,
            raw_low=item.raw_low,
            raw_close=item.raw_close,
            split_adjusted_volume=item.split_adjusted_volume,
            split_adjustment_factor=item.split_adjustment_factor,
            split_adjusted_open=item.split_adjusted_open,
            split_adjusted_high=item.split_adjusted_high,
            split_adjusted_low=item.split_adjusted_low,
            split_adjusted_close=item.split_adjusted_close,
            total_return_close=item.total_return_close,
            source_observation_identity=item.source_observation_identity,
            source_content_sha256=item.source_content_sha256,
            cost=costs.get(item.session_date),
        )
        for item in preliminary
        if item.session_date in set(required_dates)
    )
    if not records:
        _invalid(f"EODHD {identity.symbol} has no post-listing required market history")
    derived = _derived_submissions(
        identity=identity,
        instrument_id=instrument_id,
        records=records,
        daily_evidence=daily_evidence,
        split_evidence=split_evidence,
        dividend_evidence=dividend_evidence,
        available_at=available_at,
        owner=owner,
        cost_policy=cost_policy,
    )
    return AcquiredQualityCompounderMarketSeries(
        identity=identity,
        instrument_id=instrument_id,
        decision_session=decision_session,
        decision_close=selected_sessions[-1][1],
        next_open=next_open,
        available_at=available_at,
        required_session_dates=required_dates,
        missing_prelisting_session_dates=tuple(
            item for item in required_dates if item in set(missing)
        ),
        daily_evidence=daily_evidence,
        split_evidence=split_evidence,
        dividend_evidence=dividend_evidence,
        records=records,
        submissions=(
            *raw_submissions,
            *split_submissions,
            *dividend_submissions,
            *derived,
        ),
        cost_policy=cost_policy,
    )


def _adjust_records(
    *,
    identity: QualityCompounderSecurityIdentity,
    raw_submissions: Sequence[EquityObservationSubmission],
    adjustments: list[AdjustmentEvent],
    available_at: datetime,
) -> tuple[QualityCompounderDailyMarketRecord, ...]:
    by_date = {_date_value(item, "session_date"): item for item in raw_submissions}
    closes = {
        session_date: float(_decimal_value(item, "close")) for session_date, item in by_date.items()
    }
    result: list[QualityCompounderDailyMarketRecord] = []
    for session_date in sorted(by_date):
        submission = by_date[session_date]
        raw_open = _decimal_value(submission, "open")
        raw_high = _decimal_value(submission, "high")
        raw_low = _decimal_value(submission, "low")
        raw_close = _decimal_value(submission, "close")
        volume = _decimal_value(submission, "split_adjusted_volume")
        split_events = [item for item in adjustments if item.action_type == "split"]
        split_factor = Decimal(
            str(cumulative_factors_asof(split_events, closes, bar_date=session_date).price)
        )
        total_return_factor = Decimal(
            str(cumulative_factors_asof(adjustments, closes, bar_date=session_date).price)
        )
        content_sha256 = canonical_json_hash(
            {
                "daily_normalized_sha256": submission.normalized_content_sha256,
                "schema": "quality-compounder-market-record-source-v1",
                "security_id": identity.security_id,
                "session": session_date.isoformat(),
            }
        )
        source_identity = canonical_json_hash(
            {
                "content_sha256": content_sha256,
                "schema": "quality-compounder-market-record-identity-v1",
                "security_id": identity.security_id,
                "session": session_date.isoformat(),
            }
        )
        result.append(
            QualityCompounderDailyMarketRecord(
                session_date=session_date,
                closes_at=submission.event_at,
                raw_open=raw_open,
                raw_high=raw_high,
                raw_low=raw_low,
                raw_close=raw_close,
                split_adjusted_volume=volume,
                split_adjustment_factor=split_factor,
                split_adjusted_open=raw_open * split_factor,
                split_adjusted_high=raw_high * split_factor,
                split_adjusted_low=raw_low * split_factor,
                split_adjusted_close=raw_close * split_factor,
                total_return_close=raw_close * total_return_factor,
                source_observation_identity=source_identity,
                source_content_sha256=content_sha256,
                cost=None,
            )
        )
    if any(available_at < item.closes_at for item in result):
        _invalid("daily market evidence availability precedes an official close")
    return tuple(result)


def _modeled_costs(
    records: Sequence[QualityCompounderDailyMarketRecord],
    *,
    identity: QualityCompounderSecurityIdentity,
    available_at: datetime,
    policy: DailyBarCostModelPolicy,
) -> Mapping[date, ModeledDailyTransactionCost]:
    if len(records) < _MINIMUM_HISTORY_SESSIONS:
        return {}
    observations = tuple(
        DailyBarCostObservation(
            security_id=identity.security_id,
            symbol=identity.symbol,
            session_date=record.session_date,
            observed_at=record.closes_at,
            available_at=available_at,
            raw_high=float(record.raw_high),
            raw_low=float(record.raw_low),
            raw_close=float(record.raw_close),
            split_adjusted_high=float(record.split_adjusted_high),
            split_adjusted_low=float(record.split_adjusted_low),
            split_adjusted_close=float(record.split_adjusted_close),
            split_adjusted_volume=float(record.split_adjusted_volume),
            split_adjustment_factor=float(record.split_adjustment_factor),
            source_observation_id=record.source_observation_identity,
            source_content_sha256=record.source_content_sha256,
        )
        for record in records
    )
    return {
        item.session_date: item
        for item in estimate_daily_bar_transaction_costs(
            observations,
            policy,
            cost_context_sha256=policy.configuration_sha256,
        )
    }


def _derived_submissions(
    *,
    identity: QualityCompounderSecurityIdentity,
    instrument_id: int,
    records: Sequence[QualityCompounderDailyMarketRecord],
    daily_evidence: EODHDJsonEvidence,
    split_evidence: EODHDJsonEvidence,
    dividend_evidence: EODHDJsonEvidence,
    available_at: datetime,
    owner: str,
    cost_policy: DailyBarCostModelPolicy,
) -> tuple[EquityObservationSubmission, ...]:
    source_revision = canonical_json_hash(
        {
            "cost_policy": cost_policy.configuration_sha256,
            "daily": daily_evidence.content_sha256,
            "dividends": dividend_evidence.content_sha256,
            "schema": "quality-compounder-market-source-graph-v1",
            "splits": split_evidence.content_sha256,
        }
    )
    result: list[EquityObservationSubmission] = []
    for record in records:
        values = _derived_values(record)
        source_record_identity = f"{identity.symbol}.US:{record.session_date}:derived-market"
        normalized_sha256 = canonical_json_hash(
            {
                "schema": "quality-compounder-derived-daily-market-v1",
                "source_record_identity": source_record_identity,
                "values": [item.payload() for item in values],
            }
        )
        result.append(
            EquityObservationSubmission(
                provider="eodhd",
                product="derived-daily-market-with-corporate-actions",
                endpoint=daily_evidence.endpoint,
                dataset_version="prospective-derived-market-v1",
                tool_version=_WRITER_VERSION,
                source_identity=f"eodhd:derived-market:{identity.symbol}.US",
                source_revision=source_revision,
                retrieved_at=available_at,
                timestamp_semantics={
                    "adjusted_close": "ignored",
                    "available_at": "latest completion of daily, split, and dividend retrieval",
                    "cost_policy": cost_policy.to_payload(),
                    "daily_source_sha256": daily_evidence.content_sha256,
                    "dividend_source_sha256": dividend_evidence.content_sha256,
                    "event_at": "official exchange regular-session close",
                    "split_source_sha256": split_evidence.content_sha256,
                    "volume": "provider split-adjusted volume",
                },
                adjustment_policy=QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
                entitlement_scope=EODHD_PAPER_ENTITLEMENT,
                entitlement_owner_user_id=owner,
                missing_data_policy="exact-official-sessions-or-prelisting-prefix-fail-closed",
                artifact_content_sha256=source_revision,
                instrument_id=instrument_id,
                observation_kind="price",
                source_record_identity=source_record_identity,
                event_at=record.closes_at,
                available_at=available_at,
                disposition="observed",
                normalized_content_sha256=normalized_sha256,
                values=values,
            )
        )
    return tuple(result)


def _derived_values(
    record: QualityCompounderDailyMarketRecord,
) -> tuple[EquityObservationValueInput, ...]:
    values = [
        EquityObservationValueInput(
            field_name="corporate_action_clear",
            value_type="boolean",
            value=True,
        ),
        EquityObservationValueInput(
            field_name="raw_close",
            value_type="decimal",
            value=_persisted_decimal(record.raw_close),
            unit="USD",
        ),
        EquityObservationValueInput(
            field_name="session_date",
            value_type="date",
            value=record.session_date,
        ),
        *(
            EquityObservationValueInput(
                field_name=name,
                value_type="decimal",
                value=_persisted_decimal(value),
                unit="USD",
            )
            for name, value in (
                ("split_adjusted_open", record.split_adjusted_open),
                ("split_adjusted_high", record.split_adjusted_high),
                ("split_adjusted_low", record.split_adjusted_low),
                ("split_adjusted_close", record.split_adjusted_close),
                ("total_return_close", record.total_return_close),
            )
        ),
        EquityObservationValueInput(
            field_name="split_adjusted_volume",
            value_type="decimal",
            value=_persisted_decimal(record.split_adjusted_volume),
            unit="provider_split_adjusted_shares",
        ),
        EquityObservationValueInput(
            field_name="split_adjustment_factor",
            value_type="decimal",
            value=_persisted_decimal(record.split_adjustment_factor),
            unit="ratio",
        ),
    ]
    if record.cost is not None:
        values.extend(
            (
                EquityObservationValueInput(
                    field_name="cost_context_sha256",
                    value_type="text",
                    value=record.cost.cost_context_sha256,
                ),
                EquityObservationValueInput(
                    field_name="one_way_nonspread_cost_bps",
                    value_type="decimal",
                    value=_persisted_decimal(
                        Decimal(str(record.cost.estimated_one_way_nonspread_cost_bps))
                    ),
                    unit="bps",
                    context_identity=record.cost.cost_context_sha256,
                ),
                EquityObservationValueInput(
                    field_name="round_trip_spread_bps",
                    value_type="decimal",
                    value=_persisted_decimal(
                        Decimal(str(record.cost.estimated_round_trip_spread_bps))
                    ),
                    unit="bps",
                    context_identity=record.cost.cost_context_sha256,
                ),
            )
        )
    return tuple(sorted(values, key=lambda item: (item.field_name, item.ordinal)))


def _adjustment_events(
    submissions: Sequence[EquityObservationSubmission],
) -> list[AdjustmentEvent]:
    result: list[AdjustmentEvent] = []
    for submission in submissions:
        values = {item.field_name: item for item in submission.values}
        ex_date = values.get("ex_date")
        if ex_date is None:
            continue
        action_type = _text_value(submission, "action_type")
        if action_type == "split":
            result.append(
                AdjustmentEvent(
                    ex_date=_date_value(submission, "ex_date"),
                    action_type="split",
                    ratio=_decimal_value(submission, "ratio"),
                )
            )
        elif action_type == "dividend":
            amount = values.get("amount")
            if amount is None or amount.unit != "USD":
                _invalid("quality-compounder dividends must be explicitly denominated in USD")
            result.append(
                AdjustmentEvent(
                    ex_date=_date_value(submission, "ex_date"),
                    action_type="dividend",
                    amount=_decimal_value(submission, "amount"),
                )
            )
        else:
            _invalid("quality-compounder action evidence has an unsupported type")
    return result


def _select_official_window(
    official_sessions: Sequence[tuple[datetime, datetime]],
    *,
    decision_session: date,
    required_history_sessions: int,
) -> tuple[tuple[tuple[datetime, datetime], ...], datetime, datetime]:
    windows = tuple(
        (
            _utc(open_at, field_name="official open"),
            _utc(close_at, field_name="official close"),
        )
        for open_at, close_at in official_sessions
    )
    if not windows or any(open_at >= close_at for open_at, close_at in windows):
        _invalid("official market sessions are empty or inverted")
    if any(left[1] >= right[0] for left, right in pairwise(windows)):
        _invalid("official market sessions must be strictly ordered and non-overlapping")
    matches = tuple(
        index for index, item in enumerate(windows) if item[1].date() == decision_session
    )
    if len(matches) != 1:
        _invalid("official calendar must contain exactly one decision session")
    decision_index = matches[0]
    if decision_index + 1 >= len(windows):
        _invalid("official calendar lacks the next execution open")
    history_start = decision_index - required_history_sessions
    if history_start < 0:
        _invalid("official calendar lacks the required history plus one warm-up session")
    selected = windows[history_start : decision_index + 1]
    decision_close = selected[-1][1]
    next_open = windows[decision_index + 1][0]
    if next_open <= decision_close:
        _invalid("official decision close and next open are inconsistent")
    return selected, decision_close, next_open


def _require_acquisition_window(
    evidence: Sequence[EODHDJsonEvidence],
    *,
    decision_close: datetime,
    next_open: datetime,
) -> None:
    if not evidence or any(not decision_close < item.retrieved_at < next_open for item in evidence):
        _invalid("EODHD market evidence was not retrieved strictly between close and next open")


def _require_prelisting_prefix(
    identity: QualityCompounderSecurityIdentity,
    *,
    selected_dates: tuple[date, ...],
    missing: tuple[date, ...],
) -> None:
    if not missing:
        return
    listing_session = next(
        (
            item
            for item in selected_dates
            if identity.listing_date is not None and item >= identity.listing_date
        ),
        None,
    )
    expected = (
        tuple(item for item in selected_dates if item < listing_session)
        if listing_session is not None
        else ()
    )
    if missing != expected:
        _invalid(
            f"EODHD {identity.symbol} lacks unexplained official sessions: "
            f"missing={missing!r}; listing_date={identity.listing_date!r}"
        )


def _daily_dates(evidence: EODHDJsonEvidence) -> tuple[date, ...]:
    payload = evidence.payload
    if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
        _invalid("EODHD daily evidence must be an array of objects")
    result: list[date] = []
    for row in payload:
        raw = row.get("date")
        if not isinstance(raw, str):
            _invalid("EODHD daily date must be an ISO date")
        try:
            result.append(date.fromisoformat(raw))
        except ValueError as exc:
            message = "EODHD daily date must be an ISO date"
            raise QualityCompounderMarketError(message) from exc
    if len(result) != len(set(result)):
        _invalid("EODHD daily evidence contains duplicate sessions")
    return tuple(sorted(result))


def _value(
    submission: EquityObservationSubmission,
    field_name: str,
) -> EquityObservationValueInput:
    matches = tuple(item for item in submission.values if item.field_name == field_name)
    if len(matches) != 1:
        _invalid(f"normalized market evidence lacks one exact {field_name}")
    return matches[0]


def _decimal_value(submission: EquityObservationSubmission, field_name: str) -> Decimal:
    value = _value(submission, field_name)
    if value.value_type != "decimal" or not isinstance(value.value, Decimal):
        _invalid(f"normalized market {field_name} must be decimal")
    return value.value


def _date_value(submission: EquityObservationSubmission, field_name: str) -> date:
    value = _value(submission, field_name)
    if value.value_type != "date" or not isinstance(value.value, date):
        _invalid(f"normalized market {field_name} must be a date")
    return value.value


def _text_value(submission: EquityObservationSubmission, field_name: str) -> str:
    value = _value(submission, field_name)
    if value.value_type != "text" or not isinstance(value.value, str):
        _invalid(f"normalized market {field_name} must be text")
    return value.value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _invalid(f"{field_name} must be canonical non-blank text")
    return value


def _persisted_decimal(value: Decimal) -> Decimal:
    return value.quantize(_PERSISTED_DECIMAL_QUANTUM)


def _utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY",
    "AcquiredQualityCompounderMarketSeries",
    "QualityCompounderDailyMarketRecord",
    "QualityCompounderMarketClient",
    "QualityCompounderMarketError",
    "acquire_quality_compounder_market_series",
    "persist_quality_compounder_market_series",
]
