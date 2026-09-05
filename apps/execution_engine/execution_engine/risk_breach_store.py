"""Risk-breach + circuit-breaker persistence store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func

from lib_application.db.models import LinkedBrokerAccount, RiskBreach, RiskMandate

from ._persistence_common import _resolve_session_factory


class RiskBreachStore:
    """Persist operational and pre-trade risk breaches."""

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

    def record(
        self,
        *,
        user_id: str,
        rule_code: str,
        severity: str = "block",
        context: dict[str, Any] | None = None,
        broker_account_id: int | None = None,
    ) -> int:
        normalized_user_id = self._require_user_id(user_id)
        payload = dict(context or {})
        context_user_id = payload.get("user_id")
        if context_user_id is not None and str(context_user_id).strip() != normalized_user_id:
            msg = "Risk context user_id conflicts with relational user ownership"
            raise ValueError(msg)
        payload["user_id"] = normalized_user_id
        context_account_id = payload.get("broker_account_id")
        if broker_account_id is None and context_account_id is not None:
            if isinstance(context_account_id, bool):
                msg = "broker_account_id in risk context must be a positive integer"
                raise ValueError(msg)
            try:
                broker_account_id = int(context_account_id)
            except (TypeError, ValueError) as exc:
                msg = "broker_account_id in risk context must be a positive integer"
                raise ValueError(msg) from exc
        if broker_account_id is not None:
            if context_account_id is not None:
                if isinstance(context_account_id, bool):
                    msg = "broker_account_id in risk context must be a positive integer"
                    raise ValueError(msg)
                try:
                    normalized_context_account_id = int(context_account_id)
                except (TypeError, ValueError) as exc:
                    msg = "broker_account_id in risk context must be a positive integer"
                    raise ValueError(msg) from exc
                if normalized_context_account_id != broker_account_id:
                    msg = (
                        "Risk context broker_account_id conflicts with relational account ownership"
                    )
                    raise ValueError(msg)
            payload["broker_account_id"] = broker_account_id
        with self._session_factory() as session:
            self._validate_account_owner(
                session,
                user_id=normalized_user_id,
                broker_account_id=broker_account_id,
            )
            breach = RiskBreach(
                user_id=normalized_user_id,
                broker_account_id=broker_account_id,
                rule_code=rule_code,
                severity=severity,
                context=payload,
            )
            if self._uses_sqlite(session):
                breach.breach_id = self._next_risk_breach_id(session)
            session.add(breach)
            session.commit()
            session.refresh(breach)
            breach_id: int = int(breach.breach_id)
            return breach_id

    def load_mandates(self, *, user_id: str) -> list[dict[str, Any]]:
        normalized_user_id = self._require_user_id(user_id)
        now = datetime.now(tz=UTC)
        with self._session_factory() as session:
            rows = (
                session.query(RiskMandate)
                .filter(RiskMandate.user_id.is_(None) | (RiskMandate.user_id == normalized_user_id))
                .filter(RiskMandate.effective_at <= now)
                .order_by(
                    case(
                        (RiskMandate.user_id.is_(None), 0),
                        else_=1,
                    ),
                    RiskMandate.effective_at.asc(),
                    RiskMandate.mandate_id.asc(),
                )
                .all()
            )
            return [dict(row.rules or {}) for row in rows]

    def has_user_drawdown_mandate(self, *, user_id: str) -> bool:
        """Return whether an effective user-owned drawdown ceiling exists."""

        normalized_user_id = self._require_user_id(user_id)
        now = datetime.now(tz=UTC)
        with self._session_factory() as session:
            rows = (
                session.query(RiskMandate.rules)
                .filter(
                    RiskMandate.user_id == normalized_user_id,
                    RiskMandate.effective_at <= now,
                )
                .all()
            )
        return any("max_drawdown_pct" in dict(rules or {}) for (rules,) in rows)

    def upsert_circuit_breaker_state(
        self,
        *,
        user_id: str,
        scope: str,
        breaker_key: str,
        open_until: datetime,
        failure_count: int,
        last_reason: str | None = None,
        strategy_id: str | None = None,
        broker: str | None = None,
        environment: str | None = None,
        broker_account_id: int | None = None,
    ) -> int:
        normalized_user_id = self._require_user_id(user_id)
        rule_code = f"circuit_breaker:{scope}"
        payload = {
            "user_id": normalized_user_id,
            "breaker_key": breaker_key,
            "state": "open",
            "open_until": open_until.astimezone(UTC).isoformat(),
            "failure_count": int(failure_count),
            "last_reason": last_reason,
            "strategy_id": strategy_id,
            "broker": broker,
            "environment": environment,
        }
        if broker_account_id is not None:
            payload["broker_account_id"] = broker_account_id
        with self._session_factory() as session:
            self._validate_account_owner(
                session,
                user_id=normalized_user_id,
                broker_account_id=broker_account_id,
            )
            row = self._find_circuit_breaker_row(
                session,
                breaker_key=breaker_key,
                rule_code=rule_code,
                user_id=normalized_user_id,
                broker_account_id=broker_account_id,
            )
            if row is None:
                row = RiskBreach(
                    user_id=normalized_user_id,
                    broker_account_id=broker_account_id,
                    rule_code=rule_code,
                    severity="block",
                    context=payload,
                )
                if self._uses_sqlite(session):
                    row.breach_id = self._next_risk_breach_id(session)
                session.add(row)
            else:
                row.user_id = normalized_user_id
                row.broker_account_id = broker_account_id
                row.rule_code = rule_code
                row.severity = "block"
                row.context = payload
                row.occurred_at = datetime.now(tz=UTC)
            session.commit()
            session.refresh(row)
            return int(row.breach_id)

    def load_active_circuit_breakers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now.astimezone(UTC) if now else datetime.now(tz=UTC)
        with self._session_factory() as session:
            rows = (
                session.query(RiskBreach)
                .filter(
                    RiskBreach.rule_code.like("circuit_breaker:%"),
                    RiskBreach.severity.in_(("block", "warn")),
                )
                .order_by(RiskBreach.occurred_at.desc(), RiskBreach.breach_id.desc())
                .all()
            )
        active_by_key: dict[str, dict[str, Any]] = {}
        for row in rows:
            context = dict(row.context or {})
            breaker_key = str(context.get("breaker_key") or "").strip()
            if not breaker_key or breaker_key in active_by_key:
                continue
            open_until = self._parse_timestamp(context.get("open_until"))
            if open_until is None or open_until <= current:
                continue
            active_by_key[breaker_key] = {
                "breaker_key": breaker_key,
                "scope": str(row.rule_code).split(":", 1)[1] if ":" in str(row.rule_code) else "",
                "state": str(context.get("state") or "open"),
                "open_until": open_until,
                "failure_count": int(context.get("failure_count") or 0),
                "last_reason": context.get("last_reason"),
            }
        return list(active_by_key.values())

    def clear_circuit_breaker_state(self, *, breaker_key: str) -> int:
        with self._session_factory() as session:
            rows = (
                session.query(RiskBreach)
                .filter(RiskBreach.rule_code.like("circuit_breaker:%"))
                .all()
            )
            deleted = 0
            for row in rows:
                context = dict(row.context or {})
                if str(context.get("breaker_key") or "").strip() != breaker_key:
                    continue
                session.delete(row)
                deleted += 1
            if deleted:
                session.commit()
            else:
                session.rollback()
            return deleted

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _find_circuit_breaker_row(
        session: Any,
        *,
        breaker_key: str,
        rule_code: str,
        user_id: str,
        broker_account_id: int | None,
    ) -> RiskBreach | None:
        account_filter = (
            RiskBreach.broker_account_id.is_(None)
            if broker_account_id is None
            else RiskBreach.broker_account_id == broker_account_id
        )
        rows: list[RiskBreach] = (
            session.query(RiskBreach)
            .filter(
                RiskBreach.rule_code == rule_code,
                RiskBreach.user_id == user_id,
                account_filter,
            )
            .order_by(RiskBreach.occurred_at.desc(), RiskBreach.breach_id.desc())
            .all()
        )
        for row in rows:
            context = dict(row.context or {})
            if str(context.get("breaker_key") or "").strip() == breaker_key:
                return row
        return None

    @staticmethod
    def _uses_sqlite(session: Any) -> bool:
        bind = getattr(session, "bind", None)
        return bool(bind is not None and bind.dialect.name == "sqlite")

    @staticmethod
    def _next_risk_breach_id(session: Any) -> int:
        current = session.query(func.max(RiskBreach.breach_id)).scalar()
        return int(current or 0) + 1

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        normalized = str(user_id).strip()
        if not normalized:
            msg = "Risk ownership requires a non-empty user_id"
            raise ValueError(msg)
        return normalized

    @staticmethod
    def _validate_account_owner(
        session: Any,
        *,
        user_id: str,
        broker_account_id: int | None,
    ) -> None:
        if broker_account_id is None:
            return
        if isinstance(broker_account_id, bool) or int(broker_account_id) <= 0:
            msg = "broker_account_id must be a positive integer"
            raise ValueError(msg)
        owner = (
            session.query(LinkedBrokerAccount.user_id)
            .filter(LinkedBrokerAccount.account_id == int(broker_account_id))
            .scalar()
        )
        if owner is None:
            msg = f"Unknown broker account {broker_account_id}"
            raise ValueError(msg)
        if str(owner) != user_id:
            msg = f"Broker account {broker_account_id} is not owned by user {user_id}"
            raise ValueError(msg)
