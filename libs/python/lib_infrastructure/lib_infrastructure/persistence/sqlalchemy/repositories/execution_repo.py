"""SQLAlchemy implementation of IExecutionRepository.

This module provides concrete implementation of the execution
repository using SQLAlchemy ORM.
"""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import ExecutionLog as ExecutionLogModel
from lib_strategy.domain.entities import ExecutionLog
from lib_strategy.ports.execution_port import IExecutionRepository


class SQLAlchemyExecutionRepository(IExecutionRepository):
    """
    SQLAlchemy implementation of execution repository.

    Handles execution-log persistence.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """
        Initialize repository with session factory.

        Args:
            session_factory: Function that creates new database sessions
        """
        self.session_factory = session_factory

    @staticmethod
    def log_execution_on_session(session: Session, execution_log: ExecutionLog) -> str:
        """Add an execution-log row to an existing session WITHOUT committing.

        Lets a caller co-commit the log with another write (e.g. a transactional
        outbox event) in one unit of work. The caller owns the commit/rollback.
        """
        log_model = ExecutionLogModel(
            log_id=execution_log.log_id,
            user_id=execution_log.user_id,
            account_id=execution_log.account_id,
            strategy_id=execution_log.strategy_id,
            canonical_signal_id=execution_log.canonical_signal_id,
            signal_type=execution_log.signal_type,
            execution_mode=execution_log.execution_mode,
            execution_details=execution_log.execution_details,
            status=execution_log.status,
            error_message=execution_log.error_message,
            run_id=execution_log.run_id,
        )
        session.add(log_model)
        session.flush()
        return str(log_model.log_id)

    def log_execution(self, execution_log: ExecutionLog) -> str:
        """Log an execution."""
        session = self.session_factory()
        try:
            log_id = self.log_execution_on_session(session, execution_log)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return log_id

    def get_execution_log(self, log_id: str) -> ExecutionLog | None:
        """Get execution log by ID."""
        session = self.session_factory()
        try:
            log_model = session.scalars(select(ExecutionLogModel).filter_by(log_id=log_id)).first()

            if not log_model:
                return None

            return ExecutionLog(
                log_id=log_model.log_id,
                user_id=log_model.user_id,
                account_id=log_model.account_id,
                strategy_id=log_model.strategy_id,
                canonical_signal_id=log_model.canonical_signal_id,
                signal_type=log_model.signal_type,
                execution_mode=log_model.execution_mode,
                execution_details=log_model.execution_details or {},
                status=log_model.status,
                error_message=log_model.error_message,
                run_id=log_model.run_id,
                created_at=log_model.created_at,
            )
        finally:
            session.close()

    def get_user_executions(
        self,
        user_id: str,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> list[ExecutionLog]:
        """Get execution history for a user."""
        session = self.session_factory()
        try:
            stmt = select(ExecutionLogModel).filter_by(user_id=user_id)

            if strategy_id:
                stmt = stmt.filter_by(strategy_id=strategy_id)

            log_models = session.scalars(
                stmt.order_by(ExecutionLogModel.created_at.desc()).limit(limit)
            ).all()

            return [
                ExecutionLog(
                    log_id=lm.log_id,
                    user_id=lm.user_id,
                    account_id=lm.account_id,
                    strategy_id=lm.strategy_id,
                    canonical_signal_id=lm.canonical_signal_id,
                    signal_type=lm.signal_type,
                    execution_mode=lm.execution_mode,
                    execution_details=lm.execution_details or {},
                    status=lm.status,
                    error_message=lm.error_message,
                    run_id=lm.run_id,
                    created_at=lm.created_at,
                )
                for lm in log_models
            ]
        finally:
            session.close()
