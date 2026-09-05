"""Reviewed, content-addressed provisioning for scheduled US equities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
    MarketCalendar,
)
from lib_common.hashing import canonical_json_bytes

_SCHEMA = "vynmatrix.equity-catalogue-import.v1"
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA256_LENGTH = 64
_SUPPORTED_EXCHANGES = frozenset({"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA"})
EQUITY_CATALOGUE_MAX_ARTIFACT_BYTES = _MAX_ARTIFACT_BYTES


class EquityCatalogueImportError(RuntimeError):
    """A reviewed catalogue artifact cannot be applied without ambiguity."""


def _invalid(message: str) -> NoReturn:
    raise EquityCatalogueImportError(message)


@dataclass(frozen=True, slots=True)
class ReviewedEquityInstrument:
    """One exact equity and its persisted IBKR contract identity."""

    canonical: str
    exchange: str
    settlement_currency: str
    tick_size: Decimal
    lot_size: Decimal
    calendar_code: str
    broker_code: str
    broker_symbol: str
    broker_instrument_id: str
    broker_instrument_type: str


@dataclass(frozen=True, slots=True)
class ReviewedEquityCatalogueArtifact:
    """Validated immutable input for one catalogue provisioning transaction."""

    content_sha256: str
    reviewer: str
    reviewed_at: datetime
    source_reference: str
    instruments: tuple[ReviewedEquityInstrument, ...]


@dataclass(frozen=True, slots=True)
class EquityCatalogueImportResult:
    """Deterministic dry-run or apply diagnostics."""

    content_sha256: str
    dry_run: bool
    instruments_created: tuple[str, ...]
    instruments_completed: tuple[str, ...]
    broker_mappings_created: tuple[str, ...]
    exact_replays: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PlannedInstrument:
    reviewed: ReviewedEquityInstrument
    existing: Instrument | None
    complete_fields: tuple[str, ...]
    create_mapping: bool


def load_reviewed_equity_catalogue_artifact(
    content: bytes,
    *,
    expected_sha256: str,
) -> ReviewedEquityCatalogueArtifact:
    """Validate canonical reviewed bytes against one operator-pinned digest."""

    digest = _sha256(expected_sha256, field_name="expected catalogue SHA-256")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > _MAX_ARTIFACT_BYTES
        or hashlib.sha256(content).hexdigest() != digest
    ):
        _invalid("reviewed catalogue bytes differ from the expected SHA-256")
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "reviewed catalogue artifact must be valid UTF-8 JSON"
        raise EquityCatalogueImportError(message) from exc
    root = _mapping(decoded, field_name="reviewed catalogue artifact")
    if canonical_json_bytes(root) != content:
        _invalid("reviewed catalogue artifact is not canonical JSON")
    expected_fields = {
        "instruments",
        "reviewed_at",
        "reviewer",
        "schema",
        "source_reference",
    }
    if set(root) != expected_fields or root.get("schema") != _SCHEMA:
        _invalid("reviewed catalogue artifact schema or fields are incompatible")

    rows = tuple(
        _parse_instrument(raw, index=index)
        for index, raw in enumerate(_sequence(root.get("instruments"), field_name="instruments"))
    )
    if not rows:
        _invalid("reviewed catalogue artifact contains no instruments")
    canonical_symbols = tuple(item.canonical for item in rows)
    conids = tuple(item.broker_instrument_id for item in rows)
    if canonical_symbols != tuple(sorted(canonical_symbols)):
        _invalid("reviewed catalogue instruments must be sorted by canonical symbol")
    if len(set(canonical_symbols)) != len(rows):
        _invalid("reviewed catalogue contains duplicate canonical symbols")
    if len(set(conids)) != len(rows):
        _invalid("reviewed catalogue contains duplicate IBKR conIds")
    return ReviewedEquityCatalogueArtifact(
        content_sha256=digest,
        reviewer=_text(root.get("reviewer"), field_name="reviewer"),
        reviewed_at=_timestamp(root.get("reviewed_at"), field_name="reviewed_at"),
        source_reference=_text(root.get("source_reference"), field_name="source_reference"),
        instruments=rows,
    )


def apply_reviewed_equity_catalogue(
    session: Session,
    artifact: ReviewedEquityCatalogueArtifact,
    *,
    dry_run: bool,
) -> EquityCatalogueImportResult:
    """Plan or atomically apply additive exact catalogue provisioning."""

    if not isinstance(artifact, ReviewedEquityCatalogueArtifact):
        _invalid("catalogue import requires a validated reviewed artifact")
    if not isinstance(dry_run, bool):
        _invalid("catalogue import dry_run must be boolean")
    authority = artifact.instruments[0]
    calendar = session.scalar(
        select(MarketCalendar)
        .where(MarketCalendar.code == authority.calendar_code)
        .with_for_update()
    )
    if calendar is None or str(calendar.source_kind) != "exchange":
        _invalid("catalogue import requires one existing official XNYS calendar")
    broker = session.scalar(select(Broker).where(Broker.code == authority.broker_code))
    if broker is None:
        _invalid("catalogue import requires the seeded IBKR broker")

    plan = tuple(
        _plan_instrument(
            session,
            reviewed=reviewed,
            calendar=calendar,
            broker=broker,
        )
        for reviewed in artifact.instruments
    )
    result = _result(artifact, plan=plan, dry_run=dry_run)
    if dry_run:
        return result
    for item in plan:
        instrument = item.existing
        if instrument is None:
            instrument = Instrument(
                asset_class="equity",
                canonical=item.reviewed.canonical,
                exchange=item.reviewed.exchange,
                settlement_currency=item.reviewed.settlement_currency,
                tick_size=item.reviewed.tick_size,
                lot_size=item.reviewed.lot_size,
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=int(calendar.calendar_id),
            )
            session.add(instrument)
            session.flush()
        else:
            _complete_instrument(
                instrument,
                reviewed=item.reviewed,
                calendar_id=int(calendar.calendar_id),
                fields=item.complete_fields,
            )
        if item.create_mapping:
            session.add(
                InstrumentBrokerSymbol(
                    instr_id=int(instrument.instr_id),
                    broker_id=int(broker.broker_id),
                    broker_symbol=item.reviewed.broker_symbol,
                    broker_instrument_id=item.reviewed.broker_instrument_id,
                    broker_instrument_type=item.reviewed.broker_instrument_type,
                )
            )
    session.flush()
    return result


def _parse_instrument(value: object, *, index: int) -> ReviewedEquityInstrument:
    row = _mapping(value, field_name=f"instruments[{index}]")
    expected_fields = {
        "asset_class",
        "broker_code",
        "broker_instrument_id",
        "broker_instrument_type",
        "broker_symbol",
        "calendar_code",
        "canonical",
        "exchange",
        "is_tradable",
        "lot_size",
        "market_session_policy",
        "settlement_currency",
        "tick_size",
    }
    if set(row) != expected_fields:
        _invalid(f"instruments[{index}] fields are incompatible")
    canonical = _text(row.get("canonical"), field_name=f"instruments[{index}].canonical")
    if canonical != canonical.upper() or any(character.isspace() for character in canonical):
        _invalid(f"instruments[{index}].canonical must be uppercase without whitespace")
    exchange = _text(row.get("exchange"), field_name=f"instruments[{index}].exchange")
    if exchange not in _SUPPORTED_EXCHANGES:
        _invalid(f"instruments[{index}].exchange is not a supported US equity exchange")
    fixed_values: Mapping[str, object] = {
        "asset_class": "equity",
        "broker_code": "ibkr",
        "broker_instrument_type": "STK",
        "calendar_code": "XNYS",
        "is_tradable": True,
        "market_session_policy": "scheduled",
        "settlement_currency": "USD",
    }
    if any(row.get(field) != expected for field, expected in fixed_values.items()):
        _invalid(f"instruments[{index}] fixed equity authority fields are incompatible")
    conid = _positive_integer_text(
        row.get("broker_instrument_id"),
        field_name=f"instruments[{index}].broker_instrument_id",
    )
    lot_size = _positive_decimal(
        row.get("lot_size"),
        field_name=f"instruments[{index}].lot_size",
    )
    if lot_size != lot_size.to_integral_value():
        _invalid(f"instruments[{index}].lot_size must be a positive whole-share quantity")
    return ReviewedEquityInstrument(
        canonical=canonical,
        exchange=exchange,
        settlement_currency="USD",
        tick_size=_positive_decimal(
            row.get("tick_size"),
            field_name=f"instruments[{index}].tick_size",
        ),
        lot_size=lot_size,
        calendar_code="XNYS",
        broker_code="ibkr",
        broker_symbol=_text(
            row.get("broker_symbol"),
            field_name=f"instruments[{index}].broker_symbol",
        ),
        broker_instrument_id=conid,
        broker_instrument_type="STK",
    )


def _plan_instrument(
    session: Session,
    *,
    reviewed: ReviewedEquityInstrument,
    calendar: MarketCalendar,
    broker: Broker,
) -> _PlannedInstrument:
    instruments = tuple(
        session.scalars(select(Instrument).where(Instrument.canonical == reviewed.canonical))
    )
    if len(instruments) > 1:
        _invalid(f"canonical instrument {reviewed.canonical} is ambiguous")
    instrument = instruments[0] if instruments else None
    complete_fields: list[str] = []
    if instrument is not None:
        _require_existing_instrument(
            instrument,
            reviewed=reviewed,
            calendar_id=int(calendar.calendar_id),
            complete_fields=complete_fields,
        )
    mapping = (
        session.get(
            InstrumentBrokerSymbol,
            (int(instrument.instr_id), int(broker.broker_id)),
        )
        if instrument is not None
        else None
    )
    if mapping is not None:
        actual = (
            str(mapping.broker_symbol),
            str(mapping.broker_instrument_id or ""),
            str(mapping.broker_instrument_type or ""),
        )
        expected = (
            reviewed.broker_symbol,
            reviewed.broker_instrument_id,
            reviewed.broker_instrument_type,
        )
        if actual != expected:
            _invalid(f"existing IBKR mapping differs for {reviewed.canonical}")
    collisions = tuple(
        session.scalars(
            select(InstrumentBrokerSymbol).where(
                InstrumentBrokerSymbol.broker_id == int(broker.broker_id),
                InstrumentBrokerSymbol.broker_instrument_id == reviewed.broker_instrument_id,
            )
        )
    )
    if len(collisions) > 1 or (
        collisions
        and (instrument is None or int(collisions[0].instr_id) != int(instrument.instr_id))
    ):
        _invalid(f"IBKR conId {reviewed.broker_instrument_id} belongs to another instrument")
    return _PlannedInstrument(
        reviewed=reviewed,
        existing=instrument,
        complete_fields=tuple(sorted(complete_fields)),
        create_mapping=mapping is None,
    )


def _require_existing_instrument(
    instrument: Instrument,
    *,
    reviewed: ReviewedEquityInstrument,
    calendar_id: int,
    complete_fields: list[str],
) -> None:
    required = {
        "asset_class": "equity",
        "canonical": reviewed.canonical,
        "is_tradable": True,
        "market_session_policy": "scheduled",
        "settlement_currency": reviewed.settlement_currency,
    }
    mismatches = [
        field for field, expected in required.items() if getattr(instrument, field) != expected
    ]
    nullable = {
        "exchange": reviewed.exchange,
        "lot_size": reviewed.lot_size,
        "market_calendar_id": calendar_id,
        "tick_size": reviewed.tick_size,
    }
    for field, expected in nullable.items():
        actual = getattr(instrument, field)
        if actual is None:
            complete_fields.append(field)
        elif actual != expected:
            mismatches.append(field)
    if mismatches:
        fields = ", ".join(sorted(mismatches))
        _invalid(f"existing equity {reviewed.canonical} differs in reviewed fields: {fields}")


def _complete_instrument(
    instrument: Instrument,
    *,
    reviewed: ReviewedEquityInstrument,
    calendar_id: int,
    fields: tuple[str, ...],
) -> None:
    values: Mapping[str, object] = {
        "exchange": reviewed.exchange,
        "lot_size": reviewed.lot_size,
        "market_calendar_id": calendar_id,
        "tick_size": reviewed.tick_size,
    }
    for field in fields:
        setattr(instrument, field, values[field])


def _result(
    artifact: ReviewedEquityCatalogueArtifact,
    *,
    plan: tuple[_PlannedInstrument, ...],
    dry_run: bool,
) -> EquityCatalogueImportResult:
    return EquityCatalogueImportResult(
        content_sha256=artifact.content_sha256,
        dry_run=dry_run,
        instruments_created=tuple(
            item.reviewed.canonical for item in plan if item.existing is None
        ),
        instruments_completed=tuple(
            item.reviewed.canonical
            for item in plan
            if item.existing is not None and item.complete_fields
        ),
        broker_mappings_created=tuple(
            item.reviewed.canonical for item in plan if item.create_mapping
        ),
        exact_replays=tuple(
            item.reviewed.canonical
            for item in plan
            if item.existing is not None and not item.complete_fields and not item.create_mapping
        ),
    )


def _mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        _invalid(f"{field_name} must be an object with string keys")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _invalid(f"{field_name} must be a JSON array")
    return value


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be canonical non-empty text")
    return value


def _positive_integer_text(value: object, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    if not text.isascii() or not text.isdecimal() or text.startswith("0") or int(text) <= 0:
        _invalid(f"{field_name} must be a positive integer string")
    return text


def _positive_decimal(value: object, *, field_name: str) -> Decimal:
    if not isinstance(value, str):
        _invalid(f"{field_name} must be a reviewed decimal string")
    text = _text(value, field_name=field_name)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        message = f"{field_name} must be a positive finite decimal string"
        raise EquityCatalogueImportError(message) from exc
    if not parsed.is_finite() or parsed <= 0:
        _invalid(f"{field_name} must be a positive finite decimal string")
    return parsed


def _timestamp(value: object, *, field_name: str) -> datetime:
    text = _text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        message = f"{field_name} must be an ISO-8601 timestamp"
        raise EquityCatalogueImportError(message) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != text:
        _invalid(f"{field_name} must be a canonical timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(UTC)


def _sha256(value: object, *, field_name: str) -> str:
    digest = _text(value, field_name=field_name)
    if len(digest) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


__all__ = [
    "EQUITY_CATALOGUE_MAX_ARTIFACT_BYTES",
    "EquityCatalogueImportError",
    "EquityCatalogueImportResult",
    "ReviewedEquityCatalogueArtifact",
    "ReviewedEquityInstrument",
    "apply_reviewed_equity_catalogue",
    "load_reviewed_equity_catalogue_artifact",
]
