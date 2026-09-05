"""Canonical database-backed instrument resolution.

All application services resolve external symbol spellings through this module.
The resolver gives canonical symbols precedence, then configured aliases and
broker symbols, and finally performs separator/case-insensitive matching.
Ambiguous normalized matches fail closed instead of routing money or prices to
an arbitrary instrument.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Broker,
    Instrument,
    InstrumentAlias,
    InstrumentBrokerSymbol,
)
from lib_common.asset_classes import normalize_optional_asset_class
from lib_common.logging import get_logger
from lib_data.market_data import normalize_product_symbol

logger = get_logger(__name__)


class InstrumentResolutionError(ValueError):
    """Base error for an unsafe instrument resolution."""


class AmbiguousInstrumentError(InstrumentResolutionError):
    """Raised when one symbol can identify more than one instrument."""


class InstrumentIdentityMismatchError(InstrumentResolutionError):
    """Raised when an explicit instrument ID disagrees with its symbol."""


@dataclass(frozen=True)
class BrokerInstrumentIdentity:
    """One canonical instrument's exact identity at one broker boundary."""

    instrument_id: int
    broker_id: int
    broker_code: str
    broker_symbol: str
    broker_instrument_id: str | None
    broker_instrument_type: str | None
    lot_size: Decimal | None


def resolve_broker_instrument_identity(
    session: Session,
    *,
    instrument_id: int,
    broker_code: str,
) -> BrokerInstrumentIdentity | None:
    """Resolve one exact broker-scoped catalogue mapping.

    The caller has already resolved the canonical instrument. No symbol
    normalization or venue search is performed here: an absent mapping returns
    ``None`` so the adapter capability contract can decide whether to block.
    """
    if isinstance(instrument_id, bool) or instrument_id <= 0:
        msg = "Broker instrument resolution requires a positive instrument_id"
        raise InstrumentResolutionError(msg)
    normalized_broker_code = str(broker_code or "").strip().lower()
    if not normalized_broker_code:
        msg = "Broker instrument resolution requires a broker code"
        raise InstrumentResolutionError(msg)

    row = session.execute(
        select(InstrumentBrokerSymbol, Instrument.lot_size)
        .join(Broker, Broker.broker_id == InstrumentBrokerSymbol.broker_id)
        .join(Instrument, Instrument.instr_id == InstrumentBrokerSymbol.instr_id)
        .where(
            InstrumentBrokerSymbol.instr_id == instrument_id,
            Broker.code == normalized_broker_code,
        )
    ).one_or_none()
    if row is None:
        return None
    mapping, raw_lot_size = row

    broker_symbol = str(mapping.broker_symbol or "").strip()
    if not broker_symbol:
        msg = f"Instrument {instrument_id} has a blank {normalized_broker_code} broker symbol"
        raise InstrumentResolutionError(msg)

    def _optional_exact(value: object, field_name: str) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            msg = f"Instrument {instrument_id} has a blank {normalized_broker_code} {field_name}"
            raise InstrumentResolutionError(msg)
        return normalized

    lot_size = Decimal(str(raw_lot_size)) if raw_lot_size is not None else None
    if lot_size is not None and (not lot_size.is_finite() or lot_size <= 0):
        msg = f"Instrument {instrument_id} has an invalid lot_size"
        raise InstrumentResolutionError(msg)

    return BrokerInstrumentIdentity(
        instrument_id=instrument_id,
        broker_id=int(mapping.broker_id),
        broker_code=normalized_broker_code,
        broker_symbol=broker_symbol,
        broker_instrument_id=_optional_exact(
            mapping.broker_instrument_id,
            "broker_instrument_id",
        ),
        broker_instrument_type=_optional_exact(
            mapping.broker_instrument_type,
            "broker_instrument_type",
        ),
        lot_size=lot_size,
    )


def _instrument_rows(
    session: Session,
    *,
    asset_class: str | None,
) -> list[Instrument]:
    statement = select(Instrument)
    if asset_class:
        statement = statement.where(Instrument.asset_class == asset_class)
    return list(session.execute(statement).scalars())


