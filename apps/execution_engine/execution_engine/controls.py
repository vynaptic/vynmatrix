"""Operator controls and audit persistence for live execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from lib_common.logging import get_logger

logger = get_logger(__name__)

_HALT_RULE_KEY = "global_trading_halt"


@dataclass(frozen=True)
class TradingHaltState:
    """Current global trading halt state."""

    enabled: bool = False
    reason: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


class TradingHaltStore:
    """Append-only global trading halt state stored in risk_mandates."""

    def __init__(self, *, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory
        self._memory_state = TradingHaltState()

    def get_state(self) -> TradingHaltState:
        if self._session_factory is None:
            return self._memory_state

        try:
            from lib_application.db.models import RiskMandate  # noqa: PLC0415
        except ImportError:
            return self._memory_state

        try:
            with self._session_factory() as session:
                row = (
                    session.query(RiskMandate)
                    .filter(RiskMandate.user_id.is_(None))
                    .order_by(RiskMandate.effective_at.desc())
                    .all()
                )
                for candidate in row:
                    rules = dict(candidate.rules or {})
                    if _HALT_RULE_KEY not in rules:
                        continue
                    payload = dict(rules.get(_HALT_RULE_KEY) or {})
                    return TradingHaltState(
                        enabled=bool(payload.get("enabled", False)),
                        reason=payload.get("reason"),
                        updated_by=payload.get("updated_by"),
                        updated_at=payload.get("updated_at"),
                        metadata=dict(payload.get("metadata") or {}),
                    )
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to load trading halt state")
            raise

        return TradingHaltState()

    def set_state(
        self,
        *,
        enabled: bool,
        reason: str | None,
        updated_by: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> TradingHaltState:
        now = datetime.now(tz=UTC)
        state = TradingHaltState(
            enabled=enabled,
            reason=reason,
            updated_by=updated_by,
            updated_at=now.isoformat(),
            metadata=dict(metadata or {}),
        )
        self._memory_state = state

        if self._session_factory is None:
            return state

        try:
            from lib_application.db.models import RiskMandate  # noqa: PLC0415
        except ImportError:
            return state

        try:
            with self._session_factory() as session:
                session.add(
                    RiskMandate(
                        user_id=None,
                        rules={_HALT_RULE_KEY: state.to_dict()},
                        effective_at=now,
                    )
                )
                session.commit()
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to persist trading halt state")
            raise

        return state


class AuditLogStore:
    """Append immutable API audit records."""

    def __init__(self, *, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def record(
        self,
        *,
        action: str,
        status: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> None:
        if self._session_factory is None:
            logger.info("Audit event", action=action, status=status, user_id=user_id)
            return

        try:
            from lib_application.db.models import ApiAuditLog  # noqa: PLC0415
        except ImportError:
            logger.warning("ApiAuditLog model unavailable; audit event logged only")
            return

        try:
            with self._session_factory() as session:
                session.add(
                    ApiAuditLog(
                        user_id=user_id,
                        action=action,
                        req=dict(request_payload),
                        resp=dict(response_payload or {}),
                        status=status,
                    )
                )
                session.commit()
        except (SQLAlchemyError, OSError):
            logger.exception("Failed to persist audit event", action=action, status=status)
            raise


__all__ = ["AuditLogStore", "TradingHaltState", "TradingHaltStore"]
