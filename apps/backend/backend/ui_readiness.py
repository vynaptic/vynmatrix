"""Why a bound strategy is or is not trading: the owner UI's readiness checklist.

Each binding gets six checks in pipeline order. The first four are conditions
the platform requires before anything trades -- release, mode, account and a
signal worker processing market data. The last two report what happened most
recently: the latest scoring decision for the binding and the latest risk
block on its account. A failed condition is ``blocked``; history never blocks,
it only informs (``warn``).

Everything here is read-only and reports recorded state. It does not re-run any
gate, so where nothing is recorded a check says ``unknown`` rather than guess.
The backend role's column grants for these tables come from migration
``0109_backend_ui_readiness``; every statement selects explicit columns.
Timestamps are returned as ``datetime`` and serialised by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lib_application.db.models import ExecutionDecisionLog, RiskBreach, StrategyRuntimeState

OK: Final = "ok"
WARN: Final = "warn"
BLOCKED: Final = "blocked"
UNKNOWN: Final = "unknown"

DECISION_WINDOW: Final = timedelta(days=7)
RISK_WINDOW: Final = timedelta(hours=24)
# A worker is stale once it has missed this many of its own bars, and never
# sooner than the floor, so a minute strategy is not flagged for a short pause.
STALE_BARS: Final = 3
STALE_FLOOR: Final = timedelta(hours=1)
REASON_LIMIT: Final = 300

_MODE_CHECKS: Final[dict[str, tuple[str, str]]] = {
    "trading": (OK, "Trading: it opens and closes positions on its own."),
    "close_only": (WARN, "Close only: it may close positions it holds but opens nothing new."),
    "off": (BLOCKED, "Off: it will not trade."),
    "custom": (
        WARN,
        "A custom combination of authority flags. Choose a mode here to be sure what it does.",
    ),
}
_DECISION_CHECKS: Final[dict[str, tuple[str, str]]] = {
    "executed": (OK, "Its last signal was sent to execution."),
    "pending": (OK, "Its last signal is waiting for execution."),
    "executing": (OK, "Its last signal is being executed."),
    "skipped": (WARN, "Scoring skipped its last signal."),
    "rejected": (WARN, "Execution refused its last signal."),
    "failed": (WARN, "Executing its last signal failed."),
}


def _utc(value: datetime) -> datetime:
    """A comparable UTC instant; naive columns are UTC by convention."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _check(
    key: str, state: str, label: str, detail: str, at: datetime | None = None
) -> dict[str, Any]:
    return {"key": key, "state": state, "label": label, "detail": detail, "at": at}


def _worker_states(session: Session, strategy_ids: Iterable[str]) -> dict[str, Any]:
    """The freshest worker row per strategy; one strategy may run under several workers."""
    rows = session.execute(
        select(
            StrategyRuntimeState.strategy_id,
            StrategyRuntimeState.consolidation_minutes,
            StrategyRuntimeState.last_source_ts,
        ).where(StrategyRuntimeState.strategy_id.in_(sorted(set(strategy_ids))))
    ).all()
    freshest: dict[str, Any] = {}
    for row in rows:
        current = freshest.get(row.strategy_id)
        if current is None or (
            row.last_source_ts is not None
            and (current.last_source_ts is None or row.last_source_ts > current.last_source_ts)
        ):
            freshest[row.strategy_id] = row
    return freshest


