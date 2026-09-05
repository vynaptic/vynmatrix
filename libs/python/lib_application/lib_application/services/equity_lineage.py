"""Stable semantic identity and cutoff-safe authority for equity observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
)
from lib_common.hashing import canonical_json_hash
from lib_strategy.data_authority import ProviderAuthorityPolicy

OWNER_SCOPED_DELAYED_BBO_CONTRACT = "owner-scoped-delayed-bbo-v1"
OWNER_SCOPED_DELAYED_BBO_EXECUTION_AUTHORITY = (
    "personal-use paper execution only; never live authority"
)
_DELAYED_BBO_FUTURE_SKEW = timedelta(seconds=5)
_SHA256_LENGTH = 64
_SQLITE_DECIMAL_TOLERANCE = Decimal("0.000000000001")


class EquityObservationAuthorityError(RuntimeError):
    """An observation is missing, stale, revised, or outside provider authority."""


@dataclass(frozen=True, slots=True)
class OwnerScopedDelayedBBOEvidence:
    """Validated provider-neutral quote evidence for one owner's paper account."""

    provider: str
    source_symbol: str
    exchange: str
    currency: str
    last_trade_price: Decimal
    last_trade_at: datetime
    last_trade_size: int | None
    bid_price: Decimal
    bid_size: int
    bid_at: datetime
    ask_price: Decimal
    ask_size: int
    ask_at: datetime
    snapshot_at: datetime
    available_at: datetime
    lineage_id: str
    observation_id: str
    content_sha256: str
    source_content_sha256: str
    raw_response_sha256: str


def canonical_equity_quote_decimal(value: Decimal) -> str:
    """Return a stable finite decimal identity for normalized quote evidence."""

    if not value.is_finite():
        _invalid("delayed BBO content identity requires a finite decimal")
    return format(value.normalize(), "f")


def owner_scoped_delayed_bbo_sha256(
    *,
    source_symbol: str,
    exchange: str,
    currency: str,
    last_trade_price: Decimal,
    last_trade_at: datetime,
    last_trade_size: int | None,
    bid_price: Decimal,
    bid_size: int,
    bid_at: datetime,
    ask_price: Decimal,
    ask_size: int,
    ask_at: datetime,
    snapshot_at: datetime,
    source_content_sha256: str,
    raw_response_sha256: str,
) -> str:
    """Hash normalized delayed-BBO content and its exact source/raw lineage."""

    return canonical_json_hash(
        {
            "schema": OWNER_SCOPED_DELAYED_BBO_CONTRACT,
            "source_symbol": _required_text(source_symbol, field_name="source symbol"),
            "exchange": _required_text(exchange, field_name="exchange"),
            "currency": _required_text(currency, field_name="currency"),
            "last_trade_price": canonical_equity_quote_decimal(last_trade_price),
            "last_trade_at": _aware_cutoff(last_trade_at).isoformat(),
            "last_trade_size": last_trade_size,
            "bid_price": canonical_equity_quote_decimal(bid_price),
            "bid_size": bid_size,
            "bid_at": _aware_cutoff(bid_at).isoformat(),
            "ask_price": canonical_equity_quote_decimal(ask_price),
            "ask_size": ask_size,
            "ask_at": _aware_cutoff(ask_at).isoformat(),
            "snapshot_at": _aware_cutoff(snapshot_at).isoformat(),
            "source_content_sha256": _required_sha256(
                source_content_sha256,
                field_name="source content",
            ),
            "raw_response_sha256": _required_sha256(
                raw_response_sha256,
                field_name="raw response",
            ),
        }
    )


