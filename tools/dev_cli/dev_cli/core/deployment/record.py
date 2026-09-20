"""Open, close and read the deployment record.

The row is opened with ``outcome = 'failed'`` before the first destructive
stage, so a run that is interrupted leaves a visibly incomplete record instead
of no record at all. It is closed once, with the outcome that actually
happened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

TABLE = "deployments"
SUCCEEDED = "succeeded"
ROLLED_BACK = "rolled_back"
FAILED = "failed"
INSTALL = "install"
UPGRADE = "upgrade"

#: Columns a caller may read back; the deploy tool prints these and no others.
PUBLIC_FIELDS = (
    "deployment_id",
    "mode",
    "image_tag",
    "image_digest",
    "source_commit",
    "source_dirty",
    "alembic_head",
    "started_at",
    "finished_at",
    "outcome",
    "failure_stage",
    "snapshot_path",
    "snapshot_reason",
)


@contextmanager
def maintenance_session(url: str) -> Iterator[Session]:
    """A maintenance-authority session on an explicit, already-resolved URL."""
    from lib_application.db.session import (  # noqa: PLC0415
        create_engine_for_env,
        dispose_engine,
        get_session_factory,
    )
    from lib_application.services.database_authority import (  # noqa: PLC0415
        require_maintenance_database_role,
    )

    engine = create_engine_for_env(env="dev", db_url=url)
    try:
        with get_session_factory(engine=engine)() as session:
            require_maintenance_database_role(session)
            yield session
    finally:
        dispose_engine(engine)


def table_exists(session: Session) -> bool:
    """Whether this database has reached ``0108`` yet.

    The first upgrade onto this revision runs with the table still absent, so
    the record is opened again once the migration has created it.
    """
    bind = session.get_bind()
    return inspect(bind).has_table(TABLE)


def _row(value: Any) -> dict[str, Any]:
    return {field: getattr(value, field) for field in PUBLIC_FIELDS}


def latest(session: Session, *, outcome: str | None = None) -> dict[str, Any] | None:
    """The newest record, optionally the newest with one outcome."""
    from lib_application.db.models import Deployment  # noqa: PLC0415

    statement = select(Deployment).order_by(Deployment.deployment_id.desc()).limit(1)
    if outcome is not None:
        statement = (
            select(Deployment)
            .where(Deployment.outcome == outcome)
            .order_by(Deployment.deployment_id.desc())
            .limit(1)
        )
    found = session.scalars(statement).first()
    return None if found is None else _row(found)


def open_record(
    session: Session,
    *,
    mode: str,
    image_tag: str,
    image_digest: str,
    source_commit: str,
    source_dirty: bool,
    alembic_head: str,
    snapshot_path: Path | None,
    snapshot_reason: str | None,
) -> int:
    """Insert an in-flight record and return its identifier."""
    from lib_application.db.models import Deployment  # noqa: PLC0415

    if snapshot_path is None and not snapshot_reason:
        msg = "A deployment without a snapshot must record why"
        raise ValueError(msg)
    record = Deployment(
        mode=mode,
        image_tag=image_tag,
        image_digest=image_digest,
        source_commit=source_commit,
        source_dirty=source_dirty,
        alembic_head=alembic_head,
        started_at=datetime.now(tz=UTC),
        outcome=FAILED,
        snapshot_path=None if snapshot_path is None else str(snapshot_path),
        snapshot_reason=snapshot_reason,
    )
    session.add(record)
    session.flush()
    session.commit()
    return int(record.deployment_id)


def close_record(
    session: Session,
    deployment_id: int,
    *,
    outcome: str,
    failure_stage: str | None = None,
) -> None:
    """Record how the run ended, exactly once."""
    from lib_application.db.models import Deployment  # noqa: PLC0415

    if outcome not in {SUCCEEDED, ROLLED_BACK, FAILED}:
        msg = f"Unsupported deployment outcome: {outcome}"
        raise ValueError(msg)
    record = session.get(Deployment, deployment_id)
    if record is None:
        msg = "The deployment record disappeared before it could be closed"
        raise RuntimeError(msg)
    record.outcome = outcome
    record.failure_stage = failure_stage
    record.finished_at = datetime.now(tz=UTC)
    session.commit()


__all__ = [
    "FAILED",
    "INSTALL",
    "PUBLIC_FIELDS",
    "ROLLED_BACK",
    "SUCCEEDED",
    "TABLE",
    "UPGRADE",
    "close_record",
    "latest",
    "maintenance_session",
    "open_record",
    "table_exists",
]
