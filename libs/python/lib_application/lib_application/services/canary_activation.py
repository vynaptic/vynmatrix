"""Maintenance-only activation of an explicitly designated development test canary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import ApiAuditLog, Strategy, StrategyVersion
from lib_application.db.session import tenant_scope
from lib_application.services.catalogue import StrategyRelease, lock_catalogue
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.deployment_owner import require_deployment_owner_id


@dataclass(frozen=True)
class CanarySource:
    release: StrategyRelease
    decision: str | None
    enabled: bool
    environments: tuple[str, ...]

    def validate(self) -> None:
        self.release.validate()
        if (
            self.decision != "E2E_PIPELINE_CANARY_ONLY"
            or self.enabled is not True
            or self.environments != ("dev",)
        ):
            msg = "Source is not an enabled dev-only E2E pipeline canary"
            raise ValueError(msg)


def activate_canary(
    session: Session,
    source: CanarySource,
    *,
    environment: str,
    execution_mode: str,
    allow_live: str,
) -> dict[str, Any]:
    """Activate one existing exact release; caller owns commit and rollback."""
    source.validate()
    if (environment, execution_mode, allow_live) != ("dev", "paper", "false"):
        msg = "Canary activation requires explicit dev environment, paper mode and live gate false"
        raise ValueError(msg)
    require_maintenance_database_role(session)
    owner_id = require_deployment_owner_id(session)
    lock_catalogue(session)
    release = source.release
    with tenant_scope(session, user_id=owner_id):
        strategy = session.scalar(
            select(Strategy)
            .where(Strategy.strategy_id == release.strategy_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        version = session.scalar(
            select(StrategyVersion)
            .where(
                StrategyVersion.strategy_id == release.strategy_id,
                StrategyVersion.semver == release.semver,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if strategy is None or version is None:
            msg = "Canary requires an existing exact registered strategy version"
            raise ValueError(msg)
        if (
            strategy.strategy_name != release.strategy_name
            or strategy.asset_class != release.asset_class
            or version.default_params != release.default_params
            or version.param_schema != release.param_schema
        ):
            msg = "Registered canary release differs from immutable packaged source"
            raise ValueError(msg)
        if version.status not in {"registered", "active"}:
            msg = "A retired canary release cannot be activated"
            raise ValueError(msg)
        if version.status == "active" and not strategy.is_active:
            msg = "An explicitly disabled active canary cannot be reactivated"
            raise ValueError(msg)
        result = {
            "strategy_id": release.strategy_id,
            "version": release.semver,
            "changed": version.status == "registered",
        }
        if result["changed"]:
            version.status = "active"
            strategy.is_active = True
            session.add(
                ApiAuditLog(
                    user_id=owner_id,
                    action="strategy.activate_canary",
                    req={"strategy_id": release.strategy_id, "version": release.semver},
                    resp={"decision": source.decision, "environment": environment},
                    status="ok",
                )
            )
            session.flush()
        return result