def load_owner_scoped_delayed_bbo(
    session: Session,
    *,
    instrument_id: int,
    entitlement_owner_user_id: str,
    observed_at: datetime,
    max_staleness: timedelta,
) -> OwnerScopedDelayedBBOEvidence | None:
    """Load and validate the freshest normalized delayed BBO for one owner.

    Provider clients and vendor response schemas are intentionally absent from
    this read boundary. An ingestion adapter must publish the normalized quote
    contract while retaining its source-specific content digest in lineage.
    """

    if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
        _invalid("delayed BBO lookup requires a positive instrument ID")
    owner = _required_text(entitlement_owner_user_id, field_name="entitlement owner")
    if max_staleness <= timedelta(0):
        _invalid("delayed BBO max_staleness must be positive")
    cutoff = _aware_cutoff(observed_at)
    oldest_event = cutoff - max_staleness
    newest_event = cutoff + _DELAYED_BBO_FUTURE_SKEW
    contract_join = and_(
        EquityObservationValue.observation_id == EquityObservation.observation_id,
        EquityObservationValue.field_name == "quote_contract",
        EquityObservationValue.ordinal == 0,
        EquityObservationValue.value_type == "text",
        EquityObservationValue.text_value == OWNER_SCOPED_DELAYED_BBO_CONTRACT,
    )
    row = session.execute(
        select(EquityObservation, EquitySourceLineage)
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .join(EquityObservationValue, contract_join)
        .where(
            EquityObservation.instr_id == instrument_id,
            EquityObservation.observation_kind == "price",
            EquityObservation.disposition == "observed",
            EquityObservation.available_at.is_not(None),
            EquityObservation.available_at <= _stored_for_session(session, cutoff),
            EquityObservation.event_at >= _stored_for_session(session, oldest_event),
            EquityObservation.event_at <= _stored_for_session(session, newest_event),
            EquitySourceLineage.entitlement_owner_user_id == owner,
        )
        .order_by(
            desc(EquityObservation.event_at),
            desc(EquityObservation.source_record_identity),
            desc(EquityObservation.revision),
            desc(EquityObservation.available_at),
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    observation, lineage = row
    values = _load_normalized_values(session, observation_id=str(observation.observation_id))
    return _validate_owner_scoped_delayed_bbo(
        observation=observation,
        lineage=lineage,
        values=values,
        expected_owner_user_id=owner,
        observed_at=cutoff,
        max_staleness=max_staleness,
        decimal_tolerance=(
            _SQLITE_DECIMAL_TOLERANCE
            if session.get_bind().dialect.name == "sqlite"
            else Decimal("0")
        ),
    )


def _validate_owner_scoped_delayed_bbo(
    *,
    observation: EquityObservation,
    lineage: EquitySourceLineage,
    values: dict[str, EquityObservationValue],
    expected_owner_user_id: str,
    observed_at: datetime,
    max_staleness: timedelta,
    decimal_tolerance: Decimal,
) -> OwnerScopedDelayedBBOEvidence:
    semantics = lineage.timestamp_semantics
    if (
        str(lineage.entitlement_owner_user_id or "") != expected_owner_user_id
        or not isinstance(semantics, dict)
        or semantics.get("execution_authority") != OWNER_SCOPED_DELAYED_BBO_EXECUTION_AUTHORITY
        or str(lineage.missing_data_policy) != "fail-closed"
    ):
        _invalid("delayed BBO lineage is not valid owner-scoped paper-only authority")
    provider = _required_text(lineage.provider, field_name="provider")
    source_identity = _required_text(lineage.source_identity, field_name="source identity")
    source_record_identity = str(observation.source_record_identity)
    if not source_record_identity.startswith(f"{source_identity}:snapshot:"):
        _invalid("delayed BBO record identity differs from its source lineage")
    if _text_value(values, "quote_contract") != OWNER_SCOPED_DELAYED_BBO_CONTRACT:
        _invalid("delayed BBO normalized contract is unavailable")

    source_symbol = _text_value(values, "symbol")
    exchange = _text_value(values, "exchange")
    currency = _text_value(values, "currency")
    source_content_sha256 = _required_sha256(
        _text_value(values, "source_content_sha256"),
        field_name="source content",
    )
    raw_response_sha256 = _required_sha256(
        _text_value(values, "raw_response_sha256"),
        field_name="raw response",
    )
    if source_content_sha256 != str(lineage.content_sha256):
        _invalid("delayed BBO source-content lineage verification failed")

    last_trade_price = _decimal_value(
        values,
        "last_trade_price",
        tolerance=decimal_tolerance,
    )
    last_trade_at = _timestamp_value(values, "last_trade_at")
    last_trade_size = _optional_integer_value(values, "last_trade_size")
    bid_price = _decimal_value(values, "bid_price", tolerance=decimal_tolerance)
    bid_size = _required_positive_integer_value(values, "bid_size")
    bid_at = _timestamp_value(values, "bid_at")
    ask_price = _decimal_value(values, "ask_price", tolerance=decimal_tolerance)
    ask_size = _required_positive_integer_value(values, "ask_size")
    ask_at = _timestamp_value(values, "ask_at")
    snapshot_at = _timestamp_value(values, "snapshot_at")
    if bid_price > ask_price:
        _invalid("delayed BBO is crossed or invalid")

    available_at = _utc(observation.available_at) if observation.available_at is not None else None
    if available_at is None or available_at > observed_at:
        _invalid("delayed BBO has no cutoff-safe availability timestamp")
    if _utc(observation.event_at) != last_trade_at:
        _invalid("delayed BBO event timestamp differs from normalized last-trade time")
    for event_name, event_at in (
        ("last trade", last_trade_at),
        ("bid", bid_at),
        ("ask", ask_at),
        ("snapshot", snapshot_at),
    ):
        if event_at > observed_at + _DELAYED_BBO_FUTURE_SKEW:
            _invalid(f"delayed BBO {event_name} timestamp is future-dated")
        if event_at > available_at + _DELAYED_BBO_FUTURE_SKEW:
            _invalid(f"delayed BBO {event_name} timestamp exceeds source availability")
    for event_name, event_at in (
        ("last trade", last_trade_at),
        ("bid", bid_at),
        ("ask", ask_at),
    ):
        if observed_at - event_at > max_staleness:
            _invalid(f"delayed BBO {event_name} became stale during execution lookup")
    for event_name, event_at in (("bid", bid_at), ("ask", ask_at)):
        if event_at > snapshot_at + _DELAYED_BBO_FUTURE_SKEW:
            _invalid(f"delayed BBO {event_name} timestamp exceeds its snapshot")

    digest = owner_scoped_delayed_bbo_sha256(
        source_symbol=source_symbol,
        exchange=exchange,
        currency=currency,
        last_trade_price=last_trade_price,
        last_trade_at=last_trade_at,
        last_trade_size=last_trade_size,
        bid_price=bid_price,
        bid_size=bid_size,
        bid_at=bid_at,
        ask_price=ask_price,
        ask_size=ask_size,
        ask_at=ask_at,
        snapshot_at=snapshot_at,
        source_content_sha256=source_content_sha256,
        raw_response_sha256=raw_response_sha256,
    )
    if digest != str(observation.content_sha256):
        _invalid("delayed BBO normalized content digest verification failed")
    return OwnerScopedDelayedBBOEvidence(
        provider=provider,
        source_symbol=source_symbol,
        exchange=exchange,
        currency=currency,
        last_trade_price=last_trade_price,
        last_trade_at=last_trade_at,
        last_trade_size=last_trade_size,
        bid_price=bid_price,
        bid_size=bid_size,
        bid_at=bid_at,
        ask_price=ask_price,
        ask_size=ask_size,
        ask_at=ask_at,
        snapshot_at=snapshot_at,
        available_at=available_at,
        lineage_id=str(lineage.lineage_id),
        observation_id=str(observation.observation_id),
        content_sha256=digest,
        source_content_sha256=source_content_sha256,
        raw_response_sha256=raw_response_sha256,
    )


def _load_normalized_values(
    session: Session,
    *,
    observation_id: str,
) -> dict[str, EquityObservationValue]:
    rows = tuple(
        session.scalars(
            select(EquityObservationValue)
            .where(EquityObservationValue.observation_id == observation_id)
            .order_by(EquityObservationValue.field_name)
        )
    )
    values: dict[str, EquityObservationValue] = {}
    for row in rows:
        field_name = str(row.field_name)
        if field_name in values or int(row.ordinal) != 0:
            _invalid("delayed BBO has duplicate or non-canonical typed values")
        values[field_name] = row
    return values


def _typed_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
    value_type: str,
) -> EquityObservationValue:
    row = values.get(field_name)
    if row is None or str(row.value_type) != value_type:
        _invalid(f"delayed BBO value {field_name!r} is unavailable or mistyped")
    return row


def _text_value(values: dict[str, EquityObservationValue], field_name: str) -> str:
    value = _typed_value(values, field_name, "text").text_value
    return _required_text(value, field_name=field_name)


def _decimal_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
    *,
    tolerance: Decimal,
) -> Decimal:
    value = _typed_value(values, field_name, "decimal").decimal_value
    if value is None:
        _invalid(f"delayed BBO decimal value {field_name!r} is invalid")
    stored = Decimal(str(value))
    canonical_text = _text_value(values, f"{field_name}_canonical")
    try:
        canonical = Decimal(canonical_text)
    except InvalidOperation as exc:
        message = f"delayed BBO canonical decimal {field_name!r} is invalid"
        raise EquityObservationAuthorityError(message) from exc
    if (
        not stored.is_finite()
        or not canonical.is_finite()
        or canonical <= 0
        or canonical_equity_quote_decimal(canonical) != canonical_text
        or abs(stored - canonical) > tolerance
    ):
        _invalid(f"delayed BBO decimal value {field_name!r} is not positive and canonical")
    return canonical


