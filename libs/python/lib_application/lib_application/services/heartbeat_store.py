"""DB-backed service heartbeats.

Provides last-success liveness for services that cannot be Prometheus-scraped
directly — most notably the feedback loop, which runs as a one-shot and exits
before any in-process gauge is collected. A service records a heartbeat on each
successful run; an always-on service or external monitor reads the age to alert
on staleness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from lib_application.db.models import ServiceHeartbeat
from lib_strategy.signals.utils import ensure_utc

if TYPE_CHECKING:
    from sqlalchemy import Engine


class HeartbeatStore:
    """Record + read per-service last-success heartbeats."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        *,
        service_name: str,
        status: str = "ok",
        detail: str | None = None,
        now: datetime | None = None,
        preserve_degraded: bool = False,
    ) -> None:
        """Upsert the heartbeat row for ``service_name`` to the current time.

        Dialect-agnostic (select-then-update-or-insert) so it works identically
        on Postgres and the SQLite test fixtures. ``preserve_degraded`` keeps
        an earlier failure diagnostic from being overwritten by a later
        successful sub-step; the aggregate run heartbeat clears it only after
        the complete run succeeds.
        """
        ts = now or datetime.now(tz=UTC)
        with Session(self._engine) as session:
            row = session.get(ServiceHeartbeat, service_name, with_for_update=True)
            if row is None:
                session.add(
                    ServiceHeartbeat(
                        service_name=service_name,
                        last_success_at=ts,
                        last_status=status,
                        detail=detail,
                        updated_at=ts,
                    )
                )
            else:
                if preserve_degraded and row.last_status == "degraded" and status == "ok":
                    session.rollback()
                    return
                row.last_success_at = ts
                row.last_status = status
                row.detail = detail
                row.updated_at = ts
            session.commit()

    def last_success_age_seconds(
        self, service_name: str, *, now: datetime | None = None
    ) -> float | None:
        """Seconds since ``service_name`` last recorded a successful heartbeat.

        Returns ``None`` when the service has never recorded one (distinct from a
        large age) so a consumer can tell "never ran" apart from "ran long ago".
        """
        with Session(self._engine) as session:
            row = session.get(ServiceHeartbeat, service_name)
            if row is None:
                return None
            ref = now or datetime.now(tz=UTC)
            return (ref - ensure_utc(row.last_success_at)).total_seconds()