def _latest_decisions(
    session: Session, owner_id: str, binding_ids: Iterable[int], since: datetime
) -> dict[int, Any]:
    """The owner's newest decision per binding inside the window."""
    rank = (
        func.row_number()
        .over(
            partition_by=ExecutionDecisionLog.binding_id,
            order_by=(ExecutionDecisionLog.ts.desc(), ExecutionDecisionLog.decision_id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(
            ExecutionDecisionLog.binding_id.label("binding_id"),
            ExecutionDecisionLog.status.label("status"),
            ExecutionDecisionLog.reasoning.label("reasoning"),
            ExecutionDecisionLog.ts.label("ts"),
            rank,
        )
        .where(
            ExecutionDecisionLog.user_id == owner_id,
            ExecutionDecisionLog.binding_id.in_(sorted(set(binding_ids))),
            ExecutionDecisionLog.ts >= since,
        )
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.binding_id, ranked.c.status, ranked.c.reasoning, ranked.c.ts).where(
            ranked.c.rank == 1
        )
    ).all()
    return {int(row.binding_id): row for row in rows}


def _latest_blocks(
    session: Session, owner_id: str, account_ids: Iterable[int], since: datetime
) -> dict[int, Any]:
    """The owner's newest blocking risk breach per account inside the window."""
    rank = (
        func.row_number()
        .over(
            partition_by=RiskBreach.broker_account_id,
            order_by=(RiskBreach.occurred_at.desc(), RiskBreach.breach_id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(
            RiskBreach.broker_account_id.label("account_id"),
            RiskBreach.rule_code.label("rule_code"),
            RiskBreach.occurred_at.label("occurred_at"),
            rank,
        )
        .where(
            RiskBreach.user_id == owner_id,
            RiskBreach.broker_account_id.in_(sorted(set(account_ids))),
            RiskBreach.severity == "block",
            RiskBreach.occurred_at >= since,
        )
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.account_id, ranked.c.rule_code, ranked.c.occurred_at).where(
            ranked.c.rank == 1
        )
    ).all()
    return {int(row.account_id): row for row in rows}


def _trigger_reason(reasoning: Any) -> str | None:
    """The one human-readable string the checklist shows from a decision document."""
    if not isinstance(reasoning, Mapping):
        return None
    reason = reasoning.get("trigger_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    text = " ".join(reason.split())
    return text if len(text) <= REASON_LIMIT else f"{text[: REASON_LIMIT - 1]}…"


def _worker_check(row: Any, now: datetime) -> dict[str, Any]:
    label = "Signal worker"
    if row is None:
        return _check(
            "worker",
            BLOCKED,
            label,
            "No signal worker has run this strategy here. A worker starts only when "
            "the strategy is enabled in its config.json and named in STRATEGY_LIST, "
            "and it needs market data ingested by a worker in PLATFORM_WORKERS.",
        )
    if row.last_source_ts is None:
        return _check(
            "worker", WARN, label, "A worker is registered but has not processed any market data."
        )
    last = _utc(row.last_source_ts)
    bar = timedelta(minutes=max(int(row.consolidation_minutes or 0), 1))
    if now - last > max(bar * STALE_BARS, STALE_FLOOR):
        return _check(
            "worker",
            WARN,
            label,
            f"The last bar it processed is more than {STALE_BARS} bars old. The worker "
            "may be stopped, or its market data may have stopped arriving.",
            last,
        )
    return _check("worker", OK, label, "It is processing market data.", last)


def _decision_check(row: Any) -> dict[str, Any]:
    label = "Last decision"
    if row is None:
        days = DECISION_WINDOW.days
        return _check(
            "decision",
            UNKNOWN,
            label,
            f"No decision in the last {days} days. Scoring records one each time a "
            "signal from this strategy reaches this binding.",
        )
    state, detail = _DECISION_CHECKS.get(
        str(row.status), (UNKNOWN, f"Its last decision is {row.status}.")
    )
    reason = _trigger_reason(row.reasoning) if row.status == "skipped" else None
    if reason:
        detail = f"{detail[:-1]}: {reason}"
    return _check("decision", state, label, detail, row.ts)


def _risk_check(row: Any) -> dict[str, Any]:
    label = "Risk limits"
    if row is None:
        hours = int(RISK_WINDOW.total_seconds() // 3600)
        return _check(
            "risk", OK, label, f"No risk rule blocked this account in the last {hours} hours."
        )
    return _check(
        "risk",
        WARN,
        label,
        f"Risk rule {row.rule_code} blocked an order on this account.",
        row.occurred_at,
    )


def binding_readiness(
    session: Session,
    owner_id: str,
    bindings: Iterable[Mapping[str, Any]],
    *,
    released_ids: set[str],
    account_status: Mapping[int, str],
    now: datetime | None = None,
) -> dict[int, dict[str, Any]]:
    """The checklist for each of the owner's bindings, keyed by ``binding_id``.

    ``bindings`` carry ``binding_id``, ``strategy_id``, ``account_id`` and
    ``mode`` as the strategies read model already built them.
    """
    items = list(bindings)
    if not items:
        return {}
    instant = _utc(now or datetime.now(tz=UTC))
    workers = _worker_states(session, (str(item["strategy_id"]) for item in items))
    decisions = _latest_decisions(
        session, owner_id, (int(item["binding_id"]) for item in items), instant - DECISION_WINDOW
    )
    blocks = _latest_blocks(
        session, owner_id, (int(item["account_id"]) for item in items), instant - RISK_WINDOW
    )

    result: dict[int, dict[str, Any]] = {}
    for item in items:
        binding_id = int(item["binding_id"])
        strategy_id = str(item["strategy_id"])
        status = account_status.get(int(item["account_id"]))
        mode_state, mode_detail = _MODE_CHECKS.get(
            str(item["mode"]), (UNKNOWN, f"Mode {item['mode']}.")
        )
        checks = [
            _check("release", OK, "Released", "Released for trading.")
            if strategy_id in released_ids
            else _check(
                "release",
                BLOCKED,
                "Released",
                "Not released for trading. Releasing is a maintenance action; "
                "it is not done from this page.",
            ),
            _check("mode", mode_state, "Mode", mode_detail),
            _check("account", OK, "Account", "The account is connected.")
            if status == "connected"
            else _check(
                "account",
                BLOCKED,
                "Account",
                f"The account is {status}. Only a connected account trades."
                if status
                else "The account was not found.",
            ),
            _worker_check(workers.get(strategy_id), instant),
            _decision_check(decisions.get(binding_id)),
            _risk_check(blocks.get(int(item["account_id"]))),
        ]
        result[binding_id] = {
            "ready": all(check["state"] != BLOCKED for check in checks),
            "checks": checks,
        }
    return result