def _timestamp_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
) -> datetime:
    value = _typed_value(values, field_name, "timestamp").timestamp_value
    if value is None:
        _invalid(f"delayed BBO timestamp value {field_name!r} is invalid")
    return _utc(value)


def _optional_integer_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
) -> int | None:
    row = values.get(field_name)
    if row is None:
        return None
    if str(row.value_type) != "integer" or row.integer_value is None or row.integer_value < 0:
        _invalid(f"delayed BBO integer value {field_name!r} is invalid")
    return int(row.integer_value)


def _required_positive_integer_value(
    values: dict[str, EquityObservationValue],
    field_name: str,
) -> int:
    value = _optional_integer_value(values, field_name)
    if value is None or value <= 0:
        _invalid(f"delayed BBO integer value {field_name!r} is not positive")
    return value


def equity_observation_semantic_payload(
    observation: EquityObservation,
    lineage: EquitySourceLineage,
) -> dict[str, Any]:
    """Return stable content semantics, excluding retrieval and DB surrogate identity."""

    return {
        "schema": "equity-observation-semantic-v1",
        "provider": str(lineage.provider),
        "product": str(lineage.product),
        "endpoint": str(lineage.endpoint),
        "dataset_version": str(lineage.dataset_version),
        "tool_version": str(lineage.tool_version),
        "source_identity": str(lineage.source_identity),
        "source_revision": str(lineage.source_revision),
        "timestamp_semantics": lineage.timestamp_semantics,
        "adjustment_policy": str(lineage.adjustment_policy),
        "entitlement_scope": str(lineage.entitlement_scope),
        "entitlement_owner_user_id": (
            str(lineage.entitlement_owner_user_id)
            if lineage.entitlement_owner_user_id is not None
            else None
        ),
        "missing_data_policy": str(lineage.missing_data_policy),
        "source_content_sha256": str(lineage.content_sha256),
        "instrument_id": (int(observation.instr_id) if observation.instr_id is not None else None),
        "observation_kind": str(observation.observation_kind),
        "source_record_identity": str(observation.source_record_identity),
        "event_at": _utc(observation.event_at).isoformat(),
        "available_at": (
            _utc(observation.available_at).isoformat()
            if observation.available_at is not None
            else None
        ),
        "revision": int(observation.revision),
        "disposition": str(observation.disposition),
        "content_sha256": str(observation.content_sha256),
    }


