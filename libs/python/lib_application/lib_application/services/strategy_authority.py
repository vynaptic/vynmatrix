"""Exact current release eligibility for new executable work."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lib_application.db.models import Strategy, StrategyVersion


class StrategyReleaseError(ValueError):
    """The submitted release is not currently authorized for new work."""


def require_active_strategy_version(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str | None,
) -> int:
    """Return the exact active release ID, without substituting a newer version.

    This read gate belongs at new-work ingestion and broker-I/O boundaries.
    Historical lookup, fill accounting, and replay must retain original lineage
    independently of current catalogue eligibility.
    """
    version_id = None
    if strategy_version:
        version_id = session.scalar(
            select(StrategyVersion.strat_ver_id)
            .join(Strategy, Strategy.strategy_id == StrategyVersion.strategy_id)
            .where(
                Strategy.strategy_id == strategy_id,
                Strategy.is_active.is_(True),
                StrategyVersion.semver == strategy_version,
                StrategyVersion.status == "active",
            )
        )
    if version_id is None:
        message = (
            f"An exact active strategy release is required for new work: "
            f"strategy={strategy_id!r}, version={strategy_version!r}"
        )
        raise StrategyReleaseError(message)
    return int(version_id)
