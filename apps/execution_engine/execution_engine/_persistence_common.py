"""Shared helpers for the execution-persistence stores.

Extracted from the former monolithic ``persistence.py`` so the per-store modules
(``execution_log_store``, ``execution_metrics_store``, ``risk_breach_store``,
``execution_position_store``) can share one session-factory resolver and the
idempotent strategy-upsert helper.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_application.db.models import Strategy
from lib_application.db.session import get_session_factory

_DECIMAL_ZERO = Decimal("0")


def _require_positive_account_id(account_id: int) -> int:
    """Validate the mandatory linked-account identity at write boundaries."""
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        msg = "account_id must be a positive linked broker account identifier"
        raise ValueError(msg)
    return account_id


def _resolve_session_factory(
    *,
    database_url: str | None = None,
    session_factory: Any | None = None,
) -> Any:
    if session_factory is not None:
        return session_factory
    if not database_url:
        msg = "Either database_url or session_factory is required"
        raise ValueError(msg)
    return get_session_factory(db_url=database_url)


def _ensure_strategy_exists(
    session_factory: Any,
    strategy_id: str,
    strategy_name: str | None = None,
    asset_class: str | None = None,
) -> None:
    """Ensure a strategy record exists in the database.

    Creates the strategy if it doesn't exist. Silently handles duplicates
    via rollback (race condition safe).

    Args:
        session_factory: SQLAlchemy session factory
        strategy_id: Unique strategy identifier
        strategy_name: Human-readable name (defaults to strategy_id)
        asset_class: Asset class for the strategy
    """
    if not strategy_id:
        return
    with session_factory() as session:
        existing = session.query(Strategy).filter_by(strategy_id=strategy_id).first()
        if existing:
            return
        session.add(
            Strategy(
                strategy_id=strategy_id,
                strategy_name=strategy_name or strategy_id,
                asset_class=asset_class,
            )
        )
        try:
            session.commit()
        except SQLAlchemyError:
            # Idempotency race: another writer beat us to it. Roll back so
            # the next call re-reads the row instead of erroring out.
            session.rollback()