def equity_observation_semantic_sha256(
    observation: EquityObservation,
    lineage: EquitySourceLineage,
) -> str:
    """Hash stable observation semantics across equivalent retrievals/reimports."""

    return canonical_json_hash(equity_observation_semantic_payload(observation, lineage))


def equity_observation_value_semantic_payload(
    value: EquityObservationValue,
) -> dict[str, Any]:
    """Return the exact JSON-safe semantics of one normalized observation field."""

    raw: object
    if str(value.value_type) == "decimal":
        raw = str(value.decimal_value)
    elif str(value.value_type) == "integer":
        raw = value.integer_value
    elif str(value.value_type) == "text":
        raw = value.text_value
    elif str(value.value_type) == "boolean":
        raw = value.boolean_value
    elif str(value.value_type) == "date":
        raw = value.date_value.isoformat() if value.date_value is not None else None
    elif str(value.value_type) == "timestamp":
        raw = _utc(value.timestamp_value).isoformat() if value.timestamp_value else None
    else:
        _invalid("equity observation value_type is unsupported")
    return {
        "field_name": str(value.field_name),
        "ordinal": int(value.ordinal),
        "value_type": str(value.value_type),
        "value": raw,
        "unit": str(value.unit) if value.unit is not None else None,
        "context_identity": (
            str(value.context_identity) if value.context_identity is not None else None
        ),
        "fiscal_year": int(value.fiscal_year) if value.fiscal_year is not None else None,
        "fiscal_period": (str(value.fiscal_period) if value.fiscal_period is not None else None),
        "period_start": value.period_start.isoformat() if value.period_start else None,
        "period_end": value.period_end.isoformat() if value.period_end else None,
    }