def _one_instrument(
    candidates: dict[int, Instrument],
    *,
    symbol: str,
    match_kind: str,
) -> Instrument | None:
    if not candidates:
        return None
    if len(candidates) > 1:
        candidate_ids = sorted(candidates)
        msg = (
            f"Ambiguous instrument symbol {symbol!r} via {match_kind}; "
            f"candidate instr_ids={candidate_ids}"
        )
        raise AmbiguousInstrumentError(msg)
    return next(iter(candidates.values()))


def _resolve_symbol(
    session: Session,
    symbol: str,
    *,
    asset_class: str | None,
    broker_id: int | None,
) -> Instrument | None:
    instruments = _instrument_rows(session, asset_class=asset_class)
    by_id = {int(row.instr_id): row for row in instruments}
    if not by_id:
        return None

    # Canonical names are authoritative when an alias happens to overlap one.
    exact_canonical = {instr_id: row for instr_id, row in by_id.items() if row.canonical == symbol}
    match = _one_instrument(exact_canonical, symbol=symbol, match_kind="canonical symbol")
    if match is not None:
        return match

    aliases = list(
        session.execute(
            select(InstrumentAlias).where(InstrumentAlias.instr_id.in_(tuple(by_id)))
        ).scalars()
    )
    exact_alias = {
        int(row.instr_id): by_id[int(row.instr_id)]
        for row in aliases
        if row.alias == symbol and int(row.instr_id) in by_id
    }
    match = _one_instrument(exact_alias, symbol=symbol, match_kind="instrument alias")
    if match is not None:
        return match

    broker_statement = select(InstrumentBrokerSymbol).where(
        InstrumentBrokerSymbol.instr_id.in_(tuple(by_id))
    )
    if broker_id is not None:
        broker_statement = broker_statement.where(InstrumentBrokerSymbol.broker_id == broker_id)
    broker_symbols = list(session.execute(broker_statement).scalars())
    exact_broker = {
        int(row.instr_id): by_id[int(row.instr_id)]
        for row in broker_symbols
        if row.broker_symbol == symbol and int(row.instr_id) in by_id
    }
    match = _one_instrument(exact_broker, symbol=symbol, match_kind="broker symbol")
    if match is not None:
        return match

    normalized = normalize_product_symbol(symbol)
    normalized_candidates: dict[int, Instrument] = {
        instr_id: row
        for instr_id, row in by_id.items()
        if normalize_product_symbol(row.canonical) == normalized
    }
    for alias in aliases:
        instr_id = int(alias.instr_id)
        if instr_id in by_id and normalize_product_symbol(alias.alias) == normalized:
            normalized_candidates[instr_id] = by_id[instr_id]
    for broker_symbol in broker_symbols:
        instr_id = int(broker_symbol.instr_id)
        if (
            instr_id in by_id
            and normalize_product_symbol(broker_symbol.broker_symbol) == normalized
        ):
            normalized_candidates[instr_id] = by_id[instr_id]
    return _one_instrument(
        normalized_candidates,
        symbol=symbol,
        match_kind="normalized canonical/alias/broker symbol",
    )


