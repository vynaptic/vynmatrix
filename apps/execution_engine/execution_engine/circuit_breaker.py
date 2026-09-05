"""In-memory circuit breaker for repeated execution failures."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class CircuitBreakerState:
    failure_times: deque[datetime]
    open_until: datetime | None = None
    last_reason: str | None = None


class CircuitBreaker:
    """A simple time-window breaker keyed by user/strategy/broker."""

    def __init__(self, *, threshold: int, window_sec: int, cooldown_sec: int) -> None:
        self._threshold = threshold
        self._window = timedelta(seconds=window_sec)
        self._cooldown = timedelta(seconds=cooldown_sec)
        self._states: dict[str, CircuitBreakerState] = defaultdict(
            lambda: CircuitBreakerState(deque())
        )
        self._recently_closed: set[str] = set()

    def is_open(self, key: str, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(tz=UTC)
        state = self._states[key]
        if state.open_until and current >= state.open_until:
            self.close(key, remember_transition=True)
        return bool(state.open_until and current < state.open_until)

    def open(self, key: str, *, reason: str, now: datetime | None = None) -> None:
        current = now or datetime.now(tz=UTC)
        state = self._states[key]
        state.open_until = current + self._cooldown
        state.last_reason = reason
        self._recently_closed.discard(key)

    def restore(
        self,
        key: str,
        *,
        open_until: datetime,
        failure_count: int = 0,
        last_reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(tz=UTC)
        target_open_until = open_until.astimezone(UTC)
        if target_open_until <= current:
            self.close(key)
            return
        state = self._states[key]
        state.open_until = target_open_until
        state.last_reason = last_reason
        state.failure_times = deque(current for _ in range(max(int(failure_count), 0)))
        self._recently_closed.discard(key)

    def record_failure(
        self,
        key: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(tz=UTC)
        state = self._states[key]
        cutoff = current - self._window
        while state.failure_times and state.failure_times[0] < cutoff:
            state.failure_times.popleft()
        state.failure_times.append(current)
        state.last_reason = reason
        if len(state.failure_times) >= self._threshold:
            self.open(key, reason=reason, now=current)
            return True
        return False

    def record_success(self, key: str) -> None:
        self.close(key)

    def close(self, key: str, *, remember_transition: bool = False) -> None:
        state = self._states[key]
        state.failure_times.clear()
        state.open_until = None
        state.last_reason = None
        if remember_transition:
            self._recently_closed.add(key)
        else:
            self._recently_closed.discard(key)

    def consume_recently_closed(self, key: str) -> bool:
        if key in self._recently_closed:
            self._recently_closed.discard(key)
            return True
        return False

    def snapshot(self, key: str) -> dict:
        state = self._states[key]
        return {
            "state": "open" if state.open_until else "closed",
            "open_until": state.open_until.isoformat() if state.open_until else None,
            "last_reason": state.last_reason,
            "failure_count": len(state.failure_times),
        }