def equity_observation_with_values_sha256(
    observation: EquityObservation,
    lineage: EquitySourceLineage,
    values: dict[str, EquityObservationValue],
) -> str:
    """Hash one source observation and its complete normalized typed field set."""

    if set(values) != {str(value.field_name) for value in values.values()}:
        _invalid("equity observation normalized values are keyed inconsistently")
    if any(int(value.ordinal) != 0 for value in values.values()):
        _invalid("equity observation authority does not permit repeated typed fields")
    return canonical_json_hash(
        {
            "schema": "equity-observation-with-normalized-values-v1",
            "observation": equity_observation_semantic_payload(observation, lineage),
            "values": [
                equity_observation_value_semantic_payload(values[field_name])
                for field_name in sorted(values)
            ],
        }
    )


def validate_equity_observation_authority(
    session: Session,
    *,
    observation_id: str | None,
    expected_kind: str,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
    expected_instrument_id: int | None,
) -> tuple[EquityObservation, EquitySourceLineage]:
    """Load exact lineage and prove it was the latest authorized cutoff revision."""

    if observation_id is None:
        _invalid(f"forward synchronized panel requires {expected_kind} observation lineage")
    row = session.execute(
        select(EquityObservation, EquitySourceLineage)
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .where(EquityObservation.observation_id == observation_id)
    ).one_or_none()
    if row is None:
        _invalid(f"{expected_kind} observation lineage is unavailable")
    observation, lineage = row
    if (
        str(observation.observation_kind) != expected_kind
        or str(observation.disposition) != "observed"
        or observation.available_at is None
        or _utc(observation.available_at) > _aware_cutoff(cutoff)
    ):
        _invalid(f"{expected_kind} observation is not cutoff-safe observed content")
    actual_instrument_id = int(observation.instr_id) if observation.instr_id is not None else None
    if actual_instrument_id != expected_instrument_id:
        _invalid(f"{expected_kind} observation instrument identity is inconsistent")
    try:
        provider_authority_policy.require_authorized(
            provider=str(lineage.provider),
            entitlement_scope=str(lineage.entitlement_scope),
            entitlement_owner_user_id=(
                str(lineage.entitlement_owner_user_id)
                if lineage.entitlement_owner_user_id is not None
                else None
            ),
        )
    except ValueError as exc:
        message = f"{expected_kind} observation is outside provider authority"
        raise EquityObservationAuthorityError(message) from exc
    _validate_supersession_authority(
        session,
        observation=observation,
        lineage=lineage,
        cutoff=cutoff,
        provider_authority_policy=provider_authority_policy,
    )
    later_revisions = session.execute(
        select(EquityObservation, EquitySourceLineage)
        .join(
            EquitySourceLineage,
            EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
        )
        .where(
            EquityObservation.observation_kind == observation.observation_kind,
            EquityObservation.source_record_identity == observation.source_record_identity,
            EquityObservation.instr_id.is_(None)
            if observation.instr_id is None
            else EquityObservation.instr_id == observation.instr_id,
            EquityObservation.revision > observation.revision,
            EquityObservation.available_at.is_not(None),
            EquityObservation.available_at <= _stored(cutoff),
            EquitySourceLineage.provider == lineage.provider,
            EquitySourceLineage.product == lineage.product,
            EquitySourceLineage.endpoint == lineage.endpoint,
            EquitySourceLineage.source_identity == lineage.source_identity,
        )
        .order_by(EquityObservation.revision.desc())
    ).all()
    for _, later_lineage in later_revisions:
        try:
            provider_authority_policy.require_authorized(
                provider=str(later_lineage.provider),
                entitlement_scope=str(later_lineage.entitlement_scope),
                entitlement_owner_user_id=(
                    str(later_lineage.entitlement_owner_user_id)
                    if later_lineage.entitlement_owner_user_id is not None
                    else None
                ),
            )
        except ValueError:
            continue
        _invalid(f"{expected_kind} observation was superseded before the cutoff")
    return observation, lineage


