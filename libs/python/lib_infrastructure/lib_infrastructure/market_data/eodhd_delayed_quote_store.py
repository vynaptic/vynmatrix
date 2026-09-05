"""Immutable owner-scoped persistence for EODHD US extended delayed quotes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    User,
)
from lib_application.services.equity_lineage import (
    OWNER_SCOPED_DELAYED_BBO_CONTRACT,
    canonical_equity_quote_decimal,
    owner_scoped_delayed_bbo_sha256,
)
from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.hashing import canonical_json_hash

from .eodhd_client import (
    EODHDClient,
    EODHDDelayedQuote,
    EODHDDelayedQuoteBatch,
)

EODHD_DELAYED_PROVIDER = "eodhd"
EODHD_DELAYED_PRODUCT = "us-extended-delayed-quotes"
EODHD_DELAYED_ENDPOINT = "/api/us-quote-delayed"
EODHD_DELAYED_DATASET_VERSION = "eodhd-us-quote-delayed-v2-2025"
EODHD_DELAYED_TOOL_VERSION = "vynmatrix-eodhd-delayed-quote-v1"
EODHD_DELAYED_ADJUSTMENT_POLICY = "unadjusted-quote-snapshot"
EODHD_DELAYED_MISSING_DATA_POLICY = "fail-closed"
EODHD_PERSONAL_PAPER_ENTITLEMENT = "eodhd-personal-use-paper-only"
_FUTURE_SKEW = timedelta(seconds=5)
_SHA256_LENGTH = 64
_TIMESTAMP_SEMANTICS: dict[str, str] = {
    "event_at": "EODHD lastTradeTime Unix milliseconds in UTC; delayed market event",
    "available_at": "first successful local HTTP retrieval time in UTC",
    "last_trade_at": "EODHD lastTradeTime Unix milliseconds in UTC",
    "bid_at": "EODHD bidTime Unix milliseconds in UTC when bid is present",
    "ask_at": "EODHD askTime Unix milliseconds in UTC when ask is present",
    "snapshot_at": "EODHD timestamp Unix seconds in UTC",
    "execution_authority": "personal-use paper execution only; never live authority",
}


class EODHDDelayedQuotePersistenceError(RuntimeError):
    """A delayed quote cannot be persisted without corrupting ownership or lineage."""


def _validate_quote_timestamps(
    quote: EODHDDelayedQuote,
    *,
    retrieved_at: datetime,
) -> None:
    if quote.last_trade_at > retrieved_at + _FUTURE_SKEW:
        msg = f"EODHD delayed quote trade timestamp is future-dated for {quote.symbol}"
        raise EODHDDelayedQuotePersistenceError(msg)
    if quote.snapshot_at > retrieved_at + _FUTURE_SKEW:
        msg = f"EODHD delayed quote snapshot timestamp is future-dated for {quote.symbol}"
        raise EODHDDelayedQuotePersistenceError(msg)
    for side, event_at in (("bid", quote.bid_at), ("ask", quote.ask_at)):
        assert event_at is not None
        if event_at > retrieved_at + _FUTURE_SKEW:
            msg = f"EODHD delayed quote {side} timestamp is future-dated for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)
        if event_at > quote.snapshot_at + _FUTURE_SKEW:
            msg = f"EODHD delayed quote {side} timestamp exceeds its snapshot for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)


@dataclass(frozen=True, slots=True)
class StoredEODHDDelayedQuote:
    """Stable identities of one idempotently persisted quote observation."""

    symbol: str
    instrument_id: int
    lineage_id: str
    observation_id: str
    revision: int
    supersedes_observation_id: str | None


class EODHDDelayedQuoteStore:
    """Write EODHD delayed snapshots into the existing immutable evidence graph.

    The caller owns the transaction.  PostgreSQL writes take a transaction-level
    advisory lock per owner/instrument/provider-record so concurrent polling
    cannot fork a correction chain.  Repeated content is idempotent; changed
    content at the same provider snapshot identity creates an explicit revision
    that supersedes, but never mutates, the prior observation.
    """

    def store_batch(
        self,
        session: Session,
        *,
        batch: EODHDDelayedQuoteBatch,
        entitlement_owner_user_id: str,
        instrument_ids: dict[str, int],
        entitlement_scope: str = EODHD_PERSONAL_PAPER_ENTITLEMENT,
    ) -> tuple[StoredEODHDDelayedQuote, ...]:
        owner = _required_text(entitlement_owner_user_id, field_name="entitlement owner")
        scope = _required_text(entitlement_scope, field_name="entitlement scope")
        user = session.get(User, owner)
        if user is None or str(user.status) != "active":
            msg = "EODHD delayed quote owner must be an active persisted user"
            raise EODHDDelayedQuotePersistenceError(msg)
        retrieved_at = _aware_utc(batch.retrieved_at, field_name="retrieved_at")
        requested = {quote.symbol for quote in batch.quotes}
        if len(requested) != len(batch.quotes):
            msg = "EODHD delayed quote batch contains duplicate symbols"
            raise EODHDDelayedQuotePersistenceError(msg)
        if not _is_sha256(batch.raw_response_sha256) or any(
            quote.raw_response_sha256 != batch.raw_response_sha256 for quote in batch.quotes
        ):
            msg = "EODHD delayed quote batch raw response digest is inconsistent"
            raise EODHDDelayedQuotePersistenceError(msg)
        if requested != set(instrument_ids):
            msg = "EODHD delayed quote instrument mapping must exactly cover the fetched batch"
            raise EODHDDelayedQuotePersistenceError(msg)
        stored = [
            self._store_quote(
                session,
                quote=quote,
                retrieved_at=retrieved_at,
                owner_user_id=owner,
                entitlement_scope=scope,
                instrument_id=instrument_ids[quote.symbol],
            )
            for quote in batch.quotes
        ]
        return tuple(stored)

    def _store_quote(
        self,
        session: Session,
        *,
        quote: EODHDDelayedQuote,
        retrieved_at: datetime,
        owner_user_id: str,
        entitlement_scope: str,
        instrument_id: int,
    ) -> StoredEODHDDelayedQuote:
        if (
            isinstance(instrument_id, bool)
            or not isinstance(instrument_id, int)
            or instrument_id <= 0
        ):
            msg = f"EODHD delayed quote {quote.symbol} requires a positive instrument ID"
            raise EODHDDelayedQuotePersistenceError(msg)
        base_symbol = quote.symbol.removesuffix(".US")
        instrument = resolve_instrument(
            session,
            base_symbol,
            instrument_id=instrument_id,
            asset_class="equity",
        )
        if instrument is None:
            msg = f"EODHD delayed quote instrument is unavailable for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)
        if str(instrument.settlement_currency).upper() != quote.currency:
            msg = f"EODHD delayed quote currency disagrees with instrument {instrument_id}"
            raise EODHDDelayedQuotePersistenceError(msg)
        if any(
            value is None
            for value in (
                quote.bid_price,
                quote.bid_size,
                quote.bid_at,
                quote.ask_price,
                quote.ask_size,
                quote.ask_at,
            )
        ):
            msg = f"EODHD delayed quote BBO evidence is incomplete for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)
        if int(quote.bid_size or 0) <= 0 or int(quote.ask_size or 0) <= 0:
            msg = f"EODHD delayed quote BBO sizes are not positive for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)
        if quote.bid_price is None or quote.ask_price is None or quote.bid_price > quote.ask_price:
            msg = f"EODHD delayed quote BBO is crossed or invalid for {quote.symbol}"
            raise EODHDDelayedQuotePersistenceError(msg)
        _validate_quote_timestamps(quote, retrieved_at=retrieved_at)
        normalized_content_sha256 = _normalized_quote_content_sha256(quote)

        source_identity = f"eodhd:us-quote-delayed:{quote.symbol}"
        source_record_identity = f"{source_identity}:snapshot:{int(quote.snapshot_at.timestamp())}"
        _acquire_series_lock(
            session,
            owner_user_id=owner_user_id,
            instrument_id=instrument_id,
            source_record_identity=source_record_identity,
        )
        previous = self._latest_record(
            session,
            owner_user_id=owner_user_id,
            instrument_id=instrument_id,
            source_identity=source_identity,
            source_record_identity=source_record_identity,
        )
        if previous is not None and str(previous.content_sha256) == normalized_content_sha256:
            return StoredEODHDDelayedQuote(
                symbol=quote.symbol,
                instrument_id=instrument_id,
                lineage_id=str(previous.lineage_id),
                observation_id=str(previous.observation_id),
                revision=int(previous.revision),
                supersedes_observation_id=(
                    str(previous.supersedes_observation_id)
                    if previous.supersedes_observation_id is not None
                    else None
                ),
            )

        revision = int(previous.revision) + 1 if previous is not None else 1
        supersedes = str(previous.observation_id) if previous is not None else None
        lineage = _build_lineage(
            quote=quote,
            retrieved_at=retrieved_at,
            owner_user_id=owner_user_id,
            entitlement_scope=entitlement_scope,
            source_identity=source_identity,
        )
        persisted_lineage = session.get(EquitySourceLineage, lineage.lineage_id)
        if persisted_lineage is None:
            session.add(lineage)
            session.flush()
        else:
            _validate_existing_lineage(persisted_lineage, lineage)

        observation_id = canonical_json_hash(
            {
                "schema": "normalized-delayed-bbo-observation-id-v1",
                "lineage_id": str(lineage.lineage_id),
                "instrument_id": instrument_id,
                "source_record_identity": source_record_identity,
                "revision": revision,
                "supersedes_observation_id": supersedes,
                "content_sha256": normalized_content_sha256,
            }
        )
        existing_observation = session.get(EquityObservation, observation_id)
        if existing_observation is not None:
            _validate_existing_observation(
                existing_observation,
                lineage_id=str(lineage.lineage_id),
                instrument_id=instrument_id,
                source_record_identity=source_record_identity,
                revision=revision,
                supersedes_observation_id=supersedes,
                content_sha256=normalized_content_sha256,
            )
        else:
            observation = EquityObservation(
                observation_id=observation_id,
                lineage_id=str(lineage.lineage_id),
                instr_id=instrument_id,
                observation_kind="price",
                source_record_identity=source_record_identity,
                event_at=quote.last_trade_at,
                available_at=retrieved_at,
                revision=revision,
                supersedes_observation_id=supersedes,
                disposition="observed",
                content_sha256=normalized_content_sha256,
            )
            session.add(observation)
            session.flush()
            session.add_all(_typed_values(observation_id=observation_id, quote=quote))
            session.flush()
        return StoredEODHDDelayedQuote(
            symbol=quote.symbol,
            instrument_id=instrument_id,
            lineage_id=str(lineage.lineage_id),
            observation_id=observation_id,
            revision=revision,
            supersedes_observation_id=supersedes,
        )

    @staticmethod
    def _latest_record(
        session: Session,
        *,
        owner_user_id: str,
        instrument_id: int,
        source_identity: str,
        source_record_identity: str,
    ) -> EquityObservation | None:
        return session.execute(
            select(EquityObservation)
            .join(
                EquitySourceLineage,
                EquityObservation.lineage_id == EquitySourceLineage.lineage_id,
            )
            .where(
                EquityObservation.instr_id == instrument_id,
                EquityObservation.observation_kind == "price",
                EquityObservation.source_record_identity == source_record_identity,
                EquitySourceLineage.provider == EODHD_DELAYED_PROVIDER,
                EquitySourceLineage.product == EODHD_DELAYED_PRODUCT,
                EquitySourceLineage.source_identity == source_identity,
                EquitySourceLineage.entitlement_owner_user_id == owner_user_id,
            )
            .order_by(desc(EquityObservation.revision))
            .limit(1)
        ).scalar_one_or_none()


class EODHDDelayedQuoteIngestion:
    """Acquire then atomically persist one complete owner-scoped quote batch."""

    def __init__(
        self,
        *,
        client: EODHDClient,
        session_factory: Callable[[], AbstractContextManager[Session]],
        store: EODHDDelayedQuoteStore | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._store = store or EODHDDelayedQuoteStore()

    def acquire_and_persist(
        self,
        *,
        product_ids: Sequence[str],
        entitlement_owner_user_id: str,
        instrument_ids: dict[str, int],
    ) -> tuple[StoredEODHDDelayedQuote, ...]:
        """Fetch outside the transaction, then commit the evidence graph atomically."""

        batch = self._client.fetch_us_extended_delayed_quotes(product_ids=product_ids)
        with self._session_factory() as session:
            stored = self._store.store_batch(
                session,
                batch=batch,
                entitlement_owner_user_id=entitlement_owner_user_id,
                instrument_ids=instrument_ids,
            )
            session.commit()
            return stored


def _normalized_quote_content_sha256(quote: EODHDDelayedQuote) -> str:
    if (
        quote.bid_price is None
        or quote.bid_size is None
        or quote.bid_at is None
        or quote.ask_price is None
        or quote.ask_size is None
        or quote.ask_at is None
    ):
        msg = f"EODHD delayed quote BBO evidence is incomplete for {quote.symbol}"
        raise EODHDDelayedQuotePersistenceError(msg)
    return owner_scoped_delayed_bbo_sha256(
        source_symbol=quote.symbol,
        exchange=quote.exchange,
        currency=quote.currency,
        last_trade_price=quote.last_trade_price,
        last_trade_at=quote.last_trade_at,
        last_trade_size=quote.last_trade_size,
        bid_price=quote.bid_price,
        bid_size=quote.bid_size,
        bid_at=quote.bid_at,
        ask_price=quote.ask_price,
        ask_size=quote.ask_size,
        ask_at=quote.ask_at,
        snapshot_at=quote.snapshot_at,
        source_content_sha256=quote.content_sha256,
        raw_response_sha256=quote.raw_response_sha256,
    )


def _build_lineage(
    *,
    quote: EODHDDelayedQuote,
    retrieved_at: datetime,
    owner_user_id: str,
    entitlement_scope: str,
    source_identity: str,
) -> EquitySourceLineage:
    source_revision = f"snapshot-{int(quote.snapshot_at.timestamp())}-{quote.content_sha256}"
    identity = {
        "schema": "eodhd-delayed-quote-lineage-v1",
        "provider": EODHD_DELAYED_PROVIDER,
        "product": EODHD_DELAYED_PRODUCT,
        "endpoint": EODHD_DELAYED_ENDPOINT,
        "dataset_version": EODHD_DELAYED_DATASET_VERSION,
        "tool_version": EODHD_DELAYED_TOOL_VERSION,
        "source_identity": source_identity,
        "source_revision": source_revision,
        "timestamp_semantics": _TIMESTAMP_SEMANTICS,
        "adjustment_policy": EODHD_DELAYED_ADJUSTMENT_POLICY,
        "entitlement_scope": entitlement_scope,
        "entitlement_owner_user_id": owner_user_id,
        "missing_data_policy": EODHD_DELAYED_MISSING_DATA_POLICY,
        "content_sha256": quote.content_sha256,
    }
    return EquitySourceLineage(
        lineage_id=canonical_json_hash(identity),
        provider=EODHD_DELAYED_PROVIDER,
        product=EODHD_DELAYED_PRODUCT,
        endpoint=EODHD_DELAYED_ENDPOINT,
        dataset_version=EODHD_DELAYED_DATASET_VERSION,
        tool_version=EODHD_DELAYED_TOOL_VERSION,
        source_identity=source_identity,
        source_revision=source_revision,
        retrieved_at=retrieved_at,
        timestamp_semantics=dict(_TIMESTAMP_SEMANTICS),
        adjustment_policy=EODHD_DELAYED_ADJUSTMENT_POLICY,
        entitlement_scope=entitlement_scope,
        entitlement_owner_user_id=owner_user_id,
        missing_data_policy=EODHD_DELAYED_MISSING_DATA_POLICY,
        content_sha256=quote.content_sha256,
    )


def _typed_values(
    *,
    observation_id: str,
    quote: EODHDDelayedQuote,
) -> list[EquityObservationValue]:
    values: list[tuple[str, str, object, str | None]] = [
        ("quote_contract", "text", OWNER_SCOPED_DELAYED_BBO_CONTRACT, None),
        ("source_content_sha256", "text", quote.content_sha256, None),
        ("symbol", "text", quote.symbol, None),
        ("exchange", "text", quote.exchange, None),
        ("currency", "text", quote.currency, None),
        ("last_trade_price", "decimal", quote.last_trade_price, f"{quote.currency}/share"),
        (
            "last_trade_price_canonical",
            "text",
            canonical_equity_quote_decimal(quote.last_trade_price),
            None,
        ),
        ("last_trade_at", "timestamp", quote.last_trade_at, None),
        ("snapshot_at", "timestamp", quote.snapshot_at, None),
        ("raw_response_sha256", "text", quote.raw_response_sha256, None),
    ]
    optional: tuple[tuple[str, str, object | None, str | None], ...] = (
        ("last_trade_size", "integer", quote.last_trade_size, "shares"),
        ("bid_price", "decimal", quote.bid_price, f"{quote.currency}/share"),
        (
            "bid_price_canonical",
            "text",
            canonical_equity_quote_decimal(quote.bid_price)
            if quote.bid_price is not None
            else None,
            None,
        ),
        ("bid_size", "integer", quote.bid_size, "shares"),
        ("bid_at", "timestamp", quote.bid_at, None),
        ("ask_price", "decimal", quote.ask_price, f"{quote.currency}/share"),
        (
            "ask_price_canonical",
            "text",
            canonical_equity_quote_decimal(quote.ask_price)
            if quote.ask_price is not None
            else None,
            None,
        ),
        ("ask_size", "integer", quote.ask_size, "shares"),
        ("ask_at", "timestamp", quote.ask_at, None),
    )
    values.extend(
        (name, kind, value, unit) for name, kind, value, unit in optional if value is not None
    )
    rows: list[EquityObservationValue] = []
    for field_name, value_type, value, unit in values:
        value_id = canonical_json_hash(
            {
                "schema": "eodhd-delayed-quote-value-id-v1",
                "observation_id": observation_id,
                "field_name": field_name,
                "ordinal": 0,
            }
        )
        kwargs: dict[str, Any] = {
            "value_id": value_id,
            "observation_id": observation_id,
            "field_name": field_name,
            "ordinal": 0,
            "value_type": value_type,
            "unit": unit,
        }
        if value_type == "text":
            kwargs["text_value"] = str(value)
        elif value_type == "decimal":
            kwargs["decimal_value"] = Decimal(str(value))
        elif value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                msg = f"Delayed quote value {field_name!r} must be an integer"
                raise EODHDDelayedQuotePersistenceError(msg)
            kwargs["integer_value"] = value
        elif value_type == "timestamp":
            kwargs["timestamp_value"] = _aware_utc(value, field_name=field_name)  # type: ignore[arg-type]
        else:  # pragma: no cover - all value types are declared above
            msg = f"Unsupported delayed quote value type {value_type!r}"
            raise EODHDDelayedQuotePersistenceError(msg)
        rows.append(EquityObservationValue(**kwargs))
    return rows


def _acquire_series_lock(
    session: Session,
    *,
    owner_user_id: str,
    instrument_id: int,
    source_record_identity: str,
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    digest = canonical_json_hash(
        {
            "schema": "eodhd-delayed-quote-series-lock-v1",
            "owner_user_id": owner_user_id,
            "instrument_id": instrument_id,
            "source_record_identity": source_record_identity,
        }
    )
    lock_key = int(digest[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    session.execute(select(func.pg_advisory_xact_lock(lock_key))).scalar_one()


def _validate_existing_lineage(
    existing: EquitySourceLineage,
    expected: EquitySourceLineage,
) -> None:
    fields = (
        "provider",
        "product",
        "endpoint",
        "dataset_version",
        "tool_version",
        "source_identity",
        "source_revision",
        "timestamp_semantics",
        "adjustment_policy",
        "entitlement_scope",
        "entitlement_owner_user_id",
        "missing_data_policy",
        "content_sha256",
    )
    if any(getattr(existing, field) != getattr(expected, field) for field in fields):
        msg = "EODHD delayed quote lineage identity collided with different semantics"
        raise EODHDDelayedQuotePersistenceError(msg)


def _validate_existing_observation(
    existing: EquityObservation,
    *,
    lineage_id: str,
    instrument_id: int,
    source_record_identity: str,
    revision: int,
    supersedes_observation_id: str | None,
    content_sha256: str,
) -> None:
    if (
        str(existing.lineage_id) != lineage_id
        or int(existing.instr_id or 0) != instrument_id
        or str(existing.observation_kind) != "price"
        or str(existing.source_record_identity) != source_record_identity
        or int(existing.revision) != revision
        or existing.supersedes_observation_id != supersedes_observation_id
        or str(existing.disposition) != "observed"
        or str(existing.content_sha256) != content_sha256
    ):
        msg = "EODHD delayed quote observation identity collided with different semantics"
        raise EODHDDelayedQuotePersistenceError(msg)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"EODHD delayed quote {field_name} must be non-empty text"
        raise EODHDDelayedQuotePersistenceError(msg)
    return value.strip()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        msg = f"EODHD delayed quote {field_name} must be timezone-aware"
        raise EODHDDelayedQuotePersistenceError(msg)
    return value.astimezone(UTC)


__all__ = [
    "EODHD_DELAYED_ADJUSTMENT_POLICY",
    "EODHD_DELAYED_DATASET_VERSION",
    "EODHD_DELAYED_ENDPOINT",
    "EODHD_DELAYED_MISSING_DATA_POLICY",
    "EODHD_DELAYED_PRODUCT",
    "EODHD_DELAYED_PROVIDER",
    "EODHD_DELAYED_TOOL_VERSION",
    "EODHD_PERSONAL_PAPER_ENTITLEMENT",
    "EODHDDelayedQuoteIngestion",
    "EODHDDelayedQuotePersistenceError",
    "EODHDDelayedQuoteStore",
    "StoredEODHDDelayedQuote",
]
