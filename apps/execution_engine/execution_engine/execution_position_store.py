"""Broker-position + options-spread persistence store."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func

from lib_application.db.models import (
    Broker,
    Instrument,
    LinkedBrokerAccount,
    Position,
)
from lib_application.services.instrument_resolution import resolve_instrument
from lib_common.logging import get_logger

from ._persistence_common import _resolve_session_factory
from .brokers.base import PositionValuationError, position_gross_notional
from .metrics.normalization import normalize_position_side

logger = get_logger(__name__)


class ExecutionPositionStore:
    """Persist the broker's canonical account-scoped position snapshot."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        session_factory: Any | None = None,
    ) -> None:
        self._session_factory = _resolve_session_factory(
            database_url=database_url,
            session_factory=session_factory,
        )

    def _resolve_account_id(
        self,
        session: Any,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
    ) -> int:
        """Validate one explicit, connected account route against the database."""
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            msg = "Position persistence requires an explicit user_id"
            raise ValueError(msg)
        broker_key = str(broker_code).strip().lower()
        if not broker_key:
            msg = "Position persistence requires an explicit broker_code"
            raise ValueError(msg)
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            msg = "Position persistence requires a positive integer account_id"
            raise ValueError(msg)
        normalized_environment = str(environment).strip().lower()
        if normalized_environment not in {"paper", "live"}:
            msg = "Position persistence environment must be 'paper' or 'live'"
            raise ValueError(msg)

        account = (
            session.query(LinkedBrokerAccount)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(
                LinkedBrokerAccount.account_id == account_id,
                LinkedBrokerAccount.user_id == normalized_user_id,
                func.lower(Broker.code) == broker_key,
                LinkedBrokerAccount.environment == normalized_environment,
                LinkedBrokerAccount.status == "connected",
            )
            .one_or_none()
        )
        if account is None:
            msg = (
                f"Connected broker account {account_id} is unavailable for "
                f"user={normalized_user_id} broker={broker_key} "
                f"environment={normalized_environment}"
            )
            raise ValueError(msg)
        logger.info(
            "Validated explicit broker account route",
            user_id=normalized_user_id,
            broker=broker_code,
            account_id=account_id,
            environment=normalized_environment,
            source="linked_accounts",
        )
        return account_id

    @staticmethod
    def _uses_sqlite(session: Any) -> bool:
        bind = getattr(session, "bind", None)
        return bool(bind is not None and bind.dialect.name == "sqlite")

    @staticmethod
    def _next_position_id(session: Any) -> int:
        current = session.query(func.max(Position.pos_id)).scalar()
        return int(current or 0) + 1

    def resolve_account_id(
        self,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
    ) -> int:
        """Validate and return the explicitly routed broker account id."""
        with self._session_factory() as session:
            return self._resolve_account_id(
                session,
                user_id=user_id,
                broker_code=broker_code,
                account_id=account_id,
                environment=environment,
            )

    def _resolve_instr_id(self, session: Any, symbol: str) -> int | None:
        if not symbol:
            return None
        instrument = resolve_instrument(session, symbol)
        return int(instrument.instr_id) if instrument is not None else None

    def sync_positions(
        self,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
        positions: list[dict[str, Any]],
        allow_empty_prune: bool = False,
    ) -> None:
        """Mirror a broker's position snapshot into the DB ledger.

        ``allow_empty_prune`` guards the dangerous "delete every position when the
        snapshot is empty" branch (G3): an empty snapshot from a cold/just-restarted
        broker would otherwise wipe the persisted book and orphan open positions.
        Only the authoritative post-fill path (which knows the broker book reflects
        the just-executed state) sets it True; reconciliation leaves it False so a
        transiently-empty broker read cannot zero out the ledger.
        """
        with self._session_factory() as session:
            account_id = self._resolve_account_id(
                session,
                user_id=user_id,
                broker_code=broker_code,
                account_id=account_id,
                environment=environment,
            )
            seen_instr_ids: set[int] = set()
            for pos in positions:
                symbol = str(pos.get("symbol") or "")
                instr_id = self._resolve_instr_id(session, symbol)
                if not instr_id:
                    msg = (
                        f"Position {symbol!r} does not resolve to a canonical "
                        "instrument; refusing a partial account projection"
                    )
                    raise PositionValuationError(msg)
                qty = self._finite_decimal(pos.get("quantity"), field="quantity")
                side = normalize_position_side(pos.get("side"))
                qty = -abs(qty) if side == "short" else abs(qty)

                if qty == 0:
                    session.query(Position).filter_by(
                        account_id=account_id, instr_id=instr_id
                    ).delete()
                    continue

                quantity_unit = str(pos.get("quantity_unit") or "").strip().lower()
                if quantity_unit not in {"asset", "contracts", "quote_notional"}:
                    msg = f"Position {symbol!r} omitted a canonical quantity_unit"
                    raise PositionValuationError(msg)
                contract_multiplier = self._optional_positive_decimal(
                    pos.get("contract_multiplier"),
                    field="contract_multiplier",
                )
                if quantity_unit == "contracts" and contract_multiplier is None:
                    msg = f"Contract position {symbol!r} omitted contract_multiplier"
                    raise PositionValuationError(msg)
                gross_notional = position_gross_notional(pos)
                if gross_notional <= 0:
                    msg = f"Position {symbol!r} has non-positive gross_notional"
                    raise PositionValuationError(msg)
                notional_currency = str(pos.get("notional_currency") or "").strip().upper()
                if not notional_currency or not notional_currency.isalnum():
                    msg = f"Position {symbol!r} omitted notional_currency"
                    raise PositionValuationError(msg)
                if len(notional_currency) > 10:  # noqa: PLR2004
                    msg = f"Position {symbol!r} has invalid notional_currency"
                    raise PositionValuationError(msg)

                avg_price = self._positive_decimal(pos.get("entry_price"), field="entry_price")
                last_mark = self._positive_decimal(
                    pos.get("current_price"),
                    field="current_price",
                )
                seen_instr_ids.add(instr_id)

                existing = (
                    session.query(Position)
                    .filter_by(account_id=account_id, instr_id=instr_id)
                    .first()
                )
                if existing:
                    existing.qty = qty
                    existing.avg_price = avg_price
                    existing.last_mark = last_mark
                    existing.quantity_unit = quantity_unit
                    existing.contract_multiplier = contract_multiplier
                    existing.gross_notional = gross_notional
                    existing.notional_currency = notional_currency
                else:
                    position_id = (
                        self._next_position_id(session) if self._uses_sqlite(session) else None
                    )
                    session.add(
                        Position(
                            pos_id=position_id,
                            account_id=account_id,
                            instr_id=instr_id,
                            qty=qty,
                            avg_price=avg_price,
                            last_mark=last_mark,
                            quantity_unit=quantity_unit,
                            contract_multiplier=contract_multiplier,
                            gross_notional=gross_notional,
                            notional_currency=notional_currency,
                        )
                    )

            if seen_instr_ids:
                session.query(Position).filter(
                    Position.account_id == account_id,
                    Position.instr_id.notin_(seen_instr_ids),
                ).delete(synchronize_session=False)
            elif allow_empty_prune:
                session.query(Position).filter(
                    Position.account_id == account_id,
                ).delete(synchronize_session=False)
            else:
                # Empty snapshot from a non-authoritative caller (e.g. a cold
                # broker during reconciliation): do NOT wipe the ledger.
                logger.info(
                    "Skipping empty-snapshot position prune (guarded)",
                    user_id=user_id,
                    broker=broker_code,
                    account_id=account_id,
                )
            session.commit()

    def list_positions(
        self,
        *,
        user_id: str,
        broker_code: str,
        account_id: int,
        environment: str,
    ) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            account_id = self._resolve_account_id(
                session,
                user_id=user_id,
                broker_code=broker_code,
                account_id=account_id,
                environment=environment,
            )

            rows = (
                session.query(Position, Instrument.canonical)
                .join(Instrument, Position.instr_id == Instrument.instr_id)
                .filter(Position.account_id == account_id)
                .all()
            )
            result: list[dict[str, Any]] = []
            for pos, canonical in rows:
                qty = float(pos.qty or 0.0)
                if qty == 0:
                    continue
                if pos.avg_price is None or pos.last_mark is None:
                    msg = f"Persisted position {pos.pos_id} omitted a required price"
                    raise PositionValuationError(msg)
                if pos.gross_notional is None or pos.gross_notional <= 0:
                    msg = f"Persisted position {pos.pos_id} omitted gross_notional"
                    raise PositionValuationError(msg)
                if not pos.notional_currency:
                    msg = f"Persisted position {pos.pos_id} omitted notional_currency"
                    raise PositionValuationError(msg)
                result.append(
                    {
                        "symbol": canonical,
                        "side": "long" if qty >= 0 else "short",
                        "quantity": abs(qty),
                        "entry_price": float(pos.avg_price),
                        "current_price": float(pos.last_mark),
                        "quantity_unit": pos.quantity_unit,
                        "contract_multiplier": (
                            None
                            if pos.contract_multiplier is None
                            else float(pos.contract_multiplier)
                        ),
                        "gross_notional": float(pos.gross_notional),
                        "notional_currency": pos.notional_currency,
                    }
                )
            return result

    @staticmethod
    def _finite_decimal(value: Any, *, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            msg = f"Position has invalid {field}: {value!r}"
            raise PositionValuationError(msg) from exc
        if not parsed.is_finite():
            msg = f"Position has non-finite {field}: {value!r}"
            raise PositionValuationError(msg)
        return parsed

    @classmethod
    def _positive_decimal(cls, value: Any, *, field: str) -> Decimal:
        parsed = cls._finite_decimal(value, field=field)
        if parsed <= 0:
            msg = f"Position has non-positive {field}: {value!r}"
            raise PositionValuationError(msg)
        return parsed

    @classmethod
    def _optional_positive_decimal(
        cls,
        value: Any,
        *,
        field: str,
    ) -> Decimal | None:
        if value in (None, ""):
            return None
        return cls._positive_decimal(value, field=field)