def _validate_supersession_authority(
    session: Session,
    *,
    observation: EquityObservation,
    lineage: EquitySourceLineage,
    cutoff: datetime,
    provider_authority_policy: ProviderAuthorityPolicy,
) -> None:
    """Validate an explicit correction chain and reject cutoff-safe descendants."""

    selected_id = str(observation.observation_id)
    visited = {selected_id}
    child = observation
    child_lineage = lineage
    while child.supersedes_observation_id is not None:
        parent_id = str(child.supersedes_observation_id)
        if parent_id in visited:
            _invalid(f"{observation.observation_kind} observation correction chain cycles")
        visited.add(parent_id)
        parent_row = session.execute(
            select(EquityObservation, EquitySourceLineage)
            .join(
                EquitySourceLineage,
                EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
            )
            .where(EquityObservation.observation_id == parent_id)
        ).one_or_none()
        if parent_row is None:
            _invalid(f"{observation.observation_kind} correction parent is unavailable")
        parent, parent_lineage = parent_row
        _validate_supersession_edge(
            parent=parent,
            parent_lineage=parent_lineage,
            child=child,
            child_lineage=child_lineage,
        )
        child = parent
        child_lineage = parent_lineage

    cutoff_utc = _aware_cutoff(cutoff)
    frontier = [(observation, lineage)]
    descendant_ids = {selected_id}
    while frontier:
        next_frontier: list[tuple[EquityObservation, EquitySourceLineage]] = []
        for parent, parent_lineage in frontier:
            descendants = session.execute(
                select(EquityObservation, EquitySourceLineage)
                .join(
                    EquitySourceLineage,
                    EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
                )
                .where(EquityObservation.supersedes_observation_id == str(parent.observation_id))
            ).all()
            cutoff_children: list[EquityObservation] = []
            for candidate, candidate_lineage in descendants:
                try:
                    provider_authority_policy.require_authorized(
                        provider=str(candidate_lineage.provider),
                        entitlement_scope=str(candidate_lineage.entitlement_scope),
                        entitlement_owner_user_id=(
                            str(candidate_lineage.entitlement_owner_user_id)
                            if candidate_lineage.entitlement_owner_user_id is not None
                            else None
                        ),
                    )
                except ValueError:
                    continue
                candidate_id = str(candidate.observation_id)
                if candidate_id in descendant_ids or candidate_id in visited:
                    _invalid(f"{observation.observation_kind} observation correction chain cycles")
                descendant_ids.add(candidate_id)
                _validate_supersession_edge(
                    parent=parent,
                    parent_lineage=parent_lineage,
                    child=candidate,
                    child_lineage=candidate_lineage,
                )
                next_frontier.append((candidate, candidate_lineage))
                if (
                    str(candidate.disposition) == "observed"
                    and candidate.available_at is not None
                    and _utc(candidate.available_at) <= cutoff_utc
                ):
                    cutoff_children.append(candidate)
            if len(cutoff_children) > 1:
                _invalid(f"{observation.observation_kind} observation correction chain forks")
            if cutoff_children:
                _invalid(
                    f"{observation.observation_kind} observation was superseded before the cutoff"
                )
        frontier = next_frontier


