"""Resolve deployment authority without unrestricted runtime access to users."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import User


class DeploymentOwnerError(RuntimeError):
    """Deployment ownership is absent, inactive, or ambiguous."""


def require_deployment_owner_id(session: Session) -> str:
    """Require explicit active ownership; never cache or infer an owner identity.

    PostgreSQL discovery runs before tenant scope through a migration-owned,
    narrowly granted function. SQLite lookup is only for isolated unit stores.
    This read does not designate an owner or change the caller's tenant scope.
    """
    dialect = session.get_bind().dialect.name
    owner_id: str | None = None
    if dialect == "postgresql":
        owner_id = session.execute(text("SELECT public.vm_deployment_owner_id()")).scalar_one()
    elif dialect == "sqlite":
        owners = session.execute(
            select(User.user_id, User.status).where(User.is_deployment_owner.is_(True)).limit(2)
        ).all()
        if len(owners) == 1 and owners[0].status == "active":
            owner_id = owners[0].user_id
    else:
        message = f"Deployment owner discovery is unsupported for {dialect}"
        raise DeploymentOwnerError(message)
    if not owner_id:
        message = (
            "Exactly one active deployment owner is required; use maintenance authority "
            "to initialize or explicitly adopt the deployment owner."
        )
        raise DeploymentOwnerError(message)
    return owner_id
