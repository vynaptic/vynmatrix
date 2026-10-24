"""Maintenance-only release of one exact registered strategy version for paper.

Registration leaves every strategy unreleased. This module is the general,
evidence-gated way to release one exact version so the owner can bind it, and to
withdraw that release again. The evidence is a reviewed decision made checkable:
a committed paper-ready readiness label, a database row identical to the packaged
release, and a clean deployment that ships the same strategy source.

Only ``strategies.is_active``, ``strategy_versions.status`` and one audit row are
ever written. Nothing here reads or writes the live gate, a broker environment,
an account, a binding or a credential; withdrawal reads bindings, work queues
and the fill ledger only to refuse. The caller owns commit and rollback.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from lib_application.db.models import (
    AccountRebalancePlan,
    DecisionContext,
    Deployment,
    Execution,
    ExecutionDecisionLog,
    ExecutionLog,
    Order,
    OrderIntent,
    OutboxEvent,
    PendingOrder,
    Strategy,
    StrategyVersion,
    UserStrategyBinding,
)
from lib_application.db.session import tenant_scope
from lib_application.services.catalogue import StrategyRelease, lock_catalogue
from lib_application.services.control_audit import append_audit
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.deployment_owner import require_deployment_owner_id
from lib_common.logging import get_logger
from lib_common.strategy_readiness import PAPER_READY_STATES

logger = get_logger(__name__)

#: Catalogue decisions that keep a strategy. An allowlist, so a decision added to
#: the schema later is refused until someone decides it should be releasable.
RELEASE_DECISIONS = frozenset({"KEEP_HIGH_PRIORITY", "IMPROVE_THEN_VALIDATE"})
RELEASE_ENVIRONMENTS = frozenset({"dev", "staging", "production"})
MAX_REASON_LENGTH = 500

_RETIRED = frozenset({"deprecated", "pulled"})
_FINISHED_DEPLOYMENTS = frozenset({"succeeded", "rolled_back"})
# Non-terminal statuses, copied from 0098_retire_liquidity_leaders.
_OPEN_PENDING_ORDERS = ("pending", "submission_unknown", "submitted", "working", "partially_filled")
_OPEN_EXECUTION_LOGS = (
    "pending",
    "executing",
    "submitted",
    "partially_filled",
    "accepted",
    "working",
)
_OPEN_DECISIONS = ("pending", "executing")
# A dead letter is deliberately absent: redriving one after withdrawal fails at
# the release gate, so it cannot trade, and counting it would block forever.
_OPEN_OUTBOX = ("pending", "in_progress", "failed")
_DEAD_LETTER = "dead_letter"
_OPEN_REBALANCE_PLANS = ("pending", "running", "blocked")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def require_release_labels(strategy_id: str, *, readiness: str, decision: str) -> None:
    """Refuse a strategy whose reviewed catalogue labels do not certify it for paper."""
    if readiness not in PAPER_READY_STATES:
        msg = (
            f"{strategy_id} readiness {readiness!r} is not paper-ready; a reviewed commit "
            f"must set metadata.readiness to one of {sorted(PAPER_READY_STATES)}"
        )
        raise ValueError(msg)
    if decision not in RELEASE_DECISIONS:
        msg = (
            f"{strategy_id} decision {decision!r} does not keep the strategy; "
            f"releasable decisions are {sorted(RELEASE_DECISIONS)}"
        )
        raise ValueError(msg)


@dataclass(frozen=True)
class ReleaseSource:
    """The packaged release and the reviewed evidence a release relies on."""

    release: StrategyRelease
    readiness: str
    decision: str
    source_commit: str
    config_sha256: str
    strategy_tree: str
    deployment_id: int

    def validate(self) -> None:
        self.release.validate()
        require_release_labels(
            self.release.strategy_id, readiness=self.readiness, decision=self.decision
        )
        for field in ("source_commit", "strategy_tree"):
            if not _COMMIT_RE.fullmatch(getattr(self, field)):
                msg = f"{field} must be a full lowercase Git object ID"
                raise ValueError(msg)
        if not _SHA256_RE.fullmatch(self.config_sha256):
            msg = "config_sha256 must be a lowercase SHA-256 digest"
            raise ValueError(msg)
        if isinstance(self.deployment_id, bool) or self.deployment_id <= 0:
            msg = "deployment_id must be a positive integer"
            raise ValueError(msg)


def require_paper_runtime(*, environment: str, execution_mode: str, allow_live: str) -> None:
    """Refuse unless the resolved configuration is paper with the live gate closed."""
    if (
        environment not in RELEASE_ENVIRONMENTS
        or execution_mode != "paper"
        or allow_live != "false"
    ):
        msg = (
            "Strategy release requires ENVIRONMENT dev, staging or production, "
            "EXECUTION_MODE=paper and EXECUTION_ENGINE_ALLOW_LIVE=false"
        )
        raise ValueError(msg)


def _lock_rows(
    session: Session, strategy_id: str, semver: str
) -> tuple[Strategy | None, StrategyVersion | None]:
    strategy = session.scalar(
        select(Strategy)
        .where(Strategy.strategy_id == strategy_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    version = session.scalar(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id, StrategyVersion.semver == semver)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return strategy, version


def lock_registered_release(
    session: Session, release: StrategyRelease
) -> tuple[Strategy, StrategyVersion]:
    """Lock the exact rows of a packaged release and require them to be identical."""
    strategy, version = _lock_rows(session, release.strategy_id, release.semver)
    if strategy is None or version is None:
        msg = "Release requires an existing exact registered strategy version"
        raise ValueError(msg)
    if (
        strategy.strategy_name != release.strategy_name
        or strategy.asset_class != release.asset_class
        or version.default_params != release.default_params
        or version.param_schema != release.param_schema
    ):
        msg = "Registered release differs from immutable packaged source"
        raise ValueError(msg)
    return strategy, version


def current_deployment(session: Session) -> Deployment:
    """Return the deployment whose image is running, if its source is provable."""
    newest = session.scalars(
        select(Deployment).order_by(Deployment.deployment_id.desc()).limit(1)
    ).first()
    if newest is None:
        msg = "No deployment is recorded; install or upgrade with vmdev deploy first"
        raise ValueError(msg)
    if newest.outcome not in _FINISHED_DEPLOYMENTS:
        msg = (
            f"Deployment {newest.deployment_id} did not finish ({newest.outcome}); "
            "the running image is unknown"
        )
        raise ValueError(msg)
    running = session.scalars(
        select(Deployment)
        .where(Deployment.outcome == "succeeded")
        .order_by(Deployment.deployment_id.desc())
        .limit(1)
    ).first()
    if running is None:
        msg = "No deployment has succeeded; install with vmdev deploy first"
        raise ValueError(msg)
    if running.source_dirty:
        msg = (
            f"Deployment {running.deployment_id} was built from a checkout with "
            "uncommitted or untracked files, so its strategy source is unproven; "
            "redeploy from a clean checkout"
        )
        raise ValueError(msg)
    return running


def _require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text or len(text) > MAX_REASON_LENGTH:
        msg = f"--apply requires a non-blank reason of at most {MAX_REASON_LENGTH} characters"
        raise ValueError(msg)
    return text


def _result(
    strategy_id: str,
    version: str,
    *,
    apply: bool,
    would_change: bool,
    status: str,
    strategy_is_active: bool,
) -> dict[str, Any]:
    """Report the state after this command; ``--check`` adds what ``--apply`` would do."""
    result: dict[str, Any] = {
        "strategy_id": strategy_id,
        "version": version,
        "mode": "apply" if apply else "check",
        "changed": apply and would_change,
        "status": status,
        "strategy_is_active": strategy_is_active,
    }
    if not apply:
        result["would_change"] = would_change
    return result


def release_strategy_version(
    session: Session,
    source: ReleaseSource,
    *,
    environment: str,
    execution_mode: str,
    allow_live: str,
    reason: str | None,
    apply: bool,
) -> dict[str, Any]:
    """Release one exact registered version for paper; ``apply=False`` writes nothing."""
    source.validate()
    require_paper_runtime(
        environment=environment, execution_mode=execution_mode, allow_live=allow_live
    )
    note = _require_reason(reason) if apply else None
    require_maintenance_database_role(session)
    owner_id = require_deployment_owner_id(session)
    lock_catalogue(session)
    release = source.release
    with tenant_scope(session, user_id=owner_id):
        if current_deployment(session).deployment_id != source.deployment_id:
            msg = "The running deployment changed while the release was checked; retry"
            raise ValueError(msg)
        strategy, version = lock_registered_release(session, release)
        if version.status in _RETIRED:
            msg = f"A {version.status} strategy version cannot be released; ship a new semver"
            raise ValueError(msg)
        if version.status == "active":
            if strategy.is_active is not True:
                msg = "This version is active but its strategy is switched off; withdraw it first"
                raise ValueError(msg)
            return _result(
                release.strategy_id,
                release.semver,
                apply=apply,
                would_change=False,
                status="active",
                strategy_is_active=True,
            )
        other = session.scalar(
            select(StrategyVersion.semver).where(
                StrategyVersion.strategy_id == release.strategy_id,
                StrategyVersion.status == "active",
                StrategyVersion.semver != release.semver,
            )
        )
        if other is not None:
            msg = f"{release.strategy_id} {other} is released; withdraw it first"
            raise ValueError(msg)
        current = (version.status, bool(strategy.is_active))
        if apply:
            version.status = "active"
            strategy.is_active = True
            response = {
                "environment": environment,
                "execution_mode": execution_mode,
                "allow_live": allow_live,
                "previous_status": current[0],
                "status": "active",
                "previous_strategy_is_active": current[1],
                "strategy_is_active": True,
            }
            request = {
                "strategy_id": release.strategy_id,
                "version": release.semver,
                "reason": note,
                "source_commit": source.source_commit,
                "config_sha256": source.config_sha256,
                "strategy_tree": source.strategy_tree,
                "readiness": source.readiness,
                "decision": source.decision,
                "deployment_id": source.deployment_id,
            }
            append_audit(
                session,
                user_id=owner_id,
                action="strategy.release",
                request_payload=request,
                response_payload=response,
            )
            session.flush()
            logger.info("Released strategy for paper", **request, **response)
            current = ("active", True)
        return _result(
            release.strategy_id,
            release.semver,
            apply=apply,
            would_change=True,
            status=current[0],
            strategy_is_active=current[1],
        )


def _outbox_names(payload: Any, strategy_id: str) -> bool:
    """Whether an event names the strategy at the top level or in its signal."""
    if not isinstance(payload, Mapping):
        return False
    signal = payload.get("signal")
    nested = signal.get("strategy_id") if isinstance(signal, Mapping) else None
    return strategy_id in {str(payload.get("strategy_id") or ""), str(nested or "")}


def _outbox_counts(session: Session, strategy_id: str) -> tuple[int, int]:
    rows = session.execute(
        select(OutboxEvent.status, OutboxEvent.payload).where(
            OutboxEvent.status.in_((*_OPEN_OUTBOX, _DEAD_LETTER))
        )
    ).all()
    matching = [status for status, payload in rows if _outbox_names(payload, strategy_id)]
    dead = sum(1 for status in matching if status == _DEAD_LETTER)
    return len(matching) - dead, dead


def _open_decisions(session: Session, strategy_id: str) -> int:
    binding = aliased(UserStrategyBinding)
    context = aliased(DecisionContext)
    return int(
        session.scalar(
            select(func.count(func.distinct(ExecutionDecisionLog.decision_id)))
            .select_from(ExecutionDecisionLog)
            .outerjoin(binding, binding.binding_id == ExecutionDecisionLog.binding_id)
            .outerjoin(context, context.signal_id == ExecutionDecisionLog.signal_id)
            .where(
                ExecutionDecisionLog.status.in_(_OPEN_DECISIONS),
                or_(
                    binding.strategy_id == strategy_id,
                    context.strategy_id == strategy_id,
                    and_(binding.strategy_id.is_(None), context.strategy_id.is_(None)),
                ),
            )
        )
        or 0
    )


def _open_positions(session: Session, strategy_id: str) -> int:
    """Count account/instrument pairs this strategy's canonical fills leave non-flat.

    ``positions`` carries no strategy attribution, but every fill does, through
    its order intent; the net of signed fills is the position the strategy holds.
    """
    signed = case((OrderIntent.side == "BUY", Execution.qty), else_=-Execution.qty)
    rows = session.execute(
        select(func.sum(signed))
        .select_from(Execution)
        .join(Order, Order.order_id == Execution.order_id)
        .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .where(OrderIntent.strategy_id == strategy_id)
        .group_by(Order.account_id, Execution.instr_id)
    ).all()
    return sum(1 for (net,) in rows if net is not None and Decimal(str(net)) != 0)


def _require_nothing_in_flight(session: Session, strategy_id: str) -> int:
    """Refuse while anything could still trade this strategy; return dead letters."""
    switched_on = session.scalar(
        select(func.count(UserStrategyBinding.binding_id)).where(
            UserStrategyBinding.strategy_id == strategy_id,
            or_(
                UserStrategyBinding.is_active.is_(True),
                UserStrategyBinding.entries_enabled.is_(True),
                UserStrategyBinding.exits_enabled.is_(True),
                UserStrategyBinding.autopilot.is_(True),
            ),
        )
    )
    orders = session.scalar(
        select(func.count(PendingOrder.order_id)).where(
            PendingOrder.strategy_id == strategy_id,
            PendingOrder.status.in_(_OPEN_PENDING_ORDERS),
        )
    )
    executions = session.scalar(
        select(func.count(ExecutionLog.log_id)).where(
            ExecutionLog.strategy_id == strategy_id,
            ExecutionLog.status.in_(_OPEN_EXECUTION_LOGS),
        )
    )
    plans = session.scalar(
        select(func.count(AccountRebalancePlan.account_plan_id)).where(
            AccountRebalancePlan.strategy_id == strategy_id,
            AccountRebalancePlan.status.in_(_OPEN_REBALANCE_PLANS),
        )
    )
    events, dead_letters = _outbox_counts(session, strategy_id)
    blockers = {
        "binding(s) not switched off": int(switched_on or 0),
        "open position(s)": _open_positions(session, strategy_id),
        "open or resting order(s)": int(orders or 0),
        "unfinished execution(s)": int(executions or 0),
        "pending scoring decision(s)": _open_decisions(session, strategy_id),
        "unfinished rebalance plan(s)": int(plans or 0),
        "undelivered outbox event(s)": events,
    }
    found = [f"{count} {label}" for label, count in blockers.items() if count]
    if found:
        msg = f"Cannot withdraw {strategy_id}: " + ", ".join(found)
        raise ValueError(msg)
    return dead_letters


def withdraw_strategy_version(
    session: Session,
    *,
    strategy_id: str,
    version: str,
    environment: str,
    execution_mode: str,
    allow_live: str,
    reason: str | None,
    apply: bool,
) -> dict[str, Any]:
    """Return a released version to ``registered``; ``apply=False`` writes nothing.

    The inputs are database rows only, so an old version can be withdrawn after
    the checkout has moved on or its readiness label has been lowered.
    """
    require_paper_runtime(
        environment=environment, execution_mode=execution_mode, allow_live=allow_live
    )
    note = _require_reason(reason) if apply else None
    require_maintenance_database_role(session)
    owner_id = require_deployment_owner_id(session)
    lock_catalogue(session)
    with tenant_scope(session, user_id=owner_id):
        strategy, row = _lock_rows(session, strategy_id, version)
        if strategy is None or row is None:
            msg = f"No registered version {version} of {strategy_id}"
            raise ValueError(msg)
        if row.status in _RETIRED:
            msg = f"A {row.status} strategy version is retired, not released"
            raise ValueError(msg)
        if row.status != "active":
            if strategy.is_active is True:
                msg = f"Version {version} of {strategy_id} is not the released version"
                raise ValueError(msg)
            return {
                **_result(
                    strategy_id,
                    version,
                    apply=apply,
                    would_change=False,
                    status=row.status,
                    strategy_is_active=False,
                ),
                "dead_letter_count": _outbox_counts(session, strategy_id)[1],
            }
        dead_letters = _require_nothing_in_flight(session, strategy_id)
        current = (row.status, bool(strategy.is_active))
        if apply:
            response = {
                "environment": environment,
                "execution_mode": execution_mode,
                "allow_live": allow_live,
                "previous_status": row.status,
                "status": "registered",
                "previous_strategy_is_active": strategy.is_active,
                "strategy_is_active": False,
                "dead_letter_count": dead_letters,
            }
            row.status = "registered"
            strategy.is_active = False
            request = {"strategy_id": strategy_id, "version": version, "reason": note}
            append_audit(
                session,
                user_id=owner_id,
                action="strategy.withdraw",
                request_payload=request,
                response_payload=response,
            )
            session.flush()
            logger.info("Withdrew strategy release", **request, **response)
            current = ("registered", False)
        return {
            **_result(
                strategy_id,
                version,
                apply=apply,
                would_change=True,
                status=current[0],
                strategy_is_active=current[1],
            ),
            "dead_letter_count": dead_letters,
        }


__all__ = [
    "MAX_REASON_LENGTH",
    "RELEASE_DECISIONS",
    "RELEASE_ENVIRONMENTS",
    "ReleaseSource",
    "current_deployment",
    "lock_registered_release",
    "release_strategy_version",
    "require_paper_runtime",
    "require_release_labels",
    "withdraw_strategy_version",
]