def _validate_supersession_edge(
    *,
    parent: EquityObservation,
    parent_lineage: EquitySourceLineage,
    child: EquityObservation,
    child_lineage: EquitySourceLineage,
) -> None:
    """Require one explicit correction link to remain in one authoritative series."""

    parent_instrument = int(parent.instr_id) if parent.instr_id is not None else None
    child_instrument = int(child.instr_id) if child.instr_id is not None else None
    if (
        str(child.observation_kind) != str(parent.observation_kind)
        or child_instrument != parent_instrument
        or str(child.source_record_identity) != str(parent.source_record_identity)
        or str(child_lineage.provider) != str(parent_lineage.provider)
        or str(child_lineage.product) != str(parent_lineage.product)
        or str(child_lineage.endpoint) != str(parent_lineage.endpoint)
        or str(child_lineage.source_identity) != str(parent_lineage.source_identity)
        or child_lineage.entitlement_owner_user_id != parent_lineage.entitlement_owner_user_id
        or int(child.revision) <= int(parent.revision)
    ):
        _invalid(f"{child.observation_kind} observation correction lineage is inconsistent")
    if child.available_at is None or parent.available_at is None:
        _invalid(f"{child.observation_kind} observation correction lacks availability")
    if _utc(child.available_at) < _utc(parent.available_at):
        _invalid(f"{child.observation_kind} observation correction availability regresses")


def _aware_cutoff(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid("equity observation cutoff must be timezone-aware")
    return value.astimezone(UTC)


def _stored(value: datetime) -> datetime:
    return _aware_cutoff(value).replace(tzinfo=None)


def _stored_for_session(session: Session, value: datetime) -> datetime:
    utc_value = _aware_cutoff(value)
    return (
        utc_value.replace(tzinfo=None) if session.get_bind().dialect.name == "sqlite" else utc_value
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"delayed BBO {field_name} must be non-empty text")
    return value.strip()


def _required_sha256(value: object, *, field_name: str) -> str:
    digest = _required_text(value, field_name=field_name)
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _invalid(f"delayed BBO {field_name} digest is invalid")
    return digest


def _invalid(message: str) -> NoReturn:
    raise EquityObservationAuthorityError(message)


__all__ = [
    "OWNER_SCOPED_DELAYED_BBO_CONTRACT",
    "OWNER_SCOPED_DELAYED_BBO_EXECUTION_AUTHORITY",
    "EquityObservationAuthorityError",
    "OwnerScopedDelayedBBOEvidence",
    "canonical_equity_quote_decimal",
    "equity_observation_semantic_payload",
    "equity_observation_semantic_sha256",
    "equity_observation_value_semantic_payload",
    "equity_observation_with_values_sha256",
    "load_owner_scoped_delayed_bbo",
    "owner_scoped_delayed_bbo_sha256",
    "validate_equity_observation_authority",
]