def resolve_instrument(
    session: Session,
    symbol: str,
    *,
    instrument_id: int | str | None = None,
    asset_class: str | None = None,
    broker_id: int | None = None,
    allow_create: bool = False,
    settlement_currency: str | None = None,
) -> Instrument | None:
    """Resolve one symbol to its canonical ORM instrument.

    ``instrument_id`` is cross-checked against the symbol so an inconsistent
    payload cannot silently persist or execute against the wrong instrument.
    Creation is reserved for explicit catalogue-management callers and requires
    both an asset class and settlement currency.
    """
    normalized_symbol = str(symbol or "").strip()
    if not normalized_symbol:
        msg = "Instrument symbol must be non-empty"
        raise InstrumentResolutionError(msg)
    normalized_asset_class = normalize_optional_asset_class(asset_class)

    resolved = _resolve_symbol(
        session,
        normalized_symbol,
        asset_class=normalized_asset_class,
        broker_id=broker_id,
    )

    if instrument_id is not None:
        try:
            explicit_id = int(instrument_id)
        except (TypeError, ValueError) as exc:
            msg = f"Invalid instrument_id: {instrument_id!r}"
            raise InstrumentIdentityMismatchError(msg) from exc
        explicit = cast(Instrument | None, session.get(Instrument, explicit_id))
        if explicit is None:
            msg = f"Unknown instrument_id: {explicit_id}"
            raise InstrumentIdentityMismatchError(msg)
        if normalized_asset_class and explicit.asset_class != normalized_asset_class:
            msg = (
                f"Instrument {explicit_id} has asset_class={explicit.asset_class!r}, "
                f"not {normalized_asset_class!r}"
            )
            raise InstrumentIdentityMismatchError(msg)
        if resolved is None or int(resolved.instr_id) != explicit_id:
            resolved_id = int(resolved.instr_id) if resolved is not None else None
            msg = (
                f"Instrument identity mismatch for symbol {normalized_symbol!r}: "
                f"payload instr_id={explicit_id}, resolved instr_id={resolved_id}"
            )
            raise InstrumentIdentityMismatchError(msg)
        return explicit

    if resolved is not None:
        return resolved
    if not allow_create:
        return None

    normalized_currency = str(settlement_currency or "").strip().upper()
    if not normalized_asset_class or not normalized_currency:
        msg = (
            f"Creating instrument {normalized_symbol!r} requires asset_class "
            "and settlement_currency"
        )
        raise InstrumentResolutionError(msg)
    created = Instrument(
        canonical=normalized_symbol,
        asset_class=normalized_asset_class,
        settlement_currency=normalized_currency,
    )
    session.add(created)
    session.flush()
    return created


def load_instrument_symbol_map(
    session: Session,
    *,
    asset_class: str | None = None,
    broker_id: int | None = None,
) -> dict[str, int]:
    """Build a normalized symbol map from canonical, alias, and broker tables.

    Keys that resolve to multiple instruments are omitted and logged. This
    prevents a catalogue defect from routing a market-data batch to whichever
    row happened to be returned last.
    """
    instruments = _instrument_rows(
        session,
        asset_class=normalize_optional_asset_class(asset_class),
    )
    by_id = {int(row.instr_id): row for row in instruments}
    keys_by_instrument: dict[int, set[str]] = {
        instr_id: {normalize_product_symbol(row.canonical)} for instr_id, row in by_id.items()
    }
    if by_id:
        aliases = session.execute(
            select(InstrumentAlias).where(InstrumentAlias.instr_id.in_(tuple(by_id)))
        ).scalars()
        for alias in aliases:
            instr_id = int(alias.instr_id)
            if instr_id in keys_by_instrument:
                keys_by_instrument[instr_id].add(normalize_product_symbol(alias.alias))

        broker_statement = select(InstrumentBrokerSymbol).where(
            InstrumentBrokerSymbol.instr_id.in_(tuple(by_id))
        )
        if broker_id is not None:
            broker_statement = broker_statement.where(InstrumentBrokerSymbol.broker_id == broker_id)
        for broker_symbol in session.execute(broker_statement).scalars():
            instr_id = int(broker_symbol.instr_id)
            if instr_id in keys_by_instrument:
                keys_by_instrument[instr_id].add(
                    normalize_product_symbol(broker_symbol.broker_symbol)
                )

    candidates_by_key: dict[str, set[int]] = {}
    for instr_id, keys in keys_by_instrument.items():
        for key in keys:
            candidates_by_key.setdefault(key, set()).add(instr_id)

    result: dict[str, int] = {}
    for key, candidate_ids in candidates_by_key.items():
        if len(candidate_ids) == 1:
            result[key] = next(iter(candidate_ids))
            continue
        logger.error(
            "Omitting ambiguous normalized instrument symbol",
            normalized_symbol=key,
            candidate_instr_ids=sorted(candidate_ids),
        )
    return result
