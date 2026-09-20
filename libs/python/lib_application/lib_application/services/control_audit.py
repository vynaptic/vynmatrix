"""One append-only audit row per owner control-plane write, refusals included.

``api_audit_logs`` is written inside the same transaction as the change it
describes, so a rolled-back change leaves no audit row claiming it happened. A
*refused* change has nothing to commit alongside, so its row is written in a
transaction of its own -- see ``append_refusal``.

The payload convention is metadata only: which fields changed, never their
values. Control-plane writes carry currency amounts, thresholds and account
identifiers, and an audit table is the wrong place to accumulate copies of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

OK = "ok"
ERROR = "error"


def append_audit(
    session: Session,
    *,
    user_id: str,
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None = None,
    account_id: int | None = None,
    status: str = OK,
) -> None:
    """Append an immutable tenant-owned audit row in the caller's transaction.

    The caller commits. ``status`` is ``ok`` or ``error``; the table's CHECK
    permits no third value.
    """
    from lib_application.db.models import ApiAuditLog  # noqa: PLC0415

    if status not in {OK, ERROR}:
        msg = f"Unsupported audit status: {status}"
        raise ValueError(msg)
    session.add(
        ApiAuditLog(
            user_id=user_id,
            account_id=account_id,
            action=action,
            req=request_payload,
            resp=response_payload,
            status=status,
        )
    )


def changed_fields(fields: Sequence[str]) -> dict[str, Any]:
    """The metadata-only request payload: which fields, never their values."""
    return {"fields": sorted(set(fields))}


__all__ = ["ERROR", "OK", "append_audit", "changed_fields"]
