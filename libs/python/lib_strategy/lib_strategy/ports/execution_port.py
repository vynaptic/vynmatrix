"""Port for execution-log persistence."""

from abc import ABC, abstractmethod

from lib_strategy.domain.entities import ExecutionLog


class IExecutionRepository(ABC):
    """Interface for execution-log persistence."""

    @abstractmethod
    def log_execution(self, execution_log: ExecutionLog) -> str:
        """
        Log an execution.

        Args:
            execution_log: ExecutionLog entity

        Returns:
            log_id of created log entry
        """

    @abstractmethod
    def get_execution_log(self, log_id: str) -> ExecutionLog | None:
        """
        Get execution log by ID.

        Args:
            log_id: Log ID

        Returns:
            ExecutionLog entity or None if not found
        """

    @abstractmethod
    def get_user_executions(
        self,
        user_id: str,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> list[ExecutionLog]:
        """
        Get execution history for a user.

        Args:
            user_id: User ID
            strategy_id: Optional filter by strategy
            limit: Maximum number of results

        Returns:
            List of ExecutionLog entities
        """
