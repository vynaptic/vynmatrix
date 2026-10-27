"""One durable fence for every owner research mutation and maintenance lifecycle."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models.research_jobs import ResearchControl

RESEARCH_LAUNCH_LOCK = "vynmatrix.research.launch"
RESEARCH_CONTROL_LOCK = "vynmatrix.research.control"
CONTROL_BUSY_DETAIL = "research is fenced for maintenance or another control transaction"


class ResearchWorkflowError(ValueError):
    """A bounded owner-visible research refusal."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def transaction_lock(session: Session, key: str) -> None:
    """Serialize owner/control mutations without granting user-table writes."""
    if session.get_bind().dialect.name == "postgresql":
        lock_function = "pg_try_advisory_xact_lock" if key == "control" else "pg_advisory_xact_lock"
        acquired = session.scalar(
            text(f"SELECT {lock_function}(hashtextextended(:key, 0))"),
            {"key": RESEARCH_CONTROL_LOCK if key == "control" else "vynmatrix.research." + key},
        )
        if key == "control" and acquired is not True:
            msg = CONTROL_BUSY_DETAIL
            raise ResearchWorkflowError(msg, status_code=503)


def get_research_control(session: Session, *, lock: bool = True) -> ResearchControl:
    if lock:
        transaction_lock(session, "control")
    row = session.scalar(
        select(ResearchControl)
        .where(ResearchControl.control_id == 1)
        .execution_options(populate_existing=True)
    )
    if row is None:
        msg = "research control is not initialized"
        raise ResearchWorkflowError(msg, status_code=503)
    return row


def require_research_admission(session: Session) -> ResearchControl:
    """Lock admission through commit and refuse writes during maintenance."""
    control = get_research_control(session)
    if control.maintenance:
        msg = "research is fenced for maintenance"
        raise ResearchWorkflowError(msg, status_code=503)
    return control
