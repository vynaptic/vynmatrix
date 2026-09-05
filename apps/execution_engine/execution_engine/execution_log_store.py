"""Execution-log persistence store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from lib_application.db.models import ExecutionLog as ExecutionLogModel
from lib_infrastructure.persistence.sqlalchemy.repositories.execution_repo import (
    SQLAlchemyExecutionRepository,
)
from lib_strategy.domain.entities import ExecutionLog

from ._persistence_common import (
    _ensure_strategy_exists,
    _require_positive_account_id,
    _resolve_session_factory,
)


class ExecutionLogStore:
    """Persist execution results into execution_logs."""

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
        self._repo = SQLAlchemyExecutionRepository(self._session_factory)

    def log(
        self,
        *,
        user_id: str,
        account_id: int,
        strategy_id: str,
        canonical_signal_id: int | None = None,
        signal_type: str,
        execution_mode: str,
        status: str,
        details: dict[str, Any],
        error_message: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Persist an execution_logs row."""
        account_id = _require_positive_account_id(account_id)
        _ensure_strategy_exists(self._session_factory, strategy_id=strategy_id)
        log = ExecutionLog(
            log_id=str(uuid.uuid4()),
            user_id=user_id,
            account_id=account_id,
            strategy_id=strategy_id,
            canonical_signal_id=canonical_signal_id,
            signal_type=signal_type,
            execution_mode=execution_mode,
            execution_details=details,
            status=status,
            error_message=error_message,
            run_id=run_id,
            created_at=datetime.now(tz=UTC),
        )
        result = self._repo.log_execution(log)
        return str(result) if result else ""

    def count_daily_trades(
        self,
        *,
        user_id: str,
        as_of: datetime | None = None,
    ) -> int:
        """Count actual trades executed today in UTC.

        Only rows representing submitted orders count toward the daily-trade
        limit. Deduplicated and notify-only success paths are excluded even if
        they were persisted with status='executed'.
        """
        now = as_of.astimezone(UTC) if as_of else datetime.now(tz=UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        with self._session_factory() as session:
            rows = (
                session.query(
                    ExecutionLogModel.execution_mode,
                    ExecutionLogModel.execution_details,
                )
                .filter(
                    ExecutionLogModel.user_id == user_id,
                    ExecutionLogModel.status == "executed",
                    ExecutionLogModel.created_at >= day_start,
                    ExecutionLogModel.created_at < day_end,
                )
                .all()
            )
        count = 0
        for execution_mode, execution_details in rows:
            mode = str(execution_mode or "").strip().lower()
            if mode in {"dedup", "notify_only"}:
                continue
            orders_submitted = self._extract_orders_submitted(execution_details)
            if orders_submitted > 0:
                count += 1
        return count

    @staticmethod
    def _extract_orders_submitted(details: Any) -> int:
        if not isinstance(details, dict):
            return 0
        raw_value = details.get("orders_submitted", 0)
        try:
            return int(raw_value or 0)
        except (TypeError, ValueError):
            return 0
