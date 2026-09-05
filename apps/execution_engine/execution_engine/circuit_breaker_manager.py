"""Circuit-breaker lifecycle management for the execution engine.

Extracted from ``apps/execution_engine/execution_engine/engine.py`` as the
fourth step of the Phase-3 ExecutionEngine decomposition. This module owns the
breaker state machine (open / record-failure / clear / restore) plus the
``broker_system_error`` classifier that decides whether a particular broker
failure should count against the breaker.

Responsibilities:

* Compute breaker keys (``strategy_key`` / ``broker_key``).
* Open a breaker and persist its state via ``RiskBreachStore`` + emit an alert
  via ``AlertPublisher``.
* Record an incremental failure and trip the breaker when threshold is hit.
* Clear persisted breaker state on demand.
* Restore previously-open breakers on engine startup.
* Classify a broker error message/code as a "system" failure.

The class deliberately does NOT own:

* The orthogonal ``emit_alert`` helper (general-purpose, stays on engine).
* The sandbox certification marker (different lifecycle, stays on engine).

``ExecutionEngine`` keeps the same method names
(``_strategy_breaker_key``, ``_broker_breaker_key``, ``open_circuit_breaker``,
``_record_circuit_failure``, ``_clear_circuit_breaker_state``,
``_restore_circuit_breakers``, ``is_broker_system_error``) as thin delegates
so existing tests that patch those names continue to work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lib_common.logging import get_logger
from lib_common.metrics import counter

from .alerts import AlertPublisher, ExecutionAlert
from .circuit_breaker import CircuitBreaker
from .config import BrokerType
from .risk_breach_store import RiskBreachStore

logger = get_logger(__name__)

_CIRCUIT_BREAKER_BLOCKS_TOTAL = counter(
    "vm_execution_circuit_breaker_blocks_total",
    "Signals gated by an open circuit breaker, by scope and reduce-only disposition",
    ("scope", "broker", "disposition"),
)


_SYSTEM_ERROR_CODES = frozenset(
    {
        "auth_failed",
        "authentication_error",
        "unauthorized",
        "forbidden",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "service_unavailable",
        "connection_error",
        "timeout",
    }
)

_SYSTEM_ERROR_NEEDLES = (
    "unauthorized",
    "forbidden",
    "authentication",
    "invalid api key",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
    "broker unavailable",
    "failed to create broker bridge",
)


class CircuitBreakerManager:
    """State machine + persistence + alerting for execution circuit breakers."""

    def __init__(
        self,
        *,
        circuit_breaker: CircuitBreaker,
        alerts: AlertPublisher,
        risk_breach_store: RiskBreachStore | None = None,
    ) -> None:
        self._circuit_breaker = circuit_breaker
        self._alerts = alerts
        self._risk_breach_store = risk_breach_store

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def strategy_key(
        *,
        user_id: str,
        strategy_id: str,
        broker_code: str,
        credential_ref: str,
    ) -> str:
        return f"strategy:{user_id}:{strategy_id}:{broker_code}:{credential_ref}"

    @staticmethod
    def broker_key(*, broker_code: str, environment: str) -> str:
        return f"broker:{broker_code}:{environment}"

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def is_broker_system_error(
        *,
        error_message: str | None,
        error_code: str | None = None,
    ) -> bool:
        """Return True if a broker error should count against the breaker."""
        code = str(error_code or "").strip().lower()
        if code in _SYSTEM_ERROR_CODES:
            return True

        message = str(error_message or "").strip().lower()
        return any(needle in message for needle in _SYSTEM_ERROR_NEEDLES)

    # ------------------------------------------------------------------
    # Breaker lifecycle
    # ------------------------------------------------------------------

    def open(
        self,
        *,
        scope: str,
        breaker_key: str,
        user_id: str,
        strategy_id: str,
        broker: str,
        environment: str | None = None,
        reason: str,
        broker_account_id: int | None = None,
    ) -> None:
        """Open ``breaker_key``, log + alert + persist the state."""
        self._circuit_breaker.open(breaker_key, reason=reason)
        alert_rule_code = "broker_breaker_open" if scope == "broker" else "strategy_breaker_open"
        alert_payload: dict[str, Any] = {
            "breaker_key": breaker_key,
            "scope": scope,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "broker": broker,
            "broker_account_id": broker_account_id,
            "reason": reason,
        }
        if environment:
            alert_payload["environment"] = environment
        logger.error(
            "Circuit breaker opened",
            rule_code=alert_rule_code,
            **alert_payload,
        )
        self._alerts.publish(
            ExecutionAlert(
                event_type="circuit_breaker_open",
                severity="error",
                message=reason,
                payload=alert_payload,
            )
        )
        if self._risk_breach_store is not None:
            snapshot = self._circuit_breaker.snapshot(breaker_key)
            open_until_raw = snapshot.get("open_until")
            open_until = (
                datetime.fromisoformat(str(open_until_raw))
                if open_until_raw
                else datetime.now(tz=UTC)
            )
            self._risk_breach_store.upsert_circuit_breaker_state(
                user_id=user_id,
                scope=scope,
                breaker_key=breaker_key,
                open_until=open_until,
                failure_count=int(snapshot.get("failure_count") or 0),
                last_reason=reason,
                strategy_id=strategy_id,
                broker=broker,
                environment=environment,
                broker_account_id=broker_account_id,
            )

    def record_failure(
        self,
        *,
        scope: str,
        breaker_key: str,
        user_id: str,
        strategy_id: str,
        broker: str,
        environment: str | None,
        reason: str,
        broker_account_id: int | None = None,
    ) -> bool:
        """Record an incremental failure; open the breaker if threshold tripped."""
        opened = self._circuit_breaker.record_failure(breaker_key, reason=reason)
        if opened:
            self.open(
                scope=scope,
                breaker_key=breaker_key,
                user_id=user_id,
                strategy_id=strategy_id,
                broker=broker,
                environment=environment,
                reason=reason,
                broker_account_id=broker_account_id,
            )
        return opened

    def record_success(self, *breaker_keys: str) -> None:
        """Record successful dispatch and clear persisted state for its breakers."""
        for breaker_key in breaker_keys:
            self._circuit_breaker.record_success(breaker_key)
            self.clear_state(breaker_key)

    def is_open(self, breaker_key: str) -> bool:
        """Return whether ``breaker_key`` currently blocks new exposure."""
        return self._circuit_breaker.is_open(breaker_key)

    def check_execution(
        self,
        *,
        user_id: str,
        strategy_id: str,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        strategy_breaker_key: str,
        broker_breaker_key: str,
        broker_account_id: int | None = None,
        reduce_only: bool = False,
        trace_ctx: dict[str, Any] | None = None,
    ) -> str | None:
        """Apply broker-global then strategy-scoped breaker admission.

        Reduce-only exits bypass the strategy breaker so a strategy failure
        cannot trap exposure. The broker-global breaker remains a hard stop.
        """
        trace = trace_ctx or {}
        broker_breaker_open = self._circuit_breaker.is_open(broker_breaker_key)
        if not broker_breaker_open and self._circuit_breaker.consume_recently_closed(
            broker_breaker_key
        ):
            self.clear_state(broker_breaker_key)
        if broker_breaker_open:
            msg = "Broker-global circuit breaker is open for this broker/environment"
            logger.warning(
                "Execution blocked: circuit breaker open",
                **trace,
                block_reason="broker_breaker_open",
                breaker_key=broker_breaker_key,
                broker=broker_type.value,
                environment=environment,
            )
            if _CIRCUIT_BREAKER_BLOCKS_TOTAL is not None:
                _CIRCUIT_BREAKER_BLOCKS_TOTAL.labels(
                    scope="broker",
                    broker=broker_type.value,
                    disposition="blocked",
                ).inc()
            self._record_admission_block(
                user_id=user_id,
                strategy_id=strategy_id,
                broker_type=broker_type,
                environment=environment,
                credential_ref=credential_ref,
                rule_code="broker_breaker_open",
                broker_account_id=broker_account_id,
            )
            return msg

        strategy_breaker_open = self._circuit_breaker.is_open(strategy_breaker_key)
        if not strategy_breaker_open and self._circuit_breaker.consume_recently_closed(
            strategy_breaker_key
        ):
            self.clear_state(strategy_breaker_key)
        if not strategy_breaker_open:
            return None
        if reduce_only:
            logger.warning(
                "Circuit breaker open; allowing reduce-only order to flatten",
                **trace,
                block_reason="strategy_breaker_open",
                breaker_key=strategy_breaker_key,
                broker=broker_type.value,
                environment=environment,
            )
            if _CIRCUIT_BREAKER_BLOCKS_TOTAL is not None:
                _CIRCUIT_BREAKER_BLOCKS_TOTAL.labels(
                    scope="strategy",
                    broker=broker_type.value,
                    disposition="allowed_reduce_only",
                ).inc()
            return None

        msg = "Circuit breaker is open for this user/strategy/broker"
        logger.warning(
            "Execution blocked: circuit breaker open",
            **trace,
            block_reason="strategy_breaker_open",
            breaker_key=strategy_breaker_key,
            broker=broker_type.value,
            environment=environment,
        )
        if _CIRCUIT_BREAKER_BLOCKS_TOTAL is not None:
            _CIRCUIT_BREAKER_BLOCKS_TOTAL.labels(
                scope="strategy",
                broker=broker_type.value,
                disposition="blocked",
            ).inc()
        self._record_admission_block(
            user_id=user_id,
            strategy_id=strategy_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            rule_code="strategy_breaker_open",
            broker_account_id=broker_account_id,
        )
        return msg

    def _record_admission_block(
        self,
        *,
        user_id: str,
        strategy_id: str,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        rule_code: str,
        broker_account_id: int | None = None,
    ) -> None:
        if self._risk_breach_store is None:
            return
        self._risk_breach_store.record(
            user_id=user_id,
            rule_code=rule_code,
            severity="block",
            broker_account_id=broker_account_id,
            context={
                "strategy_id": strategy_id,
                "broker": broker_type.value,
                "environment": environment,
                "credential_ref": credential_ref,
            },
        )

    def clear_state(self, breaker_key: str) -> None:
        """Drop persisted state for ``breaker_key`` (no-op without a store)."""
        if self._risk_breach_store is None:
            return
        try:
            self._risk_breach_store.clear_circuit_breaker_state(breaker_key=breaker_key)
        except Exception:
            # Boundary catch: persistence failure must not abort the request
            # that triggered the clear; the next restore-cycle is the
            # recovery path.
            logger.exception(
                "Failed to clear persisted circuit breaker state",
                breaker_key=breaker_key,
            )

    def restore_all(self) -> None:
        """Reopen breakers from persisted state on engine startup."""
        if self._risk_breach_store is None:
            return
        try:
            for row in self._risk_breach_store.load_active_circuit_breakers():
                open_until = row.get("open_until")
                if not isinstance(open_until, datetime):
                    continue
                self._circuit_breaker.restore(
                    str(row.get("breaker_key") or ""),
                    open_until=open_until,
                    failure_count=int(row.get("failure_count") or 0),
                    last_reason=(
                        str(row["last_reason"]) if row.get("last_reason") is not None else None
                    ),
                )
        except Exception:
            # Boundary catch: persistence failure must not block engine
            # startup. The breaker subsystem falls back to in-memory state
            # and rebuilds as new failures are recorded.
            logger.exception("Failed to restore circuit breaker state")
